"""Typed models for tasks and reminders.

A task is work to do; a reminder is a scheduled notification. A reminder may
point at a task but does not need to. All timestamps are timezone-aware; the
database stores UTC and the application converts to the user's timezone.
"""

import re
from datetime import datetime, timezone
from enum import IntEnum, StrEnum
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

MAX_TITLE_CHARS = 200
MAX_NOTES_CHARS = 2000
MAX_MESSAGE_CHARS = 300
MAX_HISTORY_ENTRIES = 50

_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


def new_id() -> str:
    return uuid4().hex


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class TaskError(Exception):
    """Base class for task/reminder errors. Messages never contain task content."""


class TaskValidationError(TaskError):
    pass


class TaskNotFound(TaskError):
    pass


class InvalidTransition(TaskError):
    """The requested status change is not allowed from the current status."""


class TaskStorageError(TaskError):
    """The database failed. Carries the exception type only (driver messages echo SQL parameters)."""


def to_utc(value: datetime) -> datetime:
    """Normalize an aware datetime to UTC. A naive datetime is a bug, never guessed."""
    if value.tzinfo is None or value.utcoffset() is None:
        raise TaskValidationError("Timestamps must be timezone-aware")
    return value.astimezone(timezone.utc)


class TaskStatus(StrEnum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    OVERDUE = "overdue"


OPEN_STATUSES = frozenset({TaskStatus.PENDING, TaskStatus.IN_PROGRESS, TaskStatus.OVERDUE})

# Every allowed status change. Anything else is refused; reopening is the separate, explicit reopen_task().
TASK_TRANSITIONS: dict[TaskStatus, frozenset[TaskStatus]] = {
    TaskStatus.PENDING: frozenset(
        {TaskStatus.IN_PROGRESS, TaskStatus.COMPLETED, TaskStatus.CANCELLED, TaskStatus.OVERDUE}
    ),
    TaskStatus.IN_PROGRESS: frozenset({TaskStatus.COMPLETED, TaskStatus.CANCELLED}),
    # OVERDUE -> PENDING happens when the due date is moved to the future (update_task).
    TaskStatus.OVERDUE: frozenset(
        {TaskStatus.PENDING, TaskStatus.IN_PROGRESS, TaskStatus.COMPLETED, TaskStatus.CANCELLED}
    ),
    TaskStatus.COMPLETED: frozenset(),
    TaskStatus.CANCELLED: frozenset(),
}
# reopen_task(): the only way out of a terminal status.
REOPEN_FROM = frozenset({TaskStatus.COMPLETED, TaskStatus.CANCELLED})


class TaskPriority(IntEnum):
    LOW = 1
    MEDIUM = 2
    HIGH = 3
    CRITICAL = 4


class ReminderStatus(StrEnum):
    SCHEDULED = "scheduled"
    TRIGGERED = "triggered"
    CANCELLED = "cancelled"
    EXPIRED = "expired"


class Frequency(StrEnum):
    DAILY = "daily"
    WEEKLY = "weekly"
    MONTHLY = "monthly"


class MissedPolicy(StrEnum):
    NOTIFY = "notify"  # deliver it late, clearly marked as missed
    EXPIRE = "expire"  # do not deliver; mark it expired (recurring: skip to the next occurrence)


def _clean_text(value: str, limit: int, field: str) -> str:
    text = " ".join(_CONTROL.sub(" ", value).split())
    if not text:
        raise ValueError(f"{field} must not be blank")
    if len(text) > limit:
        raise ValueError(f"{field} is too long (max {limit} characters)")
    return text


class Recurrence(BaseModel):
    """Structured recurrence, evaluated in the reminder's own timezone (wall-clock time).

    daily: every day at hour:minute. weekly: on `weekdays` (0=Monday .. 6=Sunday).
    monthly: on `day_of_month` (a day past the end of a short month is clamped to its last day).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    frequency: Frequency
    hour: int = Field(ge=0, le=23)
    minute: int = Field(default=0, ge=0, le=59)
    weekdays: tuple[int, ...] = ()
    day_of_month: int | None = Field(default=None, ge=1, le=31)

    @model_validator(mode="after")
    def _check(self) -> "Recurrence":
        if self.frequency is Frequency.WEEKLY:
            if (
                not self.weekdays
                or any(not 0 <= d <= 6 for d in self.weekdays)
                or len(set(self.weekdays)) != len(self.weekdays)
            ):
                raise ValueError("weekly recurrence needs unique weekdays 0-6")
            if self.day_of_month is not None:
                raise ValueError("weekly recurrence has no day_of_month")
        elif self.frequency is Frequency.MONTHLY:
            if self.day_of_month is None or self.weekdays:
                raise ValueError("monthly recurrence needs day_of_month and no weekdays")
        elif self.weekdays or self.day_of_month is not None:
            raise ValueError("daily recurrence takes no weekdays or day_of_month")
        return self

    def to_json(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


class Task(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task_id: str = Field(default_factory=new_id)
    title: str
    notes: str | None = None
    status: TaskStatus = TaskStatus.PENDING
    priority: TaskPriority = TaskPriority.MEDIUM
    created_at: datetime
    updated_at: datetime
    due_at: datetime | None = None
    completed_at: datetime | None = None
    cancelled_at: datetime | None = None
    session_id: str | None = None
    source: str = "api"
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("title")
    @classmethod
    def _title(cls, value: str) -> str:
        return _clean_text(value, MAX_TITLE_CHARS, "title")

    @field_validator("notes")
    @classmethod
    def _notes(cls, value: str | None) -> str | None:
        if value is None or not value.strip():
            return None
        if len(value) > MAX_NOTES_CHARS:
            raise ValueError(f"notes are too long (max {MAX_NOTES_CHARS} characters)")
        return _CONTROL.sub(" ", value).strip()

    @field_validator("created_at", "updated_at", "due_at", "completed_at", "cancelled_at")
    @classmethod
    def _aware(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("timestamps must be timezone-aware")
        return value.astimezone(timezone.utc)

    @model_validator(mode="after")
    def _consistent(self) -> "Task":
        if self.status is TaskStatus.COMPLETED and self.completed_at is None:
            raise ValueError("a completed task needs completed_at")
        if self.status is TaskStatus.CANCELLED and self.cancelled_at is None:
            raise ValueError("a cancelled task needs cancelled_at")
        return self

    @property
    def is_open(self) -> bool:
        return self.status in OPEN_STATUSES

    def is_overdue(self, now: datetime) -> bool:
        return self.is_open and self.due_at is not None and self.due_at < now


class Reminder(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reminder_id: str = Field(default_factory=new_id)
    task_id: str | None = None
    message: str
    scheduled_at: datetime
    status: ReminderStatus = ReminderStatus.SCHEDULED
    timezone: str
    recurrence: Recurrence | None = None
    created_at: datetime
    updated_at: datetime
    triggered_at: datetime | None = None  # last delivery (recurring reminders update it each time)
    cancelled_at: datetime | None = None
    occurrences: int = 0  # deliveries so far
    delivery_attempts: int = 0  # failed attempts for the current occurrence
    claimed_at: datetime | None = None  # delivery lease; set only while a scheduler is delivering it
    session_id: str | None = None
    source: str = "api"
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("message")
    @classmethod
    def _message(cls, value: str) -> str:
        return _clean_text(value, MAX_MESSAGE_CHARS, "message")

    @field_validator("timezone")
    @classmethod
    def _tz(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError, OSError):
            raise ValueError("unknown timezone") from None
        return value

    @field_validator("scheduled_at", "created_at", "updated_at", "triggered_at", "cancelled_at", "claimed_at")
    @classmethod
    def _aware(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("timestamps must be timezone-aware")
        return value.astimezone(timezone.utc)

    @model_validator(mode="after")
    def _consistent(self) -> "Reminder":
        if self.status is ReminderStatus.TRIGGERED and self.triggered_at is None:
            raise ValueError("a triggered reminder needs triggered_at")
        if self.status is ReminderStatus.CANCELLED and self.cancelled_at is None:
            raise ValueError("a cancelled reminder needs cancelled_at")
        return self

    @property
    def is_recurring(self) -> bool:
        return self.recurrence is not None

    @property
    def zone(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)
