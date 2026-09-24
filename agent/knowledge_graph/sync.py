"""Feeding the graph from Phase 6 memory and Phase 7 documents (and keeping it honest).

- Memory -> graph is deterministic (no LLM): a memory's templated content
  ("User prefers Java.") maps to at most one fact whose provenance is that
  memory's id. When the memory is deleted, superseded or purged, exactly that
  provenance is invalidated.
- Document -> graph is LLM-assisted and explicit (`extract_document`, e.g. from
  scripts/kg_cli.py), never automatic and never one node per sentence. Every
  fact keeps document id, filename, page and chunk id. When the document is
  deleted or re-indexed its provenance is invalidated.
Memory stays the authority for structured memories and the RAG index for
documents; the graph only holds derived, provenance-tagged relationships.
"""

import re
from dataclasses import dataclass

from agent.knowledge_graph.extraction import GraphExtractor, SourceInfo
from agent.knowledge_graph.models import (
    Confidence,
    EntityRef,
    EntityType,
    GraphExtractionError,
    Provenance,
    RelationshipFact,
    RelationshipType,
    SourceKind,
    TrustLevel,
)
from agent.knowledge_graph.normalize import USER_NAME, fold_text, name_key
from agent.knowledge_graph.rules import KNOWN_TECHNOLOGIES
from agent.knowledge_graph.service import GraphService
from agent.memory.models import Memory, MemoryBasis, MemoryEvent, MemoryEventKind, MemoryStatus, MemoryType
from agent.rag.models import DocumentEvent, DocumentEventKind, DocumentStatus
from agent.rag.service import RagService
from backend.core.logging import get_logger

logger = get_logger(__name__)

_USER = EntityRef(name=USER_NAME, entity_type=EntityType.PERSON)
_TECH_SLOT = re.compile(r"\b(language|framework|library|database|editor|ide|tool|stack)\b", re.IGNORECASE)
_MAX_VALUE_CHARS = 100

_PATTERNS: list[tuple[MemoryType, re.Pattern[str], RelationshipType, EntityType | None]] = [
    (MemoryType.PREFERENCE, re.compile(r"^User's favorite (?P<slot>.+?) is (?P<v>.+?)\.?$"), RelationshipType.PREFERS, None),
    (MemoryType.PREFERENCE, re.compile(r"^User prefers (?P<v>.+?)(?: for .+)?\.?$"), RelationshipType.PREFERS, None),
    (MemoryType.PREFERENCE, re.compile(r"^User (?:likes|loves|enjoys|adores) (?P<v>.+?)\.?$"), RelationshipType.PREFERS, None),
    (MemoryType.GOAL, re.compile(r"^User is preparing for (?P<v>.+?)\.?$"), RelationshipType.HAS_GOAL, EntityType.GOAL),
    (MemoryType.GOAL, re.compile(r"^User wants to (?P<v>.+?)\.?$"), RelationshipType.HAS_GOAL, EntityType.GOAL),
    (MemoryType.GOAL, re.compile(r"^User's goal is to (?P<v>.+?)\.?$"), RelationshipType.HAS_GOAL, EntityType.GOAL),
    (MemoryType.CONTEXT, re.compile(r"^User is currently working on (?P<v>.+?)\.?$"), RelationshipType.WORKS_ON, EntityType.PROJECT),
    (MemoryType.FACT, re.compile(r"^User studies (?P<v>.+?)\.?$"), RelationshipType.STUDIES, EntityType.TOPIC),
    (MemoryType.FACT, re.compile(r"^User works at (?P<v>.+?)\.?$"), RelationshipType.WORKS_AT, EntityType.ORGANIZATION),
    (MemoryType.FACT, re.compile(r"^User lives in (?P<v>.+?)\.?$"), RelationshipType.LOCATED_IN, EntityType.LOCATION),
]


def _target_type(value: str, slot: str | None) -> EntityType:
    if name_key(value, EntityType.TECHNOLOGY) in KNOWN_TECHNOLOGIES or (slot and _TECH_SLOT.search(slot)):
        return EntityType.TECHNOLOGY
    return EntityType.TOPIC


def memory_to_facts(memory: Memory) -> list[RelationshipFact]:
    """The graph fact a memory expresses, if its (templated) content maps to one. Only ACTIVE
    memories produce facts; unmappable ones (profile, dislikes, ...) produce none."""
    if memory.status is not MemoryStatus.ACTIVE:
        return []
    for mtype, pattern, rtype, target_type in _PATTERNS:
        match = pattern.match(memory.content)
        if memory.type is not mtype or not match:
            continue
        value = " ".join(match["v"].split())[:_MAX_VALUE_CHARS].strip(" .")
        if not value:
            return []
        explicit = memory.basis is MemoryBasis.EXPLICIT
        try:
            provenance = Provenance(
                source_kind=SourceKind.PERSONAL_MEMORY, source_id=memory.memory_id,
                confidence=memory.confidence if explicit else Confidence.LOW,
                trust=TrustLevel.EXPLICIT_USER if explicit else TrustLevel.INFERRED,
            )
            slot = match.groupdict().get("slot")
            target = EntityRef(name=value, entity_type=target_type or _target_type(value, slot))
            return [RelationshipFact(source=_USER, relationship_type=rtype, target=target, provenance=provenance)]
        except ValueError:
            return []
    return []


class MemoryGraphSync:
    """Keeps memory-derived graph facts in step with the memory system (a MemoryService listener)."""

    def __init__(self, graph: GraphService):
        self._graph = graph

    def handle(self, event: MemoryEvent) -> None:
        memory = event.memory
        if event.kind in (MemoryEventKind.STORED, MemoryEventKind.UPDATED):
            self._graph.replace_source(SourceKind.PERSONAL_MEMORY, memory.memory_id, memory_to_facts(memory))
        else:  # DELETED, SUPERSEDED, PURGED: the derived facts must not stay active
            self._graph.remove_source(SourceKind.PERSONAL_MEMORY, memory.memory_id)

    def sync_all(self, memory_service, limit: int = 5000) -> int:
        """Reconcile the graph with every active memory. Returns how many produced facts."""
        count = 0
        for memory in memory_service.search(limit=limit):
            facts = memory_to_facts(memory)
            self._graph.replace_source(SourceKind.PERSONAL_MEMORY, memory.memory_id, facts)
            count += bool(facts)
        return count


class DocumentGraphSync:
    """Invalidates document-derived facts when a document leaves or changes in the RAG index
    (a RagService listener). It never extracts by itself."""

    def __init__(self, graph: GraphService):
        self._graph = graph

    def handle(self, event: DocumentEvent) -> None:
        if event.kind in (DocumentEventKind.DELETED, DocumentEventKind.REINDEXED):
            self._graph.remove_source(SourceKind.PERSONAL_DOCUMENT, event.document.document_id)


@dataclass
class DocumentExtractionReport:
    chunks_processed: int = 0
    chunks_failed: int = 0
    entities: int = 0
    relationships: int = 0
    rejected: int = 0


class DocumentGraphIngestor:
    """LLM-assisted extraction of graph facts from an indexed document's chunks."""

    def __init__(self, graph: GraphService, rag: RagService, extractor: GraphExtractor, max_chunks: int = 40):
        self._graph = graph
        self._rag = rag
        self._extractor = extractor
        self._max_chunks = max_chunks

    def extract_document(self, document_id: str) -> DocumentExtractionReport:
        """Extract, validate, then replace this document's facts atomically. On any failure the
        graph is left as it was. Raises GraphExtractionError if nothing could be extracted."""
        doc = self._rag.get_document(document_id)
        if doc is None or doc.status is not DocumentStatus.INDEXED:
            raise GraphExtractionError("Document is not indexed")
        chunks = self._rag.get_chunks(document_id)[: self._max_chunks]
        report = DocumentExtractionReport()
        facts: list[RelationshipFact] = []
        doc_ref = EntityRef(name=doc.filename, entity_type=EntityType.DOCUMENT)
        seen: set[tuple[str, str, str, str]] = set()

        def add(fact: RelationshipFact) -> None:
            key = (fold_text(fact.source.name), fact.relationship_type.value, fold_text(fact.target.name), fact.provenance.chunk_id or "")
            if key not in seen:
                seen.add(key)
                facts.append(fact)

        for chunk in chunks:
            source = SourceInfo(SourceKind.PERSONAL_DOCUMENT, doc.document_id, doc.filename, chunk.page, chunk.chunk_id)
            try:
                result = self._extractor.extract(chunk.text, source)
            except GraphExtractionError:
                report.chunks_failed += 1
                continue
            report.chunks_processed += 1
            report.rejected += result.rejected
            mention = Provenance(
                source_kind=SourceKind.PERSONAL_DOCUMENT, source_id=doc.document_id, source_name=doc.filename,
                page=chunk.page, chunk_id=chunk.chunk_id, confidence=Confidence.HIGH, trust=TrustLevel.VERIFIED_SOURCE,
            )
            for entity in result.entities:  # name found verbatim in this chunk
                add(RelationshipFact(source=doc_ref, relationship_type=RelationshipType.MENTIONS, target=entity, provenance=mention))
                if entity.entity_type is EntityType.PROJECT:
                    add(RelationshipFact(source=entity, relationship_type=RelationshipType.DOCUMENTED_IN, target=doc_ref, provenance=mention))
            for fact in result.facts:
                add(fact)
        if not report.chunks_processed:
            raise GraphExtractionError("Extraction failed for every chunk; the graph was not changed")
        self._graph.replace_source(SourceKind.PERSONAL_DOCUMENT, doc.document_id, facts)
        report.entities = len({fold_text(f.target.name) for f in facts if f.relationship_type is RelationshipType.MENTIONS})
        report.relationships = len(facts)
        logger.info("Document graph extraction applied (document=%s, facts=%d, chunks_failed=%d)",
                    doc.document_id, len(facts), report.chunks_failed)
        return report
