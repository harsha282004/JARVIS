"""The structured Google Calendar action the LLM may propose (models only; nothing here touches Google).

The model supplies words: a calendar's name, a title, times exactly as the user said them, a location, e-mail
addresses the user gave, a recurrence phrase. Times, calendars and events are resolved by code; events are found by
searching and asked about when ambiguous. The model can never supply an event or calendar id, a URL, an HTTP method,
a token, a path, an RRULE, an invitation setting, a command or SQL: such keys make the action invalid.
"""

from enum import StrEnum
from typing import Any, Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator


class CalendarActionName(StrEnum):
    LIST = "calendar_list"
    EVENTS = "calendar_events"
    SEARCH = "calendar_search"
    GET_EVENT = "calendar_get_event"
    CREATE_EVENT = "calendar_create_event"
    UPDATE_EVENT = "calendar_update_event"
    CANCEL_EVENT = "calendar_cancel_event"


CALENDAR_ACTION_NAMES = frozenset(a.value for a in CalendarActionName)

FORBIDDEN_KEYS = frozenset({
    "id", "event_id", "eventid", "calendar_id", "calendarid", "cal_id", "recurring_event_id", "etag", "ical_uid",
    "url", "uri", "endpoint", "api", "method", "http_method", "headers", "params", "fields", "body", "token",
    "access_token", "refresh_token", "authorization", "credentials", "scopes", "oauth_scope", "path", "filepath", "file",
    "command", "cmd", "shell", "sql", "rrule", "recurrence_rule", "send_updates", "sendupdates", "send_notifications",
    "conference", "conference_data", "conferencedata", "guests_can_modify", "status", "visibility",
})

_SCOPE_ALIASES = {
    "week": "this_week", "this week": "this_week", "next week": "next_week", "7 days": "next_7_days",
    "next 7 days": "next_7_days", "coming up": "upcoming", "soon": "upcoming", "next": "upcoming",
}


class InvalidCalendarAction(ValueError):
    """The proposed action is not a valid calendar action. The message never contains model text."""


class _Args(BaseModel):
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)

    @field_validator("*", mode="before")
    @classmethod
    def _blank_is_none(cls, value: Any) -> Any:
        return None if isinstance(value, str) and not value.strip() else value


def _flag(value: Any) -> Any:
    return False if value is None else value


def _zone(value: Any) -> Any:
    if value is None:
        return None
    try:
        ZoneInfo(str(value))
    except Exception:  # noqa: BLE001 - unknown key, bad format, missing tz database
        raise ValueError("unknown timezone") from None
    return str(value)


class CalendarListArgs(_Args):
    pass


class CalendarEventsArgs(_Args):
    scope: Literal["upcoming", "today", "tomorrow", "this_week", "next_week", "next_7_days"] = "upcoming"
    on: str | None = Field(default=None, max_length=100)  # a day, as said: "Friday", "December 12"
    start: str | None = Field(default=None, max_length=100)  # a period's first day
    end: str | None = Field(default=None, max_length=100)  # a period's last day
    day_part: Literal["morning", "afternoon", "evening"] | None = None
    calendar: str | None = Field(default=None, max_length=100)  # a calendar's NAME, never an id
    conflicts: bool = False
    next: bool = False

    _flags = field_validator("conflicts", "next", mode="before")(classmethod(lambda cls, v: _flag(v)))

    @field_validator("scope", mode="before")
    @classmethod
    def _scope(cls, value: Any) -> Any:
        if not isinstance(value, str):
            return "upcoming"
        key = value.strip().lower().replace("-", " ").replace("_", " ")
        return _SCOPE_ALIASES.get(key, key.replace(" ", "_"))

    @field_validator("day_part", mode="before")
    @classmethod
    def _part(cls, value: Any) -> Any:
        return value.strip().lower() if isinstance(value, str) and value.strip().lower() in ("morning", "afternoon", "evening") else None


class CalendarSearchArgs(_Args):
    query: str = Field(min_length=1, max_length=100)
    start: str | None = Field(default=None, max_length=100)
    end: str | None = Field(default=None, max_length=100)
    days: int | None = Field(default=None, ge=1, le=365)  # how far ahead to look; default 90
    calendar: str | None = Field(default=None, max_length=100)
    next: bool = False

    _flag = field_validator("next", mode="before")(classmethod(lambda cls, v: _flag(v)))


class _Target(_Args):
    query: str = Field(min_length=1, max_length=200)  # words identifying the event; matched by code, never an id
    on: str | None = Field(default=None, max_length=100)  # the day it is on, as said
    calendar: str | None = Field(default=None, max_length=100)


class CalendarGetEventArgs(_Target):
    pass


class CalendarCancelEventArgs(_Target):
    whole_series: bool = False  # a repeating event: every occurrence (default: only the one meant)

    _flag = field_validator("whole_series", mode="before")(classmethod(lambda cls, v: _flag(v)))


class CalendarCreateEventArgs(_Args):
    title: str = Field(min_length=1, max_length=300)
    start: str = Field(min_length=1, max_length=120)  # exactly as the user said it
    end: str | None = Field(default=None, max_length=120)  # "until 4 PM"
    duration_minutes: int | None = Field(default=None, ge=1, le=1440)
    all_day: bool = False
    timezone: str | None = Field(default=None, max_length=64)
    location: str | None = Field(default=None, max_length=300)
    description: str | None = Field(default=None, max_length=500)
    attendees: list[str] = Field(default_factory=list, max_length=10)  # e-mail addresses the user GAVE; validated later
    recurrence: str | None = Field(default=None, max_length=150)  # "every Monday at 10 AM"
    repeat_count: int | None = Field(default=None, ge=1, le=730)
    repeat_until: str | None = Field(default=None, max_length=100)
    calendar: str | None = Field(default=None, max_length=100)
    allow_conflict: bool = False  # only after the user was told about an overlap and still wants it

    _flags = field_validator("all_day", "allow_conflict", mode="before")(classmethod(lambda cls, v: _flag(v)))
    _tz = field_validator("timezone", mode="before")(classmethod(lambda cls, v: _zone(v)))

    @field_validator("attendees", mode="before")
    @classmethod
    def _attendees(cls, value: Any) -> Any:
        if value is None:
            return []
        if isinstance(value, str):
            return [value]
        return [str(v).strip() for v in value if isinstance(v, str) and str(v).strip()] if isinstance(value, list) else value

    @model_validator(mode="after")
    def _exclusive(self) -> "CalendarCreateEventArgs":
        if self.repeat_count and self.repeat_until:
            raise ValueError("use a count or an end date, not both")
        if (self.repeat_count or self.repeat_until) and not self.recurrence:
            raise ValueError("a repeat count or end date needs a recurrence")
        return self


class CalendarUpdateEventArgs(_Target):
    new_title: str | None = Field(default=None, max_length=300)
    start: str | None = Field(default=None, max_length=120)  # the NEW start, as said
    end: str | None = Field(default=None, max_length=120)
    duration_minutes: int | None = Field(default=None, ge=1, le=1440)
    location: str | None = Field(default=None, max_length=300)
    clear_location: bool = False
    description: str | None = Field(default=None, max_length=500)
    whole_series: bool = False
    allow_conflict: bool = False

    _flags = field_validator("clear_location", "whole_series", "allow_conflict", mode="before")(classmethod(lambda cls, v: _flag(v)))

    @model_validator(mode="after")
    def _has_change(self) -> "CalendarUpdateEventArgs":
        if not any((self.new_title, self.start, self.end, self.duration_minutes, self.location, self.clear_location, self.description)):
            raise ValueError("say what to change")
        return self


ARGUMENT_MODELS: dict[CalendarActionName, type[BaseModel]] = {
    CalendarActionName.LIST: CalendarListArgs,
    CalendarActionName.EVENTS: CalendarEventsArgs,
    CalendarActionName.SEARCH: CalendarSearchArgs,
    CalendarActionName.GET_EVENT: CalendarGetEventArgs,
    CalendarActionName.CREATE_EVENT: CalendarCreateEventArgs,
    CalendarActionName.UPDATE_EVENT: CalendarUpdateEventArgs,
    CalendarActionName.CANCEL_EVENT: CalendarCancelEventArgs,
}


class CalendarAction(BaseModel):
    """A validated proposal. Executes nothing."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    name: CalendarActionName
    arguments: BaseModel


def parse_calendar_action(raw: object) -> CalendarAction:
    """Validate the model's `action` object. Raises InvalidCalendarAction on anything unexpected."""
    if not isinstance(raw, dict):
        raise InvalidCalendarAction("action is not an object")
    name = raw.get("name")
    if not isinstance(name, str) or name.strip().lower() not in CALENDAR_ACTION_NAMES:
        raise InvalidCalendarAction("unknown action name")
    action = CalendarActionName(name.strip().lower())
    arguments = raw.get("arguments", {})
    if arguments is None:
        arguments = {}
    if not isinstance(arguments, dict):
        raise InvalidCalendarAction("action arguments are not an object")
    if {str(k).strip().lower() for k in arguments} & FORBIDDEN_KEYS:
        raise InvalidCalendarAction("arguments contain a field the model may not supply")
    try:
        parsed = ARGUMENT_MODELS[action].model_validate(arguments)
    except ValidationError as exc:
        problems = ", ".join(sorted({".".join(str(p) for p in e["loc"]) or "arguments" for e in exc.errors()}))
        raise InvalidCalendarAction(f"invalid arguments for {action.value} ({problems})") from None
    return CalendarAction(name=action, arguments=parsed)
