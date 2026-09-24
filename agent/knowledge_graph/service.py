"""GraphService: the graph rules. The only writer of the knowledge graph.

    KnowledgeGraphInterface -> GraphService (rules) -> GraphRepository -> PostgreSQL

Every mutation is validated here (entity/relationship types, allowed
combinations, provenance, trust) before anything is written, and multi-step
writes run in one transaction. Model output never reaches this class except
as already-validated `RelationshipFact`s (see extraction.py); nothing here
runs SQL supplied by a caller.

Rules in short:
- Identity: (entity_type, deterministic name key). Ambiguous names are never merged.
- One active relationship per (source, type, target); further sources add
  provenance. Confidence/trust = best of its active provenance.
- A relationship is active only while it has active provenance, so deleting a
  memory or document invalidates exactly the facts that depended on it alone.
- Single-valued relationships (see rules.SINGLE_VALUED): a newer fact of equal
  or higher trust replaces the current one; a lower-trust fact is recorded
  inactive (flagged conflict) and never overrides.
"""

from collections.abc import Callable, Sequence
from datetime import datetime, timedelta
from typing import Any

from agent.knowledge_graph.base import KnowledgeGraphInterface
from agent.knowledge_graph.models import (
    ApplyResult,
    Confidence,
    Entity,
    EntityRef,
    EntityType,
    GraphFact,
    GraphPath,
    GraphStatus,
    GraphStorageError,
    GraphValidationError,
    PathStep,
    Provenance,
    RelatedEntity,
    Relationship,
    RelationshipFact,
    RelationshipType,
    SourceKind,
    TrustLevel,
    new_id,
    utcnow,
)
from agent.knowledge_graph.normalize import USER_NAME, fold_text, name_key
from agent.knowledge_graph.repository import GraphRepository
from agent.knowledge_graph.rules import SINGLE_VALUED, is_allowed
from backend.core.logging import get_logger

logger = get_logger(__name__)

HARD_MAX_DEPTH = 6
MAX_EXPANSIONS = 2000  # node expansions per path search: bounded work on any graph
MAX_ALIASES = 10
ENTITY_SCAN_LIMIT = 5000
BACKOFF_SECONDS = 30.0


class GraphService(KnowledgeGraphInterface):
    def __init__(
        self,
        repository: GraphRepository,
        min_confidence: Confidence = Confidence.MEDIUM,
        max_path_depth: int = 3,
        max_results: int = 20,
        clock: Callable[[], datetime] = utcnow,
        backoff_seconds: float = BACKOFF_SECONDS,
    ):
        self._repo = repository
        self.min_confidence = min_confidence
        self._max_depth = max(1, min(max_path_depth, HARD_MAX_DEPTH))
        self.max_results = max_results
        self._clock = clock
        self._backoff = timedelta(seconds=backoff_seconds)
        self._retry_after: datetime | None = None

    def _db(self, work: Callable[[], Any]):
        """Run repository work; after a database failure skip the database briefly so an
        unavailable graph does not slow every conversation turn."""
        now = self._clock()
        if self._retry_after is not None and now < self._retry_after:
            raise GraphStorageError("Graph database temporarily unavailable")
        try:
            result = work()
        except GraphStorageError:
            self._retry_after = now + self._backoff
            raise
        self._retry_after = None
        return result

    # ---- entities ------------------------------------------------------------

    def create_entity(
        self, entity_type: EntityType, name: str, description: str | None = None,
        metadata: dict[str, Any] | None = None, confidence: Confidence = Confidence.HIGH,
        trust: TrustLevel = TrustLevel.EXPLICIT_USER,
    ) -> Entity:
        return self._db(lambda: self._ensure_entity(entity_type, name, description, metadata, confidence, trust)[0])

    def user_entity(self) -> Entity:
        return self.create_entity(EntityType.PERSON, USER_NAME)

    def get_entity(self, entity_id: str) -> Entity | None:
        return self._db(lambda: self._repo.get_entity(entity_id))

    def resolve_entity(self, name: str, entity_type: EntityType | None = None) -> Entity | None:
        def work() -> Entity | None:
            types = [entity_type] if entity_type else list(EntityType)
            matches: dict[str, Entity] = {}
            for t in types:
                key = name_key(name, t)
                if key:
                    for e in self._repo.find_entities(t, key):
                        matches[e.entity_id] = e
            return next(iter(matches.values())) if len(matches) == 1 else None  # ambiguous or unknown: None

        return self._db(work)

    def list_entities(self, limit: int = ENTITY_SCAN_LIMIT) -> list[Entity]:
        return self._db(lambda: self._repo.find_entities(limit=limit))

    def search_entities(
        self, text: str | None = None, entity_types: Sequence[EntityType] | None = None, limit: int = 20,
    ) -> list[Entity]:
        keys = [k for k in fold_text(text).split() if len(k) >= 2] if text else None
        if text and not keys:
            return []
        return self._db(lambda: self._repo.search_entities(keys, entity_types, limit))

    def deactivate_entity(self, entity_id: str) -> bool:
        def work() -> bool:
            with self._repo.unit_of_work():
                entity = self._repo.get_entity(entity_id)
                if entity is None or entity.status is GraphStatus.INACTIVE:
                    return False
                for rel in self._repo.find_relationships(involving=entity_id):
                    self._deactivate(rel)
                self._repo.save_entity(entity.model_copy(update={"status": GraphStatus.INACTIVE, "updated_at": self._clock()}))
            logger.info("Graph entity deactivated (id=%s, type=%s)", entity_id, entity.entity_type.value)
            return True

        return self._db(work)

    # ---- relationships ----------------------------------------------------------

    def create_relationship(
        self, source_entity_id: str, relationship_type: RelationshipType, target_entity_id: str,
        provenance: Provenance,
    ) -> Relationship:
        def work() -> Relationship:
            with self._repo.unit_of_work():
                return self._add_relationship(source_entity_id, relationship_type, target_entity_id, provenance)[0]

        return self._db(work)

    def get_relationship(self, relationship_id: str) -> Relationship | None:
        return self._db(lambda: self._repo.get_relationship(relationship_id))

    def get_provenance(self, relationship_id: str, active_only: bool = False) -> list[Provenance]:
        return self._db(lambda: self._repo.list_provenance(relationship_id, active_only))

    def deactivate_relationship(self, relationship_id: str) -> bool:
        def work() -> bool:
            with self._repo.unit_of_work():
                rel = self._repo.get_relationship(relationship_id)
                if rel is None or rel.status is GraphStatus.INACTIVE:
                    return False
                self._deactivate(rel)
            logger.info("Graph relationship deactivated (id=%s)", relationship_id)
            return True

        return self._db(work)

    def remove_source(self, source_kind: SourceKind, source_id: str) -> int:
        """Invalidate everything a source (memory or document) contributed. A relationship
        with another active source survives; one with none becomes inactive. Returns the
        number of relationships deactivated."""

        def work() -> int:
            deactivated = 0
            with self._repo.unit_of_work():
                rows = self._repo.find_provenance_by_source(source_kind, source_id)
                for _, p in rows:
                    self._repo.set_provenance_active(p.provenance_id, False)
                for rel_id in dict.fromkeys(rid for rid, _ in rows):
                    rel = self._repo.get_relationship(rel_id)
                    if rel is not None and self._recompute(rel).status is GraphStatus.INACTIVE and rel.status is GraphStatus.ACTIVE:
                        deactivated += 1
            if rows:
                logger.info("Graph source removed (kind=%s, source=%s, relationships_deactivated=%d)",
                            source_kind.value, source_id, deactivated)
            return deactivated

        return self._db(work)

    def replace_source(self, source_kind: SourceKind, source_id: str, facts: Sequence[RelationshipFact]) -> ApplyResult:
        """Atomically swap what a source contributes: invalidate its old facts, then apply the new
        ones. If anything fails, the graph is unchanged."""

        def work() -> ApplyResult:
            with self._repo.unit_of_work():
                self.remove_source(source_kind, source_id)
                return self.apply_facts(facts)

        return self._db(work)

    def apply_facts(self, facts: Sequence[RelationshipFact]) -> ApplyResult:
        """Write already-validated facts atomically (all or nothing)."""

        def work() -> ApplyResult:
            result = ApplyResult()
            with self._repo.unit_of_work():
                for fact in facts:
                    p = fact.provenance
                    src, created_s = self._ensure_entity(fact.source.entity_type, fact.source.name, fact.source.description, None, p.confidence, p.trust)
                    tgt, created_t = self._ensure_entity(fact.target.entity_type, fact.target.name, fact.target.description, None, p.confidence, p.trust)
                    result.entities_created += created_s + created_t
                    try:
                        _, outcome = self._add_relationship(src.entity_id, fact.relationship_type, tgt.entity_id, p)
                    except GraphValidationError:
                        result.skipped += 1
                        continue
                    if outcome == "created":
                        result.relationships_created += 1
                    elif outcome == "updated":
                        result.relationships_updated += 1
                    else:
                        result.relationships_conflicted += 1
            logger.info(
                "Graph facts applied (entities_created=%d, relationships_created=%d, updated=%d, conflicts=%d, skipped=%d)",
                result.entities_created, result.relationships_created, result.relationships_updated,
                result.relationships_conflicted, result.skipped,
            )
            return result

        return self._db(work)

    # ---- queries -----------------------------------------------------------------

    def find_related_entities(
        self, entity_id: str, relationship_type: RelationshipType | None = None,
        entity_type: EntityType | None = None, direction: str = "both", limit: int | None = None,
    ) -> list[RelatedEntity]:
        if direction not in ("out", "in", "both"):
            raise GraphValidationError("direction must be 'out', 'in' or 'both'")

        def work() -> list[RelatedEntity]:
            found: list[RelatedEntity] = []
            for rel in self._repo.find_relationships(involving=entity_id, relationship_type=relationship_type):
                outgoing = rel.source_entity_id == entity_id
                if direction == "out" and not outgoing or direction == "in" and outgoing:
                    continue
                other = self._repo.get_entity(rel.target_entity_id if outgoing else rel.source_entity_id)
                if other is None or other.status is not GraphStatus.ACTIVE:
                    continue
                if entity_type is not None and other.entity_type is not entity_type:
                    continue
                found.append(RelatedEntity(entity=other, relationship=rel, direction="out" if outgoing else "in"))
            found.sort(key=lambda r: (-int(r.relationship.trust), -int(r.relationship.confidence),
                                      r.entity.canonical_name.casefold(), r.relationship.relationship_id))
            return found[: limit or self.max_results]

        return self._db(work)

    def find_paths(self, source_id: str, target_id: str, max_depth: int | None = None) -> list[GraphPath]:
        """Simple paths (no repeated entity) from source to target over active relationships in
        either direction, up to `max_depth` steps. Deterministic order: shortest first, then by
        relationship ids. Bounded: depth is capped and so is the work done."""
        depth = max(1, min(max_depth or self._max_depth, HARD_MAX_DEPTH))

        def work() -> list[GraphPath]:
            if source_id == target_id:
                return []
            adjacency: dict[str, list[tuple[Relationship, str]]] = {}

            def neighbours(entity_id: str) -> list[tuple[Relationship, str]]:
                if entity_id not in adjacency:
                    edges = []
                    for rel in self._repo.find_relationships(involving=entity_id):
                        other = rel.target_entity_id if rel.source_entity_id == entity_id else rel.source_entity_id
                        edges.append((rel, other))
                    adjacency[entity_id] = sorted(edges, key=lambda e: e[0].relationship_id)
                return adjacency[entity_id]

            paths: list[GraphPath] = []
            expansions = 0
            stack: list[tuple[str, list[str], list[PathStep]]] = [(source_id, [source_id], [])]
            while stack and expansions < MAX_EXPANSIONS:
                node, visited, steps = stack.pop()
                expansions += 1
                if len(steps) >= depth:
                    continue
                for rel, other in reversed(neighbours(node)):
                    if other in visited:  # cycle protection
                        continue
                    step = PathStep(relationship=rel, from_entity_id=node, to_entity_id=other)
                    if other == target_id:
                        paths.append(GraphPath(entity_ids=[*visited, other], steps=[*steps, step]))
                    else:
                        stack.append((other, [*visited, other], [*steps, step]))
            paths.sort(key=lambda p: (p.length, [s.relationship.relationship_id for s in p.steps]))
            return paths[: self.max_results]

        return self._db(work)

    def facts_about(self, entity_ids: Sequence[str], entity_type: EntityType | None = None,
                    limit: int | None = None) -> list[GraphFact]:
        """Readable one-hop facts around the given entities (both directions), best trust first."""

        def work() -> list[GraphFact]:
            entities: dict[str, Entity] = {}

            def entity(eid: str) -> Entity | None:
                if eid not in entities:
                    entities[eid] = self._repo.get_entity(eid)
                return entities[eid]

            seen: dict[str, GraphFact] = {}
            for eid in entity_ids:
                for rel in self._repo.find_relationships(involving=eid):
                    if rel.confidence < self.min_confidence:
                        continue
                    src, tgt = entity(rel.source_entity_id), entity(rel.target_entity_id)
                    if src is None or tgt is None or GraphStatus.INACTIVE in (src.status, tgt.status):
                        continue
                    other = tgt if rel.source_entity_id == eid else src
                    if entity_type is not None and other.entity_type is not entity_type:
                        continue
                    seen.setdefault(rel.relationship_id, GraphFact(
                        source_name=src.canonical_name, source_type=src.entity_type,
                        relationship_type=rel.relationship_type, target_name=tgt.canonical_name,
                        target_type=tgt.entity_type, trust=rel.trust, confidence=rel.confidence,
                        sources=self._source_labels(rel.relationship_id),
                    ))
            ordered = sorted(seen.values(), key=lambda f: (-int(f.trust), -int(f.confidence), f.source_name.casefold(),
                                                            f.relationship_type.value, f.target_name.casefold()))
            return ordered[: limit or self.max_results]

        return self._db(work)

    def fact_for_relationship(self, rel: Relationship) -> GraphFact | None:
        src, tgt = self._repo.get_entity(rel.source_entity_id), self._repo.get_entity(rel.target_entity_id)
        if src is None or tgt is None:
            return None
        return GraphFact(
            source_name=src.canonical_name, source_type=src.entity_type, relationship_type=rel.relationship_type,
            target_name=tgt.canonical_name, target_type=tgt.entity_type, trust=rel.trust,
            confidence=rel.confidence, sources=self._source_labels(rel.relationship_id),
        )

    def stats(self) -> dict[str, int]:
        return self._db(self._repo.count)

    # ---- internals ---------------------------------------------------------------

    def _source_labels(self, relationship_id: str) -> list[str]:
        labels = []
        for p in self._repo.list_provenance(relationship_id, active_only=True):
            label = p.source_name if p.source_kind is SourceKind.PERSONAL_DOCUMENT else p.source_kind.value
            if label and label not in labels:
                labels.append(label)
        return labels[:5]

    def _ensure_entity(
        self, entity_type: EntityType, name: str, description: str | None, metadata: dict[str, Any] | None,
        confidence: Confidence, trust: TrustLevel,
    ) -> tuple[Entity, int]:
        try:
            key = name_key(name, entity_type)
            if not key:
                raise ValueError("empty name")
            candidate = Entity(entity_type=entity_type, canonical_name=name, name_key=key, description=description,
                               metadata=dict(metadata or {}), confidence=confidence, trust=trust,
                               created_at=self._clock(), updated_at=self._clock())
        except ValueError:
            raise GraphValidationError("Invalid entity name") from None
        with self._repo.unit_of_work():
            existing = self._repo.find_entities(entity_type, key)
            if not existing:
                self._repo.add_entity(candidate)
                logger.info("Graph entity created (id=%s, type=%s)", candidate.entity_id, entity_type.value)
                return candidate, 1
            entity = existing[0]
            changes: dict[str, Any] = {}
            aliases = list(entity.metadata.get("aliases", []))
            if " ".join(name.split()) != entity.canonical_name and name not in aliases and len(aliases) < MAX_ALIASES:
                changes["metadata"] = {**entity.metadata, "aliases": [*aliases, " ".join(name.split())]}
            if description and not entity.description:
                changes["description"] = candidate.description
            if int(trust) > int(entity.trust):
                changes["trust"] = trust
            if int(confidence) > int(entity.confidence):
                changes["confidence"] = confidence
            if changes:
                entity = entity.model_copy(update={**changes, "updated_at": self._clock()})
                self._repo.save_entity(entity)
            logger.info("Graph entity resolved (id=%s, type=%s)", entity.entity_id, entity_type.value)
            return entity, 0

    def _add_relationship(
        self, source_id: str, rtype: RelationshipType, target_id: str, provenance: Provenance,
    ) -> tuple[Relationship, str]:
        src, tgt = self._repo.get_entity(source_id), self._repo.get_entity(target_id)
        if src is None or tgt is None or GraphStatus.INACTIVE in (src.status, tgt.status):
            raise GraphValidationError("Both entities must exist and be active")
        if src.entity_id == tgt.entity_id:
            raise GraphValidationError("A relationship cannot connect an entity to itself")
        if not is_allowed(src.entity_type, rtype, tgt.entity_type):
            raise GraphValidationError(
                f"{src.entity_type.value} --{rtype.value}--> {tgt.entity_type.value} is not an allowed relationship"
            )
        now = self._clock()
        conflicted = False
        if (src.entity_type, rtype) in SINGLE_VALUED:
            others = [r for r in self._repo.find_relationships(source_id=src.entity_id, relationship_type=rtype)
                      if r.target_entity_id != tgt.entity_id]
            if others:
                if int(provenance.trust) < max(int(o.trust) for o in others):
                    conflicted = True  # a lower-trust fact never overrides
                else:
                    for other in others:
                        self._deactivate(other, superseded_by=tgt.entity_id)

        existing = self._repo.find_relationships(source_id=src.entity_id, target_id=tgt.entity_id,
                                                 relationship_type=rtype, status=None)
        if existing:
            rel = existing[0]
            outcome = "updated"
        else:
            rel = Relationship(source_entity_id=src.entity_id, relationship_type=rtype, target_entity_id=tgt.entity_id,
                               confidence=provenance.confidence, trust=provenance.trust, valid_from=now,
                               created_at=now, updated_at=now,
                               metadata={"conflict": True} if conflicted else {})
            self._repo.add_relationship(rel)
            outcome = "created"
            logger.info("Graph relationship created (id=%s, type=%s)", rel.relationship_id, rtype.value)
        if conflicted:
            rel = rel.model_copy(update={"status": GraphStatus.INACTIVE, "valid_until": now,
                                         "metadata": {**rel.metadata, "conflict": True}, "updated_at": now})
            self._repo.save_relationship(rel)
            outcome = "conflict"
        elif rel.metadata.get("deactivated") or rel.metadata.get("conflict"):
            # An explicitly deactivated relationship comes back only when a new fact re-asserts it.
            rel = rel.model_copy(update={"metadata": {k: v for k, v in rel.metadata.items() if k not in ("deactivated", "conflict")}})

        for p in self._repo.list_provenance(rel.relationship_id):
            if (p.source_kind, p.source_id, p.chunk_id) == (provenance.source_kind, provenance.source_id, provenance.chunk_id):
                if not p.active:
                    self._repo.set_provenance_active(p.provenance_id, True)
                break
        else:
            self._repo.add_provenance(rel.relationship_id, provenance.model_copy(update={"created_at": now, "provenance_id": new_id()}))
        return (self._recompute(rel) if not conflicted else rel), outcome

    def _recompute(self, rel: Relationship) -> Relationship:
        """Re-derive confidence/trust/status from the active provenance."""
        now = self._clock()
        active = self._repo.list_provenance(rel.relationship_id, active_only=True)
        if not active:
            updated = rel.model_copy(update={"status": GraphStatus.INACTIVE, "valid_until": rel.valid_until or now, "updated_at": now})
        else:
            blocked = rel.metadata.get("conflict") or rel.metadata.get("deactivated")
            updated = rel.model_copy(update={
                "confidence": max((p.confidence for p in active), key=int),
                "trust": max((p.trust for p in active), key=int),
                "status": GraphStatus.INACTIVE if blocked else GraphStatus.ACTIVE,
                "valid_until": rel.valid_until if blocked else None,
                "valid_from": rel.valid_from if rel.status is GraphStatus.ACTIVE else now,
                "updated_at": now,
            })
        self._repo.save_relationship(updated)
        return updated

    def _deactivate(self, rel: Relationship, superseded_by: str | None = None) -> None:
        now = self._clock()
        meta = {**rel.metadata, "deactivated": True}
        if superseded_by:
            meta["superseded_by_target"] = superseded_by
        for p in self._repo.list_provenance(rel.relationship_id, active_only=True):
            self._repo.set_provenance_active(p.provenance_id, False)
        self._repo.save_relationship(rel.model_copy(update={
            "status": GraphStatus.INACTIVE, "valid_until": rel.valid_until or now, "metadata": meta, "updated_at": now,
        }))
