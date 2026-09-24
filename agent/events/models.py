"""Typed models for events and deadlines.

An event is something at a time (a meeting, an interview, an exam). A deadline is something that must be
done by a time (`due_at`, no start). Both are `Event` rows, told apart by which timestamp is set. Every event
carries its provenance (where JARVIS learned it) and an extraction confidence. All timestamps are timezone-aware
and stored as UTC; `timezone` records the user's zone the times were understood in.
"""

import hashlib
import re
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from agent.memory.models import Confidence
from agent.tasks.models import TaskPriority

MAX_TITLE_CHARS = 200
MAX_DESCRIPTION_CHARS = 500
MAX_REFERENCE_CHARS = 300
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


def new_id() -> str:
    return uuid4().hex


class EventError(Exception):
    """Base class. `user_message` is safe to say aloud; exception text never contains source content."""

    user_message = "Something went wrong with the event."

    def __init__(self, detail: str = "", user_message: str | None = None):
        super().__init__(detail or user_message or self.user_message)
        if user_message:
            self.user_message = user_message


class EventValidationError(EventError):
    user_message = "That doesn't look like a valid event."


class EventNotFound(EventError):
    user_message = "I can't find that event any more, so nothing was changed."


class InvalidEventTransition(EventError):
    user_message = "That isn't possible for an event in its current state."


class EventStorageError(EventError):
    user_message = "I couldn't do that because my event database isn't available, so nothing was changed."


class EventType(StrEnum):
    DEADLINE = "deadline"
    MEETING = "meeting"
    INTERVIEW = "interview"
    EXAM = "exam"
    ASSIGNMENT = "assignment"
    APPLICATION = "application"
    APPOINTMENT = "appointment"
    EVENT = "event"
    REMINDER = "reminder"
    OTHER = "other"


# Types that are normally "due by" (a `due_at`), as opposed to "happens at" (a `start_at`).
DUE_TYPES = frozenset({EventType.DEADLINE, EventType.ASSIGNMENT, EventType.APPLICATION})


class EventStatus(StrEnum):
    UPCOMING = "upcoming"
    ACTIVE = "active"  # started and not over
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    MISSED = "missed"  # its time passed without it being marked completed
    UNKNOWN = "unknown"  # unconfirmed: a low-confidence extraction waiting for the user


OPEN_STATUSES = frozenset({EventStatus.UPCOMING, EventStatus.ACTIVE})
LIVE_STATUSES = frozenset({EventStatus.UPCOMING, EventStatus.ACTIVE, EventStatus.MISSED})

EVENT_TRANSITIONS: dict[EventStatus, frozenset[EventStatus]] = {
    EventStatus.UPCOMING: frozenset(
        {EventStatus.ACTIVE, EventStatus.COMPLETED, EventStatus.CANCELLED, EventStatus.MISSED}
    ),
    EventStatus.ACTIVE: frozenset({EventStatus.COMPLETED, EventStatus.CANCELLED, EventStatus.MISSED}),
    # MISSED -> UPCOMING when it is rescheduled to the future (update); done late is still done.
    EventStatus.MISSED: frozenset({EventStatus.COMPLETED, EventStatus.CANCELLED, EventStatus.UPCOMING}),
    EventStatus.UNKNOWN: frozenset({EventStatus.UPCOMING, EventStatus.COMPLETED, EventStatus.CANCELLED}),
    EventStatus.COMPLETED: frozenset(),
    EventStatus.CANCELLED: frozenset(),
}


class SourceType(StrEnum):
    CONVERSATION = "conversation"
    MEMORY = "memory"
    RAG_DOCUMENT = "rag_document"
    GMAIL = "gmail"
    TASK = "task"
    USER_EXPLICIT = "user_explicit"
    UNKNOWN = "unknown"


class EventSource(BaseModel):
    """Where an event came from. Never invented: without provenance the type is UNKNOWN."""

    model_config = ConfigDict(frozen=True)

    source_type: SourceType = SourceType.UNKNOWN
    source_id: str | None = Field(default=None, max_length=128)  # message id, document id, memory id, task id
    reference: str | None = Field(default=None, max_length=MAX_REFERENCE_CHARS)  # e.g. "email from X, dated ..."

    @field_validator("reference")
    @classmethod
    def _clean(cls, value: str | None) -> str | None:
        if value is None:
            return None
        text = " ".join(_CONTROL.sub(" ", value).replace("<", "(").replace(">", ")").split())
        return text[:MAX_REFERENCE_CHARS] or None


def _clean_text(value: str, limit: int, field: str) -> str:
    text = " ".join(_CONTROL.sub(" ", value).replace("<", "(").replace(">", ")").split())
    if not text:
        raise ValueError(f"{field} must not be blank")
    if len(text) > limit:
        raise ValueError(f"{field} is too long (max {limit} characters)")
    return text


def make_dedupe_key(title: str, event_type: EventType, anchor: datetime) -> str:
    """Stable identity of "this event from this source": the same title, type and time. Not semantic."""
    words = " ".join(re.findall(r"[a-z0-9]+", title.lower()))
    raw = f"{words}|{event_type.value}|{anchor.astimezone(timezone.utc).isoformat()}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


class Event(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_id: str = Field(default_factory=new_id)
    title: str
    description: str | None = None  # a short evidence excerpt or note; never a whole email or document
    event_type: EventType = EventType.EVENT
    status: EventStatus = EventStatus.UPCOMING
    priority: TaskPriority | None = None  # the Phase 9 scale; None = unknown, never guessed
    start_at: datetime | None = None
    end_at: datetime | None = None
    due_at: datetime | None = None
    timezone: str
    all_day: bool = False  # only the date is known (start: local midnight, deadline: end of that local day)
    source: EventSource = Field(default_factory=EventSource)
    confidence: Confidence = Confidence.HIGH
    task_id: str | None = None  # the Phase 9 task this belongs to, if any (a reference, not a copy)
    dedupe_key: str = ""
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None = None
    cancelled_at: datetime | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("title")
    @classmethod
    def _title(cls, value: str) -> str:
        return _clean_text(value, MAX_TITLE_CHARS, "title")

    @field_validator("description")
    @classmethod
    def _description(cls, value: str | None) -> str | None:
        if value is None or not value.strip():
            return None
        return _clean_text(value, MAX_DESCRIPTION_CHARS, "description")

    @field_validator("timezone")
    @classmethod
    def _tz(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError, OSError):
            raise ValueError("unknown timezone") from None
        return value

    @field_validator("start_at", "end_at", "due_at", "created_at", "updated_at", "completed_at", "cancelled_at")
    @classmethod
    def _aware(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("timestamps must be timezone-aware")
        return value.astimezone(timezone.utc)

    @model_validator(mode="after")
    def _consistent(self) -> "Event":
        if self.start_at is None and self.due_at is None:
            raise ValueError("an event needs a start time or a due time")
        if self.end_at is not None:
            if self.start_at is None:
                raise ValueError("an end time needs a start time")
            if self.end_at < self.start_at:
                raise ValueError("the end must not be before the start")
        if self.status is EventStatus.COMPLETED and self.completed_at is None:
            raise ValueError("a completed event needs completed_at")
        if self.status is EventStatus.CANCELLED and self.cancelled_at is None:
            raise ValueError("a cancelled event needs cancelled_at")
        return self

    @property
    def is_deadline(self) -> bool:
        return self.due_at is not None and self.start_at is None

    @property
    def anchor(self) -> datetime:
        """The time used for ordering and windows: when it starts, or when it is due."""
        return self.start_at or self.due_at  # type: ignore[return-value]

    @property
    def is_open(self) -> bool:
        return self.status in OPEN_STATUSES

    @property
    def zone(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)


class CreateResult(BaseModel):
    event: Event
    created: bool  # False: an identical event from the same source already existed (nothing new stored)
    revived: bool = False  # a cancelled/completed identical event the user explicitly added again
