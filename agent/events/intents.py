"""The structured event action the LLM may propose (models only; nothing here touches the database or any source).

The model supplies words: a title, a time exactly as the user said it, a description of which event it means.
Times are resolved by code (`resolve_when`); events, tasks, emails, documents and memories are identified by code
(search, then ask if ambiguous). The model can never supply an id, a status, a confidence, a source, a URL, a path,
a token, a command or SQL: such keys make the action invalid.
"""

from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from agent.events.models import EventType
from agent.tasks.models import TaskPriority
from integrations.gmail.models import GmailQueryError
from integrations.gmail.query import sanitize_query


class EventActionName(StrEnum):
    CREATE = "event_create"
    LIST = "event_list"
    SEARCH = "event_search"
    GET = "event_get"
    COMPLETE = "event_complete"
    CANCEL = "event_cancel"
    UPDATE = "event_update"
    EXTRACT = "event_extract"


EVENT_ACTION_NAMES = frozenset(a.value for a in EventActionName)

FORBIDDEN_KEYS = frozenset({
    "id", "event_id", "eventid", "task_id", "taskid", "message_id", "thread_id", "gmail_id", "document_id", "doc_id",
    "memory_id", "source_id", "database_id", "db_id", "attachment_id", "status", "confidence", "source_type",
    "dedupe_key", "created_at", "updated_at", "url", "uri", "endpoint", "method", "headers", "token", "access_token",
    "credentials", "path", "filepath", "file", "command", "cmd", "shell", "sql", "scope_id",
})

_TYPE_ALIASES = {
    "due": "deadline", "submission": "deadline", "deadlines": "deadline", "test": "exam", "quiz": "exam", "midterm": "exam",
    "call": "meeting", "meetings": "meeting", "interviews": "interview", "exams": "exam", "assignments": "assignment",
    "homework": "assignment", "applications": "application", "appointments": "appointment", "events": "event",
}
_SCOPE_ALIASES = {
    "week": "this_week", "this week": "this_week", "thisweek": "this_week", "next week": "next_week", "nextweek": "next_week",
    "7 days": "next_7_days", "next 7 days": "next_7_days", "seven days": "next_7_days", "coming up": "upcoming",
    "deadlines": "upcoming", "soon": "upcoming", "important": "all", "everything": "all", "overdue deadlines": "overdue",
    "missed": "overdue", "late": "overdue",
}


class InvalidEventAction(ValueError):
    """The proposed action is not a valid event action. The message never contains model text."""


class _Args(BaseModel):
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)

    @field_validator("*", mode="before")
    @classmethod
    def _blank_is_none(cls, value: Any) -> Any:
        return None if isinstance(value, str) and not value.strip() else value


def _event_type(value: Any) -> Any:
    if isinstance(value, EventType) or value is None:
        return value
    if isinstance(value, str):
        key = value.strip().lower()
        key = _TYPE_ALIASES.get(key, key)
        return EventType(key) if key in EventType._value2member_map_ else None  # unknown type: inferred from the words
    return None


def _priority(value: Any) -> Any:
    if isinstance(value, str):  # an unrecognised priority word is ignored (never guessed)
        return TaskPriority.__members__.get(value.strip().upper())
    return value if isinstance(value, TaskPriority) else None


def _flag(value: Any) -> Any:
    return False if value is None else value


class EventCreateArgs(_Args):
    title: str | None = Field(default=None, max_length=200)
    when: str | None = Field(default=None, max_length=120)  # exactly as the user said it; parsed by code
    duration_minutes: int | None = Field(default=None, ge=1, le=1440)
    type: EventType | None = None
    description: str | None = Field(default=None, max_length=300)
    priority: TaskPriority | None = None
    task_query: str | None = Field(default=None, max_length=200)  # words identifying an existing task to link
    project: str | None = Field(default=None, max_length=80)  # an existing project/goal in the knowledge graph
    person: str | None = Field(default=None, max_length=80)  # an existing person in the knowledge graph

    _type = field_validator("type", mode="before")(classmethod(lambda cls, v: _event_type(v)))
    _priority = field_validator("priority", mode="before")(classmethod(lambda cls, v: _priority(v)))

    @model_validator(mode="after")
    def _enough(self) -> "EventCreateArgs":
        if not self.title and not self.task_query:
            raise ValueError("a title or a task to take it from is required")
        if not self.when and not self.task_query:
            raise ValueError("a time is required")
        return self


class EventListArgs(_Args):
    scope: Literal["upcoming", "today", "tomorrow", "this_week", "next_week", "next_7_days", "overdue", "all"] = "upcoming"
    type: EventType | None = None
    next: bool = False  # only the next one ("when is my next interview?")
    conflicts: bool = False  # say whether the listed events overlap

    _type = field_validator("type", mode="before")(classmethod(lambda cls, v: _event_type(v)))
    _flags = field_validator("next", "conflicts", mode="before")(classmethod(lambda cls, v: _flag(v)))

    @field_validator("scope", mode="before")
    @classmethod
    def _scope(cls, value: Any) -> Any:
        if not isinstance(value, str):
            return "upcoming"
        key = value.strip().lower().replace("-", " ")
        return _SCOPE_ALIASES.get(key, key.replace(" ", "_"))


class _Query(_Args):
    query: str = Field(min_length=1, max_length=200)  # words describing the event; matched by code, never an id


class EventSearchArgs(_Query):
    type: EventType | None = None
    include_past: bool = False

    _type = field_validator("type", mode="before")(classmethod(lambda cls, v: _event_type(v)))
    _flag = field_validator("include_past", mode="before")(classmethod(lambda cls, v: _flag(v)))


class EventGetArgs(_Query):
    pass


class EventCompleteArgs(_Query):
    pass


class EventCancelArgs(_Query):
    pass


class EventUpdateArgs(_Query):
    title: str | None = Field(default=None, max_length=200)
    when: str | None = Field(default=None, max_length=120)
    duration_minutes: int | None = Field(default=None, ge=1, le=1440)
    type: EventType | None = None
    priority: TaskPriority | None = None
    description: str | None = Field(default=None, max_length=300)
    task_query: str | None = Field(default=None, max_length=200)
    confirm: bool = False  # the user confirms an unconfirmed (low-confidence) event

    _type = field_validator("type", mode="before")(classmethod(lambda cls, v: _event_type(v)))
    _priority = field_validator("priority", mode="before")(classmethod(lambda cls, v: _priority(v)))
    _flag = field_validator("confirm", mode="before")(classmethod(lambda cls, v: _flag(v)))

    @model_validator(mode="after")
    def _has_change(self) -> "EventUpdateArgs":
        if not any((self.title, self.when, self.duration_minutes, self.type, self.priority, self.description,
                    self.task_query, self.confirm)):
            raise ValueError("say what to change")
        return self


class EventExtractArgs(_Args):
    source: Literal["gmail", "document", "memory"]
    query: str | None = Field(default=None, max_length=300)  # gmail: search words; document: file name words; memory: words
    latest: bool = False  # gmail: the newest matching email

    _flag = field_validator("latest", mode="before")(classmethod(lambda cls, v: _flag(v)))

    @field_validator("source", mode="before")
    @classmethod
    def _source(cls, value: Any) -> Any:
        aliases = {"email": "gmail", "mail": "gmail", "emails": "gmail", "doc": "document", "file": "document", "documents": "document", "memories": "memory"}
        return aliases.get(value.strip().lower(), value.strip().lower()) if isinstance(value, str) else value

    @model_validator(mode="after")
    def _check(self) -> "EventExtractArgs":
        if self.source == "gmail":
            try:
                self.query = sanitize_query(self.query or "")
            except GmailQueryError:
                raise ValueError("unsupported search") from None
            if not self.query and not self.latest:
                raise ValueError("describe the email or ask for the latest one")
        elif not self.query:
            raise ValueError("say which document or memory")
        return self


ARGUMENT_MODELS: dict[EventActionName, type[BaseModel]] = {
    EventActionName.CREATE: EventCreateArgs,
    EventActionName.LIST: EventListArgs,
    EventActionName.SEARCH: EventSearchArgs,
    EventActionName.GET: EventGetArgs,
    EventActionName.COMPLETE: EventCompleteArgs,
    EventActionName.CANCEL: EventCancelArgs,
    EventActionName.UPDATE: EventUpdateArgs,
    EventActionName.EXTRACT: EventExtractArgs,
}


class EventAction(BaseModel):
    """A validated proposal. Executes nothing."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    name: EventActionName
    arguments: BaseModel


def parse_event_action(raw: object) -> EventAction:
    """Validate the model's `action` object. Raises InvalidEventAction on anything unexpected."""
    if not isinstance(raw, dict):
        raise InvalidEventAction("action is not an object")
    name = raw.get("name")
    if not isinstance(name, str) or name.strip().lower() not in EVENT_ACTION_NAMES:
        raise InvalidEventAction("unknown action name")
    action = EventActionName(name.strip().lower())
    arguments = raw.get("arguments", {})
    if arguments is None:
        arguments = {}
    if not isinstance(arguments, dict):
        raise InvalidEventAction("action arguments are not an object")
    if {str(k).strip().lower() for k in arguments} & FORBIDDEN_KEYS:
        raise InvalidEventAction("arguments contain a field the model may not supply")
    try:
        parsed = ARGUMENT_MODELS[action].model_validate(arguments)
    except ValidationError as exc:
        problems = ", ".join(sorted({".".join(str(p) for p in e["loc"]) or "arguments" for e in exc.errors()}))
        raise InvalidEventAction(f"invalid arguments for {action.value} ({problems})") from None
    return EventAction(name=action, arguments=parsed)
