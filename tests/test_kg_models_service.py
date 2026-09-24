"""Knowledge graph: models, canonicalization, GraphService rules, persistence and queries.

Runs on an isolated in-memory SQLite database with foreign keys enforced (and on a disposable
PostgreSQL too when JARVIS_TEST_DATABASE_URL is set)."""

import pytest
from pydantic import ValidationError

from agent.knowledge_graph.models import (
    Confidence,
    Entity,
    EntityType as E,
    GraphStatus,
    GraphStorageError,
    GraphValidationError,
    Provenance,
    RelationshipType as R,
    SourceKind,
    TrustLevel,
)
from agent.knowledge_graph.normalize import name_key
from agent.knowledge_graph.repository import GraphRepository
from agent.knowledge_graph.rules import ALLOWED, is_allowed
from tests.kg_helpers import Clock, doc_prov, fact, make_graph, memory_prov, seed_example


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def graph(session_factory, clock):
    return make_graph(session_factory, clock)


# ---- models / vocabulary ----

def test_entity_and_relationship_taxonomies_are_small_and_controlled():
    assert {t.name for t in E} == {"PERSON", "PROJECT", "TECHNOLOGY", "ORGANIZATION", "DOCUMENT", "SKILL", "GOAL", "LOCATION", "TOPIC", "EVENT"}  # EVENT: Phase 11
    assert set(R) == set(ALLOWED)  # every relationship type has explicit allowed endpoints


def test_entity_model_validation():
    e = Entity(entity_type=E.PROJECT, canonical_name="  JARVIS   assistant ", name_key="jarvis")
    assert e.canonical_name == "JARVIS assistant" and e.status is GraphStatus.ACTIVE and len(e.entity_id) == 32
    assert e.created_at.tzinfo is not None
    for bad in ("", "   ", "x" * 121):
        with pytest.raises(ValidationError):
            Entity(entity_type=E.PROJECT, canonical_name=bad, name_key="k")
    with pytest.raises(ValidationError):
        Entity(entity_type="not-a-type", canonical_name="x", name_key="x")


def test_provenance_is_never_fabricated():
    for kwargs in (
        {"source_kind": SourceKind.PERSONAL_MEMORY},  # no memory id
        {"source_kind": SourceKind.PERSONAL_DOCUMENT, "source_id": "d"},  # no filename
        {"source_kind": SourceKind.IMPORTED_SOURCE},
    ):
        with pytest.raises(ValidationError):
            Provenance(trust=TrustLevel.VERIFIED_SOURCE, **kwargs)
    with pytest.raises(ValidationError):  # a document is not the user speaking
        Provenance(source_kind=SourceKind.PERSONAL_DOCUMENT, source_id="d", source_name="a.pdf", trust=TrustLevel.EXPLICIT_USER)
    with pytest.raises(ValidationError):  # inferred facts are LOW confidence only
        Provenance(source_kind=SourceKind.PERSONAL_MEMORY, source_id="m", trust=TrustLevel.INFERRED, confidence=Confidence.HIGH)
    assert Provenance(source_kind=SourceKind.EXPLICIT_USER_STATEMENT).trust is TrustLevel.EXPLICIT_USER


def test_trust_levels_are_ordered():
    assert TrustLevel.INFERRED < TrustLevel.VERIFIED_SOURCE < TrustLevel.EXPLICIT_USER


def test_allowed_relationship_combinations():
    assert is_allowed(E.PROJECT, R.USES, E.TECHNOLOGY) and is_allowed(E.PERSON, R.WORKS_ON, E.PROJECT)
    assert not is_allowed(E.TECHNOLOGY, R.WORKS_ON, E.PERSON)
    assert not is_allowed(E.PERSON, R.USES, E.DOCUMENT)
    assert not is_allowed(E.PROJECT, R.MENTIONS, E.TECHNOLOGY)  # only documents mention
    assert not is_allowed(E.DOCUMENT, R.PART_OF, E.PROJECT)


# ---- canonicalization ----

@pytest.mark.parametrize("name", ["Python", "python", "PYTHON", " Python ", "Python programming language", "python language", "Python."])
def test_python_variants_share_one_key(name):
    assert name_key(name, E.TECHNOLOGY) == "python"


def test_project_suffixes_are_stripped_but_distinct_names_are_not_merged():
    assert name_key("JARVIS", E.PROJECT) == name_key("Jarvis", E.PROJECT) == name_key("JARVIS assistant", E.PROJECT) == "jarvis"
    assert name_key("Virtual Campus project", E.PROJECT) == "virtual campus"
    assert name_key("Jarvis Mark 2", E.PROJECT) != "jarvis"
    assert name_key("JavaScript", E.TECHNOLOGY) != name_key("Java", E.TECHNOLOGY)
    assert name_key("C++", E.TECHNOLOGY) == "c++" and name_key("Café", E.TOPIC) == "cafe"


def test_a_name_is_never_reduced_to_nothing():
    assert name_key("Project", E.PROJECT) == "project"


def test_entity_creation_deduplicates_case_and_generic_suffix(graph):
    a = graph.create_entity(E.TECHNOLOGY, "Python")
    b = graph.create_entity(E.TECHNOLOGY, "python")
    c = graph.create_entity(E.TECHNOLOGY, "PYTHON programming language")
    assert a.entity_id == b.entity_id == c.entity_id
    assert len(graph.search_entities("python")) == 1
    assert "python programming language".casefold() in [x.casefold() for x in graph.get_entity(a.entity_id).metadata["aliases"]]
    assert graph.get_entity(a.entity_id).canonical_name == "Python"


def test_same_name_under_different_types_is_two_entities(graph):
    tech = graph.create_entity(E.TECHNOLOGY, "Java")
    place = graph.create_entity(E.LOCATION, "Java")
    assert tech.entity_id != place.entity_id


def test_resolution_is_deterministic_and_never_guesses(graph):
    tech = graph.create_entity(E.TECHNOLOGY, "Java")
    assert graph.resolve_entity("java").entity_id == tech.entity_id
    assert graph.resolve_entity("Java", E.TECHNOLOGY).entity_id == tech.entity_id
    graph.create_entity(E.LOCATION, "Java")
    assert graph.resolve_entity("Java") is None  # ambiguous across types: not guessed
    assert graph.resolve_entity("Java", E.LOCATION) is not None
    assert graph.resolve_entity("Kotlin") is None
    assert graph.resolve_entity("Jarvis Mark 2", E.PROJECT) is None


def test_entity_lookup_and_persistence_across_service_instances(session_factory, clock):
    first = make_graph(session_factory, clock)
    e = first.create_entity(E.PROJECT, "JARVIS", description="Voice assistant", metadata={"k": 1})
    second = make_graph(session_factory, clock)
    got = second.get_entity(e.entity_id)
    assert (got.canonical_name, got.description, got.metadata["k"], got.entity_type) == ("JARVIS", "Voice assistant", 1, E.PROJECT)
    assert got.created_at.tzinfo is not None and second.get_entity("f" * 32) is None


def test_invalid_entity_names_are_rejected(graph):
    for bad in ("", "   ", "x" * 200, "..."):
        with pytest.raises(GraphValidationError):
            graph.create_entity(E.TOPIC, bad)


# ---- relationships ----

def test_relationship_creation_with_provenance_and_trust(graph):
    user, jarvis = graph.user_entity(), graph.create_entity(E.PROJECT, "JARVIS")
    rel = graph.create_relationship(user.entity_id, R.WORKS_ON, jarvis.entity_id, memory_prov("m1"))
    assert (rel.status, rel.trust, rel.confidence) == (GraphStatus.ACTIVE, TrustLevel.EXPLICIT_USER, Confidence.HIGH)
    assert rel.valid_from and rel.valid_until is None
    [p] = graph.get_provenance(rel.relationship_id)
    assert (p.source_kind, p.source_id, p.active) == (SourceKind.PERSONAL_MEMORY, "m1", True)
    assert graph.get_relationship(rel.relationship_id).relationship_type is R.WORKS_ON


def test_relationship_validation_rejects_bad_edges(graph):
    user, py = graph.user_entity(), graph.create_entity(E.TECHNOLOGY, "Python")
    doc = graph.create_entity(E.DOCUMENT, "a.pdf")
    p = memory_prov()
    with pytest.raises(GraphValidationError):
        graph.create_relationship(user.entity_id, R.WORKS_ON, py.entity_id, p)  # person -> technology
    with pytest.raises(GraphValidationError):
        graph.create_relationship(py.entity_id, R.USES, py.entity_id, p)  # self loop
    with pytest.raises(GraphValidationError):
        graph.create_relationship(user.entity_id, R.PREFERS, "0" * 32, p)  # unknown entity
    with pytest.raises(GraphValidationError):
        graph.create_relationship(user.entity_id, R.USES, doc.entity_id, p)
    assert graph.find_related_entities(user.entity_id) == []


def test_relationship_deduplication_merges_sources_into_one_edge(graph):
    user, py = graph.user_entity(), graph.create_entity(E.TECHNOLOGY, "Python")
    a = graph.create_relationship(user.entity_id, R.USES, py.entity_id, doc_prov("d1", "a.pdf", 1, "c1"))
    b = graph.create_relationship(user.entity_id, R.USES, py.entity_id, doc_prov("d2", "b.pdf", 3, "c7"))
    again = graph.create_relationship(user.entity_id, R.USES, py.entity_id, doc_prov("d2", "b.pdf", 3, "c7"))
    assert a.relationship_id == b.relationship_id == again.relationship_id
    assert len(graph.find_related_entities(user.entity_id)) == 1
    provenance = graph.get_provenance(a.relationship_id)
    assert {(p.source_name, p.page, p.chunk_id) for p in provenance} == {("a.pdf", 1, "c1"), ("b.pdf", 3, "c7")}


def test_relationship_confidence_and_trust_follow_best_active_source(graph):
    user, py = graph.user_entity(), graph.create_entity(E.TECHNOLOGY, "Python")
    graph.create_relationship(user.entity_id, R.USES, py.entity_id,
                              doc_prov("d1", "a.pdf", trust=TrustLevel.VERIFIED_SOURCE, confidence=Confidence.MEDIUM))
    rel = graph.create_relationship(user.entity_id, R.USES, py.entity_id, memory_prov("m1"))
    assert (rel.trust, rel.confidence) == (TrustLevel.EXPLICIT_USER, Confidence.HIGH)
    graph.remove_source(SourceKind.PERSONAL_MEMORY, "m1")
    weaker = graph.get_relationship(rel.relationship_id)
    assert (weaker.trust, weaker.confidence, weaker.status) == (TrustLevel.VERIFIED_SOURCE, Confidence.MEDIUM, GraphStatus.ACTIVE)


def test_inferred_relationship_is_low_and_distinguishable(graph):
    user, py = graph.user_entity(), graph.create_entity(E.TECHNOLOGY, "Python")
    rel = graph.create_relationship(user.entity_id, R.KNOWS, py.entity_id,
                                    memory_prov("m9", TrustLevel.INFERRED, Confidence.LOW))
    assert rel.trust is TrustLevel.INFERRED and rel.confidence is Confidence.LOW


# ---- source removal / deactivation ----

def test_removing_the_only_source_deactivates_the_relationship(graph, clock):
    user, java = graph.user_entity(), graph.create_entity(E.TECHNOLOGY, "Java")
    rel = graph.create_relationship(user.entity_id, R.PREFERS, java.entity_id, memory_prov("m1"))
    clock.advance(60)
    assert graph.remove_source(SourceKind.PERSONAL_MEMORY, "m1") == 1
    gone = graph.get_relationship(rel.relationship_id)
    assert gone.status is GraphStatus.INACTIVE and gone.valid_until == clock.now
    assert graph.find_related_entities(user.entity_id) == []
    assert graph.remove_source(SourceKind.PERSONAL_MEMORY, "m1") == 0  # idempotent


def test_a_relationship_with_another_valid_source_survives_source_removal(graph):
    user, py = graph.user_entity(), graph.create_entity(E.TECHNOLOGY, "Python")
    graph.create_relationship(user.entity_id, R.USES, py.entity_id, memory_prov("m1"))
    graph.create_relationship(user.entity_id, R.USES, py.entity_id, doc_prov("d1", "a.pdf"))
    assert graph.remove_source(SourceKind.PERSONAL_DOCUMENT, "d1") == 0
    [related] = graph.find_related_entities(user.entity_id)
    assert related.relationship.status is GraphStatus.ACTIVE
    assert sorted(p.active for p in graph.get_provenance(related.relationship.relationship_id)) == [False, True]


def test_reasserting_a_removed_fact_reactivates_it_without_a_duplicate(graph):
    user, java = graph.user_entity(), graph.create_entity(E.TECHNOLOGY, "Java")
    rel = graph.create_relationship(user.entity_id, R.PREFERS, java.entity_id, memory_prov("m1"))
    graph.remove_source(SourceKind.PERSONAL_MEMORY, "m1")
    again = graph.create_relationship(user.entity_id, R.PREFERS, java.entity_id, memory_prov("m2"))
    assert again.relationship_id == rel.relationship_id and again.status is GraphStatus.ACTIVE and again.valid_until is None


def test_explicit_deactivation_of_relationship_and_entity(graph):
    user, java, py = graph.user_entity(), graph.create_entity(E.TECHNOLOGY, "Java"), graph.create_entity(E.TECHNOLOGY, "Python")
    r1 = graph.create_relationship(user.entity_id, R.PREFERS, java.entity_id, memory_prov("m1"))
    graph.create_relationship(user.entity_id, R.PREFERS, py.entity_id, memory_prov("m2"))
    assert graph.deactivate_relationship(r1.relationship_id) is True and graph.deactivate_relationship(r1.relationship_id) is False
    assert [r.entity.canonical_name for r in graph.find_related_entities(user.entity_id)] == ["Python"]
    assert graph.deactivate_entity(py.entity_id) is True and graph.deactivate_entity(py.entity_id) is False
    assert graph.find_related_entities(user.entity_id) == []
    assert graph.search_entities("python") == []
    with pytest.raises(GraphValidationError):  # inactive entities cannot receive new facts
        graph.create_relationship(user.entity_id, R.PREFERS, py.entity_id, memory_prov("m3"))


def test_foreign_keys_are_enforced_by_the_database(session_factory):
    repo = GraphRepository(session_factory)
    from agent.knowledge_graph.models import Relationship

    if session_factory().bind.dialect.name == "sqlite":
        pass  # foreign_keys pragma is enabled by the shared fixture
    with pytest.raises(GraphStorageError) as exc:
        repo.add_relationship(Relationship(source_entity_id="a" * 32, relationship_type=R.USES, target_entity_id="b" * 32))
    assert "IntegrityError" in str(exc.value)


def test_multi_step_writes_are_atomic(graph):
    good = fact("User", E.PERSON, R.WORKS_ON, "JARVIS", E.PROJECT, memory_prov("m1"))
    bad = fact("User", E.PERSON, R.WORKS_ON, "Python", E.TECHNOLOGY, memory_prov("m2"))  # disallowed combination
    result = graph.apply_facts([good, bad])
    assert result.relationships_created == 1 and result.skipped == 1  # the invalid one is skipped, not half-written

    class Boom(Exception):
        pass

    repo = graph._repo
    with pytest.raises(Boom):
        with repo.unit_of_work():
            repo.add_entity(Entity(entity_type=E.TOPIC, canonical_name="Rolled Back", name_key="rolled back"))
            raise Boom
    assert graph.search_entities("rolled") == []


# ---- conflicts / temporal ----

def test_newer_explicit_fact_replaces_a_single_valued_relationship(graph, clock):
    user = graph.user_entity()
    blr, mum = graph.create_entity(E.LOCATION, "Bengaluru"), graph.create_entity(E.LOCATION, "Mumbai")
    old = graph.create_relationship(user.entity_id, R.LOCATED_IN, blr.entity_id, memory_prov("m1"))
    clock.advance(100)
    new = graph.create_relationship(user.entity_id, R.LOCATED_IN, mum.entity_id, memory_prov("m2"))
    assert [r.entity.canonical_name for r in graph.find_related_entities(user.entity_id)] == ["Mumbai"]
    kept = graph.get_relationship(old.relationship_id)  # preserved as history, not deleted
    assert kept.status is GraphStatus.INACTIVE and kept.valid_until == clock.now and kept.metadata["deactivated"]
    assert new.status is GraphStatus.ACTIVE


def test_lower_trust_fact_never_overrides_an_explicit_one(graph):
    user = graph.user_entity()
    blr, mum = graph.create_entity(E.LOCATION, "Bengaluru"), graph.create_entity(E.LOCATION, "Mumbai")
    graph.create_relationship(user.entity_id, R.LOCATED_IN, blr.entity_id, memory_prov("m1"))
    doc = graph.create_relationship(user.entity_id, R.LOCATED_IN, mum.entity_id, doc_prov("d1", "old_cv.pdf"))
    assert doc.status is GraphStatus.INACTIVE and doc.metadata["conflict"] is True
    assert [r.entity.canonical_name for r in graph.find_related_entities(user.entity_id)] == ["Bengaluru"]
    assert [p.source_name for p in graph.get_provenance(doc.relationship_id)] == ["old_cv.pdf"]  # provenance kept


def test_multi_valued_relationships_keep_both_facts_with_their_provenance(graph):
    jarvis = graph.create_entity(E.PROJECT, "JARVIS")
    fastapi, django = graph.create_entity(E.TECHNOLOGY, "FastAPI"), graph.create_entity(E.TECHNOLOGY, "Django")
    graph.create_relationship(jarvis.entity_id, R.USES, fastapi.entity_id, doc_prov("d1", "readme.md"))
    graph.create_relationship(jarvis.entity_id, R.USES, django.entity_id, doc_prov("d2", "old_notes.md"))
    names = {r.entity.canonical_name for r in graph.find_related_entities(jarvis.entity_id, R.USES)}
    assert names == {"FastAPI", "Django"}  # never guessed which is right


# ---- queries ----

@pytest.fixture
def seeded(graph):
    seed_example(graph)
    return graph


def names(related):
    return sorted(r.entity.canonical_name for r in related)


def test_direct_queries_by_relationship_and_entity_type(seeded):
    g = seeded
    jarvis = g.resolve_entity("JARVIS", E.PROJECT)
    assert names(g.find_related_entities(jarvis.entity_id, R.USES, direction="out")) == ["FastAPI", "Ollama", "PostgreSQL", "Python"]
    assert names(g.find_related_entities(jarvis.entity_id, entity_type=E.DOCUMENT)) == ["project_report.pdf"] * 2  # mentions (in) + documented_in (out)
    assert names(g.find_related_entities(jarvis.entity_id, direction="in")) == ["User", "project_report.pdf"]
    assert g.find_related_entities(jarvis.entity_id, R.KNOWS) == []
    with pytest.raises(GraphValidationError):
        g.find_related_entities(jarvis.entity_id, direction="sideways")


def test_which_projects_use_python(seeded):
    py = seeded.resolve_entity("Python", E.TECHNOLOGY)
    users = seeded.find_related_entities(py.entity_id, R.USES, E.PROJECT, direction="in")
    assert names(users) == ["JARVIS", "Virtual Campus"]


def test_query_results_are_ordered_limited_and_deterministic(seeded):
    jarvis = seeded.resolve_entity("JARVIS", E.PROJECT)
    first = [r.entity.entity_id for r in seeded.find_related_entities(jarvis.entity_id)]
    assert first == [r.entity.entity_id for r in seeded.find_related_entities(jarvis.entity_id)]
    assert len(seeded.find_related_entities(jarvis.entity_id, limit=2)) == 2


def test_entity_search_filters(seeded):
    assert [e.canonical_name for e in seeded.search_entities("fast")] == ["FastAPI"]
    assert names_of(seeded.search_entities(entity_types=[E.PROJECT])) == ["JARVIS", "Satellite Imaging", "Virtual Campus"]
    assert seeded.search_entities("zzz") == [] and seeded.search_entities("!!") == []


def names_of(entities):
    return sorted(e.canonical_name for e in entities)


def test_path_between_user_and_fastapi(seeded):
    user, fastapi = seeded.user_entity(), seeded.resolve_entity("FastAPI", E.TECHNOLOGY)
    [path] = seeded.find_paths(user.entity_id, fastapi.entity_id, max_depth=3)
    assert path.length == 2
    assert [s.relationship.relationship_type for s in path.steps] == [R.WORKS_ON, R.USES]
    assert path.entity_ids[0] == user.entity_id and path.entity_ids[-1] == fastapi.entity_id


def test_path_depth_limit_and_no_path(seeded):
    user, fastapi = seeded.user_entity(), seeded.resolve_entity("FastAPI", E.TECHNOLOGY)
    assert seeded.find_paths(user.entity_id, fastapi.entity_id, max_depth=1) == []
    sat = seeded.resolve_entity("Satellite Imaging", E.PROJECT)
    assert seeded.find_paths(user.entity_id, sat.entity_id, max_depth=6) == []  # different component
    assert seeded.find_paths(user.entity_id, user.entity_id) == []


def test_path_search_is_cycle_safe_deterministic_and_bounded(graph):
    a, b, c, d = (graph.create_entity(E.PROJECT, n) for n in ("Alpha", "Beta", "Gamma", "Delta"))
    p = memory_prov()
    for x, y in ((a, b), (b, c), (c, a), (c, d), (a, d)):  # a triangle plus exits: cycles everywhere
        graph.create_relationship(x.entity_id, R.RELATED_TO, y.entity_id, p)
    paths = graph.find_paths(a.entity_id, d.entity_id, max_depth=6)
    assert paths and all(len(set(pth.entity_ids)) == len(pth.entity_ids) for pth in paths)  # no repeated node
    assert [pth.length for pth in paths] == sorted(pth.length for pth in paths)  # shortest first
    assert paths == graph.find_paths(a.entity_id, d.entity_id, max_depth=6)
    assert paths[0].length == 1  # the direct edge
    assert len(graph.find_paths(a.entity_id, d.entity_id, max_depth=99)) == len(paths)  # depth is capped


def test_default_path_depth_comes_from_configuration(session_factory, clock):
    g = make_graph(session_factory, clock, max_path_depth=1)
    seed_example(g)
    assert g.find_paths(g.user_entity().entity_id, g.resolve_entity("FastAPI", E.TECHNOLOGY).entity_id) == []


def test_provenance_is_retrievable_per_relationship(seeded):
    jarvis = seeded.resolve_entity("JARVIS", E.PROJECT)
    fastapi_rel = next(r for r in seeded.find_related_entities(jarvis.entity_id, R.USES) if r.entity.canonical_name == "FastAPI")
    [p] = seeded.get_provenance(fastapi_rel.relationship.relationship_id)
    assert (p.source_kind, p.source_name, p.page, p.chunk_id) == (SourceKind.PERSONAL_DOCUMENT, "project_report.pdf", 1, "c1")


# ---- storage failure ----

class Broken:
    def __call__(self):
        from sqlalchemy.exc import OperationalError

        raise OperationalError("SELECT secret-doc-text", {}, Exception("down"))


def test_storage_failure_is_content_free_and_backs_off(clock):
    g = make_graph(Broken(), clock, backoff_seconds=30)
    with pytest.raises(GraphStorageError) as exc:
        g.create_entity(E.TOPIC, "anything")
    assert "secret-doc-text" not in str(exc.value)
    with pytest.raises(GraphStorageError, match="temporarily"):
        g.list_entities()  # within the back-off window the database is not touched
