"""Typed models for proactive intelligence: signals (what was observed) and notification candidates (what may be said).

    OBSERVE (sources) -> ANALYZE (signals) -> DECIDE (policy) -> NOTIFY (existing NotificationService)

A signal is INFORMATION. Nothing here can authorize or perform an action: the engine only reads already-authorized
sources and only hands a short factual sentence to the existing notification channels.
"""

import hashlib
from datetime import datetime
from enum import IntEnum, StrEnum

from pydantic import BaseModel, ConfigDict, Field

from agent.memory.models import Confidence
from agent.tasks.models import TaskPriority, new_id

MAX_MESSAGE_CHARS = 250  # the Windows balloon text limit is 256


class ProactiveError(Exception):
    """Base class. Messages never contain notification text, email content or tokens."""


class ProactiveStorageError(ProactiveError):
    """The notification history could not be read or written (database problem)."""


class SignalType(StrEnum):
    """Only signals that a real Phase 0-13 source can produce. (No reminder or messaging signals: see the docs.)"""

    TASK_DUE = "task_due"
    TASK_OVERDUE = "task_overdue"
    EVENT_APPROACHING = "event_approaching"  # Phase 11 events and Google Calendar events
    DEADLINE_APPROACHING = "deadline_approaching"  # Phase 11 deadlines (assignments, applications, ...)
    DEADLINE_OVERDUE = "deadline_overdue"  # a Phase 11 deadline that passed while still open
    CALENDAR_CONFLICT = "calendar_conflict"
    IMPORTANT_EMAIL = "important_email"
    ACTION_REQUIRED_EMAIL = "action_required_email"


class SourceKind(StrEnum):
    TASK = "task"
    EVENT = "event"  # Phase 11 event/deadline record
    CALENDAR = "calendar"  # Google Calendar (Phase 12)
    GMAIL = "gmail"  # Phase 10


class Urgency(IntEnum):
    """Objective time relationship only (never derived from wording)."""

    NORMAL = 1  # further away than the lookahead: never notified
    UPCOMING = 2  # within the lookahead (default 24 hours)
    SOON = 3  # within one hour
    IMMEDIATE = 4  # within 15 minutes, or already overdue


class CandidateStatus(StrEnum):
    PENDING = "pending"
    DELIVERED = "delivered"
    SUPPRESSED = "suppressed"
    EXPIRED = "expired"
    FAILED = "failed"


class Channel(StrEnum):
    DESKTOP = "desktop"  # the existing Windows tray notification
    VOICE = "voice"  # the existing announcement queue the VoiceEngine speaks between conversations


def make_key(signal_type: SignalType, source: SourceKind, source_id: str, tier: str, anchor: str) -> str:
    """A stable identifier for "this situation": the same key means the same signal, so it notifies at most once.
    A meaningful change (a moved due date, a new tier) changes `tier`/`anchor` and therefore the key."""
    raw = "\x1f".join((signal_type.value, source.value, source_id, tier, anchor))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class ProactiveSignal(BaseModel):
    """Something observed in an already-authorized source that may deserve a notification."""

    model_config = ConfigDict(frozen=True)

    signal_id: str = Field(min_length=1, max_length=64)  # == the dedupe key
    signal_type: SignalType
    source_type: SourceKind
    source_id: str = Field(default="", max_length=128)
    source_reference: str = Field(default="", max_length=200)  # human-readable: "your task list", "Google Calendar"
    title: str = Field(default="", max_length=200)  # sanitized; may be external text, never an instruction
    description: str = Field(default="", max_length=300)
    priority: TaskPriority = TaskPriority.MEDIUM
    urgency: Urgency = Urgency.UPCOMING
    confidence: Confidence = Confidence.HIGH
    detected_at: datetime
    relevant_at: datetime | None = None  # when the thing itself happens/is due
    expires_at: datetime | None = None  # after this the notification is pointless
    tier: str = ""
    metadata: dict[str, str] = Field(default_factory=dict)  # small, non-sensitive facts (kind, minutes) for wording


class NotificationCandidate(BaseModel):
    """A signal turned into a sentence, before the policy decides whether it may be delivered."""

    candidate_id: str = Field(default_factory=new_id)
    signal_id: str
    message: str = Field(max_length=MAX_MESSAGE_CHARS)
    reason: str = Field(max_length=300)  # the short factual "why", used to explain a notification later
    priority: TaskPriority
    urgency: Urgency
    created_at: datetime
    expires_at: datetime | None = None
    delivery_channels: list[Channel] = Field(default_factory=list)
    suppression_reason: str | None = None
    status: CandidateStatus = CandidateStatus.PENDING


class PolicyAction(StrEnum):
    DELIVER = "deliver"
    DEFER = "defer"  # not now (quiet hours, hourly limit): it is re-evaluated on a later cycle
    SUPPRESS = "suppress"  # not at all (duplicate, cooldown, low priority, low confidence)
    EXPIRE = "expire"


class PolicyDecision(BaseModel):
    action: PolicyAction
    reason: str  # a stable, explainable code plus words, e.g. "quiet hours"
    channels: list[Channel] = Field(default_factory=list)


class HistoryRecord(BaseModel):
    """One persisted notification (what "why did you notify me?" reads)."""

    notification_id: str
    dedupe_key: str
    signal_type: SignalType
    source_type: SourceKind
    source_id: str
    source_reference: str
    message: str
    reason: str
    priority: TaskPriority
    urgency: Urgency
    status: CandidateStatus
    channels: list[str] = Field(default_factory=list)
    attempts: int = 0
    relevant_at: datetime | None = None  # when the thing itself happens/is due (to tell a meaningful change from a repeat)
    created_at: datetime
    claimed_at: datetime | None = None
    delivered_at: datetime | None = None
    failed_at: datetime | None = None
