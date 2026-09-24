"""Turning source text into stored events, with provenance: Gmail, indexed documents, saved memories.

Nothing runs automatically: these are called by the (permission-gated) `event_extract` tool after the user asks.
Source text is untrusted data; only a short evidence sentence is stored as the description. The same source
processed twice stores nothing new (the repository's unique source + dedupe key). Low-confidence results from
these sources are stored as UNKNOWN (unconfirmed) and never appear as firm deadlines until the user confirms.
"""

from dataclasses import dataclass, field
from datetime import datetime

from agent.events.dates import event_times
from agent.events.extraction import ExtractionResult, Unresolved, extract_events
from agent.events.graph import EventGraphLinker
from agent.events.models import Event, EventSource, SourceType
from agent.events.service import EventService
from agent.memory.models import Confidence, MemoryBasis
from agent.tasks.timeparse import TimeParser
from backend.core.logging import get_logger
from integrations.gmail.models import GmailMessage
from integrations.gmail.text import strip_quoted_reply

logger = get_logger(__name__)

MAX_DOCUMENT_CHUNKS = 60
MAX_GMAIL_CHARS = 8000


@dataclass
class IngestResult:
    created: list[Event] = field(default_factory=list)
    duplicates: list[Event] = field(default_factory=list)  # already stored from this source: nothing new
    unresolved: list[Unresolved] = field(default_factory=list)  # dates too vague/ambiguous to store; questions to ask
    expired: int = 0  # dates already in the past: not imported
    truncated: bool = False

    @property
    def unconfirmed(self) -> int:
        from agent.events.models import EventStatus

        return sum(1 for e in self.created if e.status is EventStatus.UNKNOWN)


class EventIngestor:
    def __init__(self, events: EventService, parser: TimeParser, linker: EventGraphLinker | None = None):
        self._events = events
        self._parser = parser
        self._linker = linker

    def _store(
        self, extraction: ExtractionResult, source: EventSource, metadata: dict, out: IngestResult,
        document_name: str | None = None,
    ) -> None:
        out.unresolved += extraction.unresolved
        out.expired += extraction.expired
        out.truncated = out.truncated or extraction.truncated
        for candidate in extraction.candidates:
            times = event_times(candidate.when, candidate.event_type)
            result = self._events.create_event(
                candidate.title, candidate.event_type, start_at=times.start_at, end_at=times.end_at, due_at=times.due_at,
                all_day=times.all_day, description=candidate.evidence, source=source, confidence=candidate.confidence,
                metadata={**metadata, "phrase": candidate.phrase[:80]},
            )
            if not result.created:
                out.duplicates.append(result.event)
                continue
            out.created.append(result.event)
            if self._linker is not None:
                links = self._linker.link(result.event, document_name=document_name)
                if links.relationship_ids:
                    self._events.merge_metadata(result.event.event_id, {"graph_links": links.relationship_ids})

    def from_gmail_message(self, message: GmailMessage, now: datetime) -> IngestResult:
        """Events in one email. Relative dates count from the day it was sent. Untrusted text: pattern-matched only."""
        reference = (message.timestamp or now).astimezone(self._parser.zone)
        body = strip_quoted_reply(message.plain_text_body or message.snippet)[:MAX_GMAIL_CHARS]
        sender = message.sender.display if message.sender else "an unknown sender"
        sent = reference.strftime("%B %d, %Y").replace(" 0", " ")
        extraction = extract_events(body, reference=reference, parser=self._parser, now=now, subject=message.subject or None)
        source = EventSource(source_type=SourceType.GMAIL, source_id=message.message_id,
                             reference=f"email from {sender}, dated {sent}")
        out = IngestResult()
        self._store(extraction, source, {"thread_id": message.thread_id}, out)
        return out

    def from_document(self, document_id: str, filename: str, chunks: list, now: datetime) -> IngestResult:
        """Deadlines in an indexed document (at most 60 chunks). Keeps document id, filename, page and chunk."""
        out = IngestResult()
        for chunk in chunks[:MAX_DOCUMENT_CHUNKS]:
            extraction = extract_events(chunk.text, reference=now, parser=self._parser, now=now)
            page = f", page {chunk.page}" if getattr(chunk, "page", None) else ""
            source = EventSource(source_type=SourceType.RAG_DOCUMENT, source_id=document_id, reference=f"{filename}{page}")
            self._store(extraction, source, {"chunk_id": getattr(chunk, "chunk_id", None), "page": getattr(chunk, "page", None)},
                        out, document_name=filename)
        out.truncated = out.truncated or len(chunks) > MAX_DOCUMENT_CHUNKS
        return out

    def from_memories(self, memories: list, now: datetime) -> IngestResult:
        """Dates in memories the user stated explicitly. Inferred memories are ignored, and a remembered date is
        never turned into an event unless the user asks for this."""
        out = IngestResult()
        for memory in memories:
            if memory.basis is not MemoryBasis.EXPLICIT:
                continue
            extraction = extract_events(memory.content, reference=now, parser=self._parser, now=now, user_stated=True)
            source = EventSource(source_type=SourceType.MEMORY, source_id=memory.memory_id, reference="a memory you told me")
            self._store(extraction, source, {}, out)
        return out
