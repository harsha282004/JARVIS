"""Controlled Knowledge Graph links for events (Phase 8 schema, extended by exactly two names).

Allowed, and only these:
    PROJECT | GOAL --HAS_DEADLINE--> EVENT     only to an EXISTING project/goal named by the user (never created here)
    EVENT --DOCUMENTED_IN--> DOCUMENT          for an event extracted from an indexed document
    PERSON --RELATED_TO--> EVENT               only to an EXISTING person entity named by the user
Every relationship keeps provenance and confidence from the event's own source. At most three links per event,
and no entity is created except the EVENT itself (and the DOCUMENT it came from), so the graph cannot balloon.
Tasks are not graph entities: a task is linked through `events.task_id`, not through the graph. Unconfirmed
(UNKNOWN) events get no links. Everything here is best-effort: a graph failure never affects the event.
The direction of DOCUMENTED_IN follows the Phase 8 schema (thing documented in a document), so it is
EVENT -> DOCUMENT rather than DOCUMENT -> EVENT.
"""

from dataclasses import dataclass, field

from agent.events.models import Event, EventStatus, SourceType
from agent.knowledge_graph.models import (
    Confidence as GraphConfidence,
    EntityType,
    Provenance,
    RelationshipType,
    SourceKind,
    TrustLevel,
)
from agent.knowledge_graph.service import GraphService
from backend.core.logging import get_logger

logger = get_logger(__name__)


@dataclass
class LinkResult:
    relationship_ids: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)  # e.g. "no project named X in the graph"


def _provenance(event: Event, document_name: str | None = None) -> Provenance | None:
    """Evidence for a relationship, from the event's own source. None when the source cannot be described honestly."""
    src = event.source
    confidence = GraphConfidence(int(event.confidence))
    if src.source_type in (SourceType.USER_EXPLICIT, SourceType.CONVERSATION):
        return Provenance(source_kind=SourceKind.EXPLICIT_USER_STATEMENT, trust=TrustLevel.EXPLICIT_USER, confidence=confidence)
    if src.source_type is SourceType.MEMORY and src.source_id:
        return Provenance(source_kind=SourceKind.PERSONAL_MEMORY, source_id=src.source_id, trust=TrustLevel.EXPLICIT_USER,
                          confidence=confidence)
    if src.source_type is SourceType.RAG_DOCUMENT and src.source_id and document_name:
        return Provenance(source_kind=SourceKind.PERSONAL_DOCUMENT, source_id=src.source_id, source_name=document_name,
                          trust=TrustLevel.VERIFIED_SOURCE, confidence=confidence)
    if src.source_type is SourceType.GMAIL:
        return Provenance(source_kind=SourceKind.IMPORTED_SOURCE, source_name="gmail", trust=TrustLevel.VERIFIED_SOURCE,
                          confidence=confidence)
    return None


class EventGraphLinker:
    def __init__(self, graph: GraphService):
        self._graph = graph

    def link(
        self, event: Event, *, project: str | None = None, person: str | None = None, document_name: str | None = None
    ) -> LinkResult:
        result = LinkResult()
        if event.status is EventStatus.UNKNOWN:
            result.notes.append("unconfirmed events are not added to the graph")
            return result
        prov = _provenance(event, document_name)
        if prov is None:
            return result
        try:
            entity = self._event_entity(event, prov)
            if event.source.source_type is SourceType.RAG_DOCUMENT and document_name:
                doc = self._graph.create_entity(EntityType.DOCUMENT, document_name, trust=prov.trust,
                                                confidence=prov.confidence)
                self._add(result, entity.entity_id, RelationshipType.DOCUMENTED_IN, doc.entity_id, prov)
            if project:
                found = self._graph.resolve_entity(project, EntityType.PROJECT) or self._graph.resolve_entity(project, EntityType.GOAL)
                if found is None:
                    result.notes.append("no such project or goal in the knowledge graph")
                else:
                    self._add(result, found.entity_id, RelationshipType.HAS_DEADLINE, entity.entity_id, prov)
            if person:
                found = self._graph.resolve_entity(person, EntityType.PERSON)
                if found is None:
                    result.notes.append("no such person in the knowledge graph")
                else:
                    self._add(result, found.entity_id, RelationshipType.RELATED_TO, entity.entity_id, prov)
        except Exception as exc:  # noqa: BLE001 - the graph is optional; log the type only
            logger.warning("Event graph link failed (%s)", type(exc).__name__)
        return result

    def unlink(self, relationship_ids: list[str]) -> int:
        """Deactivate the relationships of a cancelled event. Returns how many were deactivated."""
        count = 0
        for rid in relationship_ids:
            try:
                count += int(bool(self._graph.deactivate_relationship(rid)))
            except Exception as exc:  # noqa: BLE001
                logger.warning("Event graph unlink failed (%s)", type(exc).__name__)
        return count

    def _event_entity(self, event: Event, prov: Provenance):
        day = event.anchor.astimezone(event.zone).date().isoformat()
        name = f"{event.title[:90]} ({day})"  # the date keeps two "Interview" events apart
        return self._graph.create_entity(EntityType.EVENT, name, metadata={"event_id": event.event_id},
                                         confidence=prov.confidence, trust=prov.trust)

    def _add(self, result: LinkResult, source_id: str, rel: RelationshipType, target_id: str, prov: Provenance) -> None:
        relationship = self._graph.create_relationship(source_id, rel, target_id, prov)  # the service stores a fresh provenance row
        result.relationship_ids.append(relationship.relationship_id)
