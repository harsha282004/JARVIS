"""JARVIS-owned Google Calendar models. Raw Google API objects never leave the parser.

Everything that came from a calendar (titles, descriptions, locations, attendees) is untrusted external text
(anyone can send an invitation): it is data to be shown to the user, never an instruction.
"""

import re
from datetime import date, datetime, timedelta, timezone
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator


class CalendarError(Exception):
    """Base class. `user_message` is safe to say aloud; the exception text never contains event content, tokens
    or credentials (only the exception type is ever logged)."""

    user_message = "Something went wrong while talking to Google Calendar."

    def __init__(self, detail: str = "", user_message: str | None = None):
        super().__init__(detail or user_message or self.user_message)
        if user_message:
            self.user_message = user_message


class CalendarNotConfigured(CalendarError):
    user_message = (
        "Google Calendar isn't set up yet. Add your Google OAuth client and run python scripts/calendar_cli.py auth. "
        "See docs/google-calendar-integration.md."
    )


class CalendarAuthError(CalendarError):
    user_message = "I couldn't sign in to Google Calendar. Please run python scripts/calendar_cli.py auth again."


class CalendarAuthRevoked(CalendarAuthError):
    user_message = "Google Calendar access was revoked or has expired. Please run python scripts/calendar_cli.py auth again."


class CalendarPermissionDenied(CalendarError):
    user_message = "Google didn't allow that. You may not have permission to change that calendar, or the permission wasn't granted."


class CalendarRateLimited(CalendarError):
    user_message = "Google Calendar is rate limiting requests right now. Please try again in a minute."


class CalendarUnavailable(CalendarError):
    user_message = "I can't reach Google Calendar right now. Please check your connection and try again."


class CalendarNotFound(CalendarError):
    user_message = "That calendar or event can't be found any more, so nothing was changed."


class CalendarConflictError(CalendarError):
    user_message = "That event changed on Google Calendar while I was working on it, so I stopped. Please try again."


class CalendarInvalid(CalendarError):
    user_message = "Google Calendar rejected those event details."


class CalendarResponseError(CalendarError):
    user_message = "Google Calendar sent back something I couldn't understand."


class CalendarOutcomeUnknown(CalendarError):
    user_message = (
        "I couldn't confirm whether that worked. Please check your calendar before asking again, so nothing is created twice."
    )


class EventStatusText(StrEnum):
    CONFIRMED = "confirmed"
    TENTATIVE = "tentative"
    CANCELLED = "cancelled"


class CalendarInfo(BaseModel):
    calendar_id: str
    summary: str = ""
    description: str = ""
    timezone: str | None = None
    primary: bool = False
    selected: bool = True  # shown in the user's calendar list
    access_role: str = "reader"  # owner | writer | reader | freeBusyReader

    @property
    def writable(self) -> bool:
        return self.access_role in ("owner", "writer")


class CalendarEventAttendee(BaseModel):
    email: str
    name: str = ""
    response_status: str = "needsAction"  # needsAction | declined | tentative | accepted
    organizer: bool = False
    self_: bool = False
    optional: bool = False

    @property
    def display(self) -> str:
        return self.name or self.email


class CalendarEventReminder(BaseModel):
    method: str = "popup"  # email | popup
    minutes: int = Field(default=10, ge=0)


class CalendarEventSource(BaseModel):
    """Marks an event as coming from Google Calendar (as opposed to JARVIS's own Phase 11 events)."""

    model_config = ConfigDict(frozen=True)

    origin: str = "google_calendar"
    calendar_id: str
    event_id: str
    etag: str | None = None

    @property
    def mapping_id(self) -> str:
        """The stable identifier stored on a mapped Phase 11 event."""
        return f"{self.calendar_id}/{self.event_id}"


class CalendarEvent(BaseModel):
    """One Google Calendar event (for recurring events: one occurrence)."""

    model_config = ConfigDict(extra="forbid")

    event_id: str
    calendar_id: str
    etag: str | None = None
    summary: str = ""
    description: str = ""
    location: str = ""
    start: datetime  # aware UTC; for an all-day event: local midnight of the first day, in `timezone`
    end: datetime  # aware UTC; exclusive end (all-day: local midnight after the last day)
    timezone: str | None = None  # the event's own zone when present
    all_day: bool = False
    organizer: CalendarEventAttendee | None = None
    attendees: list[CalendarEventAttendee] = Field(default_factory=list)
    reminders: list[CalendarEventReminder] = Field(default_factory=list)
    meeting_link: str | None = None  # an existing video-call link, only if it is a plain https URL
    recurrence: list[str] = Field(default_factory=list)  # RRULE lines of the series (master events only)
    recurring_event_id: str | None = None  # set on an occurrence: the series' master id
    status: str = "confirmed"
    busy: bool = True  # False when the event is marked "free" (transparent): it does not block time
    created: datetime | None = None
    updated: datetime | None = None
    html_link: str | None = None

    @model_validator(mode="after")
    def _order(self) -> "CalendarEvent":
        if self.end < self.start:
            raise ValueError("an event cannot end before it starts")
        return self

    @property
    def source(self) -> CalendarEventSource:
        return CalendarEventSource(calendar_id=self.calendar_id, event_id=self.event_id, etag=self.etag)

    @property
    def declined_by_me(self) -> bool:
        return any(a.self_ and a.response_status == "declined" for a in self.attendees)

    @property
    def blocks_time(self) -> bool:
        return self.busy and not self.is_cancelled and not self.declined_by_me

    @property
    def is_recurring(self) -> bool:
        return bool(self.recurrence or self.recurring_event_id)

    @property
    def is_cancelled(self) -> bool:
        return self.status == "cancelled"

    @property
    def duration(self) -> timedelta:
        return self.end - self.start


class CalendarEventDraft(BaseModel):
    """A validated new event, built by code from resolved values (never from raw model output)."""

    model_config = ConfigDict(extra="forbid")

    event_id: str  # generated by JARVIS so a retried insert cannot create a duplicate
    summary: str = Field(min_length=1, max_length=300)
    start: datetime
    end: datetime
    all_day: bool = False
    timezone: str
    location: str = Field(default="", max_length=300)
    description: str = Field(default="", max_length=2000)
    attendees: list[str] = Field(default_factory=list, max_length=20)  # validated email addresses only
    recurrence: list[str] = Field(default_factory=list, max_length=3)  # RRULE lines built by code

    @model_validator(mode="after")
    def _check(self) -> "CalendarEventDraft":
        for value in (self.start, self.end):
            if value.tzinfo is None or value.utcoffset() is None:
                raise ValueError("times must be timezone-aware")
        if self.end <= self.start:
            raise ValueError("the end must be after the start")
        return self


class CalendarEventPatch(BaseModel):
    """Changes to an existing event. Only fields that are set are sent."""

    model_config = ConfigDict(extra="forbid")

    summary: str | None = Field(default=None, min_length=1, max_length=300)
    start: datetime | None = None
    end: datetime | None = None
    all_day: bool | None = None
    timezone: str | None = None
    location: str | None = Field(default=None, max_length=300)
    description: str | None = Field(default=None, max_length=2000)

    @property
    def is_empty(self) -> bool:
        return not any((self.summary, self.start, self.end, self.location is not None, self.description is not None))


class CalendarEventsResult(BaseModel):
    events: list[CalendarEvent] = Field(default_factory=list)  # start order
    next_page_token: str | None = None
    truncated: bool = False  # more matching events exist than were returned


_EMAIL = re.compile(r"^[A-Za-z0-9._%+\-']{1,64}@(?:[A-Za-z0-9\-]{1,63}\.)+[A-Za-z]{2,24}$")


def valid_email(value: str) -> bool:
    return isinstance(value, str) and len(value) <= 254 and bool(_EMAIL.match(value.strip()))


def _iso(value: datetime | date | None) -> Any:
    return value.isoformat() if value else None


def utc(value: datetime) -> datetime:
    return value.astimezone(timezone.utc)
