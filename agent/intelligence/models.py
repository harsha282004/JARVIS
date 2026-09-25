"""Domain models of the personal intelligence layer.

Everything here is plain data. The layer READS from the existing services (tasks, reminders, events, calendar, Gmail, memory,
documents), builds a `Snapshot`, derives a `ContextGraph` from it and answers questions from that graph. Nothing derived is ever
written back to a source system without the user's confirmation.

Every derived fact carries `Provenance` (where it came from, when it was written, when JARVIS extracted it, how sure it is), and
every sentence JARVIS says is a `Statement` tagged FACT / EXTRACTED / INFERENCE / SUGGESTION / ACTION so the user can tell what
the source said from what JARVIS worked out.
"""

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any
from zoneinfo import ZoneInfo

from agent.memory.models import Confidence


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def stable_id(*parts: str) -> str:
    return hashlib.sha1("|".join(p.strip().lower() for p in parts).encode("utf-8")).hexdigest()[:16]


# ---- provenance & statements -------------------------------------------------------------------------------------------------


class SourceKind(StrEnum):
    EMAIL = "email"
    CALENDAR = "calendar"
    TASK = "task"
    REMINDER = "reminder"
    MEMORY = "memory"
    DOCUMENT = "document"
    EVENT_RECORD = "event_record"  # Phase 11 events and deadlines
    USER = "user"
    DERIVED = "derived"  # worked out by JARVIS from other records
    MESSAGE = "message"
    GITHUB = "github"


_SOURCE_PHRASE = {
    SourceKind.EMAIL: "an email", SourceKind.CALENDAR: "your calendar", SourceKind.TASK: "your task list",
    SourceKind.REMINDER: "your reminders", SourceKind.MEMORY: "your stored memory", SourceKind.DOCUMENT: "one of your documents",
    SourceKind.EVENT_RECORD: "your saved events and deadlines", SourceKind.USER: "what you told me", SourceKind.DERIVED: "my own analysis",
    SourceKind.MESSAGE: "a message", SourceKind.GITHUB: "GitHub",
}


@dataclass(frozen=True)
class Provenance:
    source_type: SourceKind
    source_id: str
    label: str  # short, sanitized, human: "email 'JARVIS project review'"
    source_timestamp: datetime | None = None  # when the source item was written/updated
    extracted_at: datetime = field(default_factory=utcnow)
    confidence: Confidence = Confidence.HIGH
    entity_id: str | None = None
    reference: str = ""  # a short quoted evidence sentence, sanitized

    @property
    def phrase(self) -> str:
        return _SOURCE_PHRASE[self.source_type]

    def describe(self) -> str:
        when = f", dated {self.source_timestamp.strftime('%b %d')}" if self.source_timestamp else ""
        return f"{self.phrase} ({self.label}{when})"

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_type": self.source_type.value, "source_id": self.source_id, "label": self.label,
            "source_timestamp": self.source_timestamp.isoformat() if self.source_timestamp else None,
            "extracted_at": self.extracted_at.isoformat(), "confidence": self.confidence.name.lower(), "entity_id": self.entity_id,
        }


class Certainty(StrEnum):
    FACT = "fact"  # retrieved from a source as it is
    EXTRACTED = "extracted"  # parsed out of source text by rules
    INFERENCE = "inference"  # a relationship JARVIS derived from evidence
    SUGGESTION = "suggestion"  # a recommendation; the user decides
    ACTION = "action"  # something JARVIS actually did (verified)


@dataclass(frozen=True)
class Statement:
    kind: Certainty
    text: str
    provenance: tuple[Provenance, ...] = ()

    def sources(self) -> str:
        seen: list[str] = []
        for p in self.provenance:
            d = p.describe()
            if d not in seen:
                seen.append(d)
        return "; ".join(seen)


def fact(text: str, *prov: Provenance) -> Statement:
    return Statement(Certainty.FACT, text, prov)


def extracted(text: str, *prov: Provenance) -> Statement:
    return Statement(Certainty.EXTRACTED, text, prov)


def inference(text: str, *prov: Provenance) -> Statement:
    return Statement(Certainty.INFERENCE, text, prov)


def suggestion(text: str, *prov: Provenance) -> Statement:
    return Statement(Certainty.SUGGESTION, text, prov)


def action(text: str, *prov: Provenance) -> Statement:
    return Statement(Certainty.ACTION, text, prov)


# ---- source snapshot -------------------------------------------------------------------------------------------------------------


class SourceState(StrEnum):
    OK = "ok"
    NOT_CONFIGURED = "not_configured"  # the integration is not set up: it is not an error, it is simply not a source
    UNAVAILABLE = "unavailable"  # set up, but failed to answer this time: what it would have said is unknown


@dataclass(frozen=True)
class EmailItem:
    message_id: str
    subject: str
    sender: str  # a display name, never an address
    received_at: datetime | None
    body: str  # bounded; untrusted external text
    action_requested: bool = False


@dataclass(frozen=True)
class CalendarItem:
    event_id: str
    calendar_id: str
    title: str
    start: datetime
    end: datetime
    all_day: bool = False
    blocks_time: bool = True


@dataclass(frozen=True)
class TaskItem:
    task_id: str
    title: str
    status: str  # pending | in_progress | overdue | completed | cancelled
    priority: int  # 1 low .. 4 critical
    due_at: datetime | None
    created_at: datetime | None = None
    completed_at: datetime | None = None
    estimate_minutes: int | None = None
    notes: str = ""

    @property
    def is_open(self) -> bool:
        return self.status in ("pending", "in_progress", "overdue")


@dataclass(frozen=True)
class ReminderItem:
    reminder_id: str
    message: str
    scheduled_at: datetime
    status: str  # scheduled | triggered | cancelled | expired
    task_id: str | None = None
    missed: bool = False


@dataclass(frozen=True)
class MemoryItem:
    memory_id: str
    content: str
    created_at: datetime
    confidence: Confidence
    explicit: bool = True
    kind: str = "fact"


@dataclass(frozen=True)
class DocumentItem:
    document_id: str
    title: str
    indexed_at: datetime | None = None
    text: str = ""  # bounded excerpt of the indexed text (untrusted); used for extraction only


@dataclass(frozen=True)
class EventRecordItem:
    event_id: str
    title: str
    event_type: str
    start_at: datetime | None
    due_at: datetime | None
    status: str
    confidence: Confidence = Confidence.HIGH
    task_id: str | None = None

    @property
    def anchor(self) -> datetime:
        return self.start_at or self.due_at  # type: ignore[return-value]


@dataclass(frozen=True)
class ExternalItem:
    """An item the Integration Hub normalized (GitHub repositories/commits/issues/pull requests, dates found in messages, hackathon registrations)."""

    kind: str  # repository | commit | issue | pull_request | event | deadline
    source: str  # github | telegram | gmail
    source_id: str
    title: str
    when: datetime | None
    confidence: Confidence = Confidence.HIGH
    metadata: tuple[tuple[str, Any], ...] = ()

    def meta(self, key: str, default: Any = None) -> Any:
        return dict(self.metadata).get(key, default)


@dataclass
class Snapshot:
    now: datetime
    zone: ZoneInfo
    emails: list[EmailItem] = field(default_factory=list)
    calendar: list[CalendarItem] = field(default_factory=list)
    tasks: list[TaskItem] = field(default_factory=list)
    reminders: list[ReminderItem] = field(default_factory=list)
    memories: list[MemoryItem] = field(default_factory=list)
    documents: list[DocumentItem] = field(default_factory=list)
    event_records: list[EventRecordItem] = field(default_factory=list)
    external: list[ExternalItem] = field(default_factory=list)
    states: dict[str, SourceState] = field(default_factory=dict)

    def ok(self, source: str) -> bool:
        return self.states.get(source) is SourceState.OK

    def unavailable(self) -> list[str]:
        return [name for name, state in self.states.items() if state is SourceState.UNAVAILABLE]

    def fingerprint(self) -> str:
        """Identity of the content (not the clock): equal fingerprints mean nothing that matters has changed."""
        h = hashlib.sha1()
        for e in self.emails:
            h.update(f"E{e.message_id}{e.subject}{hash(e.body) & 0xffffffff}".encode())
        for c in self.calendar:
            h.update(f"C{c.event_id}{c.title}{c.start.isoformat()}{c.end.isoformat()}".encode())
        for t in self.tasks:
            h.update(f"T{t.task_id}{t.title}{t.status}{t.priority}{t.due_at.isoformat() if t.due_at else ''}".encode())
        for r in self.reminders:
            h.update(f"R{r.reminder_id}{r.status}{r.scheduled_at.isoformat()}".encode())
        for m in self.memories:
            h.update(f"M{m.memory_id}{m.content}".encode())
        for d in self.documents:
            h.update(f"D{d.document_id}{d.title}".encode())
        for v in self.event_records:
            h.update(f"V{v.event_id}{v.title}{v.status}{v.anchor.isoformat() if v.anchor else ''}".encode())
        for x in self.external:
            h.update(f"X{x.source}{x.source_id}{x.title}{x.when.isoformat() if x.when else ''}".encode())
        h.update("|".join(f"{k}={v.value}" for k, v in sorted(self.states.items())).encode())
        return h.hexdigest()


# ---- entities and relationships ----------------------------------------------------------------------------------------------


class EntityKind(StrEnum):
    USER = "user"
    PROJECT = "project"
    TASK = "task"
    EVENT = "event"
    DEADLINE = "deadline"
    MEETING = "meeting"
    PERSON = "person"
    ORGANIZATION = "organization"
    DOCUMENT = "document"
    EMAIL = "email"
    MESSAGE = "message"
    HACKATHON = "hackathon"
    INTERNSHIP = "internship"
    JOB = "job"
    COURSE = "course"
    ASSIGNMENT = "assignment"
    PROJECT_REVIEW = "project_review"
    INTERVIEW = "interview"
    EXAM = "exam"
    REPOSITORY = "repository"
    COMMIT = "commit"
    ISSUE = "issue"
    PULL_REQUEST = "pull_request"


# Entities that happen at a time and that different sources may describe (candidates for resolution).
EVENT_LIKE = frozenset({
    EntityKind.EVENT, EntityKind.MEETING, EntityKind.PROJECT_REVIEW, EntityKind.HACKATHON, EntityKind.INTERVIEW, EntityKind.EXAM,
})


class RelationKind(StrEnum):
    BELONGS_TO = "belongs_to"  # TASK -> PROJECT
    APPLIES_TO = "applies_to"  # DEADLINE -> TASK
    REFERENCES = "references"  # EMAIL -> EVENT
    HAS_DEADLINE = "has_deadline"  # EVENT -> DEADLINE
    RELATES_TO = "relates_to"  # DOCUMENT / EVENT / TASK -> PROJECT (weaker than belongs_to)
    DEPENDS_ON = "depends_on"  # TASK -> TASK
    SAME_AS = "same_as"  # two sources describing one thing (kept when a merge would lose a conflicting detail)


@dataclass
class Entity:
    entity_id: str
    kind: EntityKind
    name: str
    when: datetime | None = None  # start, or due time for a deadline
    until: datetime | None = None
    all_day: bool = False
    status: str | None = None
    attributes: dict[str, Any] = field(default_factory=dict)
    provenance: list[Provenance] = field(default_factory=list)

    @property
    def confidence(self) -> Confidence:
        return max((p.confidence for p in self.provenance), default=Confidence.LOW)

    def add_provenance(self, prov: Provenance) -> None:
        if not any(p.source_type is prov.source_type and p.source_id == prov.source_id for p in self.provenance):
            self.provenance.append(prov)


@dataclass(frozen=True)
class Relationship:
    subject_id: str
    kind: RelationKind
    object_id: str
    confidence: Confidence
    reason: str  # short and evidence-based: "the task title contains the project name 'JARVIS'"
    provenance: tuple[Provenance, ...] = ()

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.subject_id, self.kind.value, self.object_id)


class ContextGraph:
    """Entities and typed, evidence-backed relationships. Rebuilt from the snapshot, so it is idempotent: the same sources
    always give the same ids and the same graph, and nothing is ever duplicated."""

    def __init__(self) -> None:
        self.entities: dict[str, Entity] = {}
        self._rels: dict[tuple[str, str, str], Relationship] = {}

    def add_entity(self, entity: Entity) -> Entity:
        existing = self.entities.get(entity.entity_id)
        if existing is None:
            self.entities[entity.entity_id] = entity
            return entity
        for p in entity.provenance:
            existing.add_provenance(p)
        return existing

    def add_relationship(self, rel: Relationship) -> bool:
        if rel.subject_id == rel.object_id or rel.subject_id not in self.entities or rel.object_id not in self.entities:
            return False
        current = self._rels.get(rel.key)
        if current is not None and current.confidence >= rel.confidence:
            return False
        self._rels[rel.key] = rel
        return True

    @property
    def relationships(self) -> list[Relationship]:
        return list(self._rels.values())

    def get(self, entity_id: str) -> Entity | None:
        return self.entities.get(entity_id)

    def of_kind(self, *kinds: EntityKind) -> list[Entity]:
        return [e for e in self.entities.values() if e.kind in kinds]

    def outgoing(self, entity_id: str, kind: RelationKind | None = None) -> list[Relationship]:
        return [r for r in self._rels.values() if r.subject_id == entity_id and (kind is None or r.kind is kind)]

    def incoming(self, entity_id: str, kind: RelationKind | None = None) -> list[Relationship]:
        return [r for r in self._rels.values() if r.object_id == entity_id and (kind is None or r.kind is kind)]

    def related(self, entity_id: str) -> list[tuple[Relationship, Entity]]:
        """Every entity connected to `entity_id` in either direction."""
        out: list[tuple[Relationship, Entity]] = []
        for r in self._rels.values():
            other = r.object_id if r.subject_id == entity_id else r.subject_id if r.object_id == entity_id else None
            if other is not None and other in self.entities:
                out.append((r, self.entities[other]))
        return out


@dataclass(frozen=True)
class Answer:
    """What JARVIS says, plus the evidence behind it (kept so "why?" and "where did you get that?" can be answered from it)."""

    text: str
    statements: tuple[Statement, ...] = ()
    entity_ids: tuple[str, ...] = ()
    detail: str | None = None  # the longer version, spoken on "tell me more"
    subject: str = ""  # a few words naming what this answer was about
