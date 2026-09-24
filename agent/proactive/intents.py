"""The structured proactive action the LLM may propose: only `proactive_explain` ("why did you notify me?").

Words only. The model can never supply a notification id, source id, path, URL, command or SQL: such keys make the action
invalid. It is read-only: it reads JARVIS's own notification history and changes nothing.
"""

import re
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator


class ProactiveActionName(StrEnum):
    EXPLAIN = "proactive_explain"


PROACTIVE_ACTION_NAMES = frozenset(a.value for a in ProactiveActionName)

FORBIDDEN_KEYS = frozenset({
    "id", "notification_id", "signal_id", "candidate_id", "source_id", "dedupe_key", "url", "uri", "endpoint", "api", "method",
    "headers", "token", "access_token", "authorization", "credentials", "path", "filepath", "file", "command", "cmd", "shell",
    "sql", "enable", "disable", "enabled", "setting", "config", "quiet_hours", "cooldown", "priority", "urgency", "channel", "channels",
})
_CONTROL = re.compile(r"[\x00-\x1f\x7f<>]")


class InvalidProactiveAction(ValueError):
    """The proposed action is not a valid proactive action. The message never contains model text."""


class ProactiveExplainArgs(BaseModel):
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)

    query: str | None = Field(default=None, max_length=100)  # words from the notification the user means
    limit: int | None = Field(default=None, ge=1, le=5)

    @field_validator("query", mode="before")
    @classmethod
    def _plain(cls, value: Any) -> Any:
        if isinstance(value, str):
            return " ".join(_CONTROL.sub(" ", value).split()) or None
        return value


class ProactiveAction(BaseModel):
    """A validated proposal. Executes nothing."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    name: ProactiveActionName
    arguments: BaseModel


def parse_proactive_action(raw: object) -> ProactiveAction:
    if not isinstance(raw, dict):
        raise InvalidProactiveAction("action is not an object")
    name = raw.get("name")
    if not isinstance(name, str) or name.strip().lower() not in PROACTIVE_ACTION_NAMES:
        raise InvalidProactiveAction("unknown action name")
    arguments = raw.get("arguments", {})
    if arguments is None:
        arguments = {}
    if not isinstance(arguments, dict):
        raise InvalidProactiveAction("action arguments are not an object")
    if {str(k).strip().lower() for k in arguments} & FORBIDDEN_KEYS:
        raise InvalidProactiveAction("arguments contain a field the model may not supply")
    try:
        parsed = ProactiveExplainArgs.model_validate(arguments)
    except ValidationError as exc:
        problems = ", ".join(sorted({".".join(str(p) for p in e["loc"]) or "arguments" for e in exc.errors()}))
        raise InvalidProactiveAction(f"invalid arguments ({problems})") from None
    return ProactiveAction(name=ProactiveActionName.EXPLAIN, arguments=parsed)
