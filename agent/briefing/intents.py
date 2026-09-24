"""The structured briefing actions the LLM may propose. Words only; nothing here reads or changes any data.

The model chooses WHAT to show (a view, a window, a level of detail). It can never supply an item id or key, a source id, a
task/event/message id, a URL, a path, a command, SQL, or anything to create, send, complete, modify or delete: such keys make the
action invalid. Briefings are read-only, and the priorities, sources and wording are decided by code, not by the model.
"""

import re
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from agent.briefing.models import BriefingWindow, Detail, View


class BriefingActionName(StrEnum):
    GENERATE = "briefing_generate"
    EXPLAIN = "briefing_explain"


BRIEFING_ACTION_NAMES = frozenset(a.value for a in BriefingActionName)

FORBIDDEN_KEYS = frozenset({
    "id", "item_id", "key", "source_id", "task_id", "event_id", "message_id", "reminder_id", "url", "uri", "endpoint", "api", "method",
    "headers", "token", "access_token", "authorization", "credentials", "path", "filepath", "file", "command", "cmd", "shell", "sql",
    "send", "create", "complete", "modify", "update", "delete", "cancel", "reschedule", "to", "recipient", "body", "text", "content",
    "prompt", "instructions", "provider", "priority", "level", "score", "settings", "config",
})

_VIEWS = {
    "overview": View.OVERVIEW, "briefing": View.OVERVIEW, "morning": View.OVERVIEW, "morning briefing": View.OVERVIEW, "day": View.OVERVIEW, "summary": View.OVERVIEW,
    "schedule": View.SCHEDULE, "calendar": View.SCHEDULE, "agenda": View.SCHEDULE, "meetings": View.SCHEDULE, "events": View.SCHEDULE,
    "tasks": View.TASKS, "task": View.TASKS, "todo": View.TASKS, "to-do": View.TASKS, "to do": View.TASKS,
    "deadlines": View.DEADLINES, "deadline": View.DEADLINES, "due": View.DEADLINES,
    "priorities": View.PRIORITIES, "priority": View.PRIORITIES,
    "focus": View.FOCUS, "next": View.NEXT, "whats next": View.NEXT, "what's next": View.NEXT, "upcoming": View.NEXT,
    "missed": View.MISSED, "miss": View.MISSED, "catch up": View.MISSED, "catch-up": View.MISSED,
    "prepare": View.PREPARE, "preparation": View.PREPARE, "prep": View.PREPARE,
}
_WINDOWS = {
    "today": BriefingWindow.TODAY, "now": BriefingWindow.TODAY, "tomorrow": BriefingWindow.TOMORROW, "this week": BriefingWindow.THIS_WEEK, "this_week": BriefingWindow.THIS_WEEK,
    "week": BriefingWindow.THIS_WEEK, "next 7 days": BriefingWindow.NEXT_7_DAYS, "next_7_days": BriefingWindow.NEXT_7_DAYS, "next week": BriefingWindow.NEXT_7_DAYS,
    "yesterday": BriefingWindow.YESTERDAY, "last 24 hours": BriefingWindow.LAST_24_HOURS, "last_24_hours": BriefingWindow.LAST_24_HOURS,
    "recent": BriefingWindow.LAST_24_HOURS, "while away": BriefingWindow.LAST_24_HOURS,
}
_DETAILS = {
    "quick": Detail.QUICK, "brief": Detail.QUICK, "short": Detail.QUICK, "normal": Detail.NORMAL, "standard": Detail.NORMAL,
    "detailed": Detail.DETAILED, "detail": Detail.DETAILED, "full": Detail.DETAILED, "everything": Detail.DETAILED, "long": Detail.DETAILED,
}
_CONTROL = re.compile(r"[\x00-\x1f\x7f<>]")


class InvalidBriefingAction(ValueError):
    """The proposed action is not a valid briefing action. The message never contains model text."""


def _key(value: Any) -> str:
    return " ".join(str(value).strip().lower().replace("_", " ").split()) if isinstance(value, str) else ""


class _Args(BaseModel):
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)

    @field_validator("*", mode="before")
    @classmethod
    def _blank_is_none(cls, value: Any) -> Any:
        return None if isinstance(value, str) and not value.strip() else value


class BriefingGenerateArgs(_Args):
    view: View = View.OVERVIEW
    window: BriefingWindow | None = None  # default: today (yesterday for the missed view)
    detail: Detail = Detail.NORMAL
    day_part: Literal["morning", "afternoon", "evening"] | None = None

    @field_validator("view", mode="before")
    @classmethod
    def _view(cls, value: Any) -> Any:
        if value is None:
            return View.OVERVIEW
        k = _key(value)
        for candidate in (k, k.replace("-", " "), k.replace("'", "")):
            if candidate in _VIEWS:
                return _VIEWS[candidate]
        raise ValueError("unknown view")

    @field_validator("window", mode="before")
    @classmethod
    def _window(cls, value: Any) -> Any:
        if value is None:
            return None
        try:
            return _WINDOWS[_key(value)]
        except KeyError:
            raise ValueError("unknown window") from None

    @field_validator("detail", mode="before")
    @classmethod
    def _detail(cls, value: Any) -> Any:
        if value is None:
            return Detail.NORMAL
        try:
            return _DETAILS[_key(value)]
        except KeyError:
            raise ValueError("unknown detail") from None

    @field_validator("day_part", mode="before")
    @classmethod
    def _part(cls, value: Any) -> Any:
        return _key(value) if _key(value) in ("morning", "afternoon", "evening") else None


class BriefingExplainArgs(_Args):
    query: str | None = Field(default=None, max_length=100)  # words from the item the user means
    aspect: Literal["source", "reason", "both"] = "both"

    @field_validator("query", mode="before")
    @classmethod
    def _plain(cls, value: Any) -> Any:
        return " ".join(_CONTROL.sub(" ", value).split()) or None if isinstance(value, str) else value

    @field_validator("aspect", mode="before")
    @classmethod
    def _aspect(cls, value: Any) -> Any:
        return {"source": "source", "where": "source", "reason": "reason", "why": "reason"}.get(_key(value), "both")


ARGUMENT_MODELS: dict[BriefingActionName, type[BaseModel]] = {
    BriefingActionName.GENERATE: BriefingGenerateArgs,
    BriefingActionName.EXPLAIN: BriefingExplainArgs,
}


class BriefingAction(BaseModel):
    """A validated proposal. Executes nothing."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    name: BriefingActionName
    arguments: BaseModel


def parse_briefing_action(raw: object) -> BriefingAction:
    if not isinstance(raw, dict):
        raise InvalidBriefingAction("action is not an object")
    name = raw.get("name")
    if not isinstance(name, str) or name.strip().lower() not in BRIEFING_ACTION_NAMES:
        raise InvalidBriefingAction("unknown action name")
    action = BriefingActionName(name.strip().lower())
    arguments = raw.get("arguments", {})
    if arguments is None:
        arguments = {}
    if not isinstance(arguments, dict):
        raise InvalidBriefingAction("action arguments are not an object")
    if {str(k).strip().lower() for k in arguments} & FORBIDDEN_KEYS:
        raise InvalidBriefingAction("arguments contain a field the model may not supply")
    try:
        parsed = ARGUMENT_MODELS[action].model_validate(arguments)
    except ValidationError as exc:
        problems = ", ".join(sorted({".".join(str(p) for p in e["loc"]) or "arguments" for e in exc.errors()}))
        raise InvalidBriefingAction(f"invalid arguments for {action.value} ({problems})") from None
    return BriefingAction(name=action, arguments=parsed)
