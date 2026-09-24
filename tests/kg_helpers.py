"""Shared helpers for knowledge-graph tests."""

from datetime import datetime, timedelta, timezone

from agent.knowledge_graph.models import (
    Confidence,
    EntityRef,
    EntityType as E,
    Provenance,
    RelationshipFact,
    RelationshipType as R,
    SourceKind,
    TrustLevel,
)
from agent.knowledge_graph.repository import GraphRepository
from agent.knowledge_graph.service import GraphService


class Clock:
    def __init__(self):
        self.now = datetime(2030, 1, 1, tzinfo=timezone.utc)

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += timedelta(seconds=seconds)


def make_graph(session_factory, clock=None, **kw):
    return GraphService(GraphRepository(session_factory), clock=clock or Clock(), **kw)


def memory_prov(memory_id="m1", trust=TrustLevel.EXPLICIT_USER, confidence=Confidence.HIGH):
    return Provenance(source_kind=SourceKind.PERSONAL_MEMORY, source_id=memory_id, trust=trust, confidence=confidence)


def doc_prov(doc_id="d1", name="report.pdf", page=1, chunk_id="c1", trust=TrustLevel.VERIFIED_SOURCE, confidence=Confidence.HIGH):
    return Provenance(source_kind=SourceKind.PERSONAL_DOCUMENT, source_id=doc_id, source_name=name, page=page,
                      chunk_id=chunk_id, trust=trust, confidence=confidence)


def fact(src, stype, rel, tgt, ttype, prov):
    return RelationshipFact(source=EntityRef(name=src, entity_type=stype), relationship_type=rel,
                            target=EntityRef(name=tgt, entity_type=ttype), provenance=prov)


def seed_example(graph):
    """User works_on JARVIS; JARVIS uses FastAPI/PostgreSQL/Ollama; user prefers Java, studies CS;
    JARVIS documented in project_report.pdf; a satellite project mentioned in notes.txt."""
    mp = lambda i: memory_prov(i)  # noqa: E731
    dp = doc_prov("d1", "project_report.pdf")
    graph.apply_facts([
        fact("User", E.PERSON, R.WORKS_ON, "JARVIS", E.PROJECT, mp("m1")),
        fact("User", E.PERSON, R.WORKS_ON, "Virtual Campus", E.PROJECT, mp("m2")),
        fact("User", E.PERSON, R.PREFERS, "Java", E.TECHNOLOGY, mp("m3")),
        fact("User", E.PERSON, R.STUDIES, "Computer Science", E.TOPIC, mp("m4")),
        fact("JARVIS", E.PROJECT, R.USES, "FastAPI", E.TECHNOLOGY, dp),
        fact("JARVIS", E.PROJECT, R.USES, "PostgreSQL", E.TECHNOLOGY, dp),
        fact("JARVIS", E.PROJECT, R.USES, "Ollama", E.TECHNOLOGY, dp),
        fact("Virtual Campus", E.PROJECT, R.USES, "Python", E.TECHNOLOGY, doc_prov("d2", "campus.md", None, "c9")),
        fact("JARVIS", E.PROJECT, R.USES, "Python", E.TECHNOLOGY, dp),
        fact("project_report.pdf", E.DOCUMENT, R.MENTIONS, "JARVIS", E.PROJECT, dp),
        fact("JARVIS", E.PROJECT, R.DOCUMENTED_IN, "project_report.pdf", E.DOCUMENT, dp),
        fact("satellite notes.txt", E.DOCUMENT, R.MENTIONS, "Satellite Imaging", E.PROJECT, doc_prov("d3", "satellite notes.txt")),
    ])
