"""The structured Gmail action the LLM may propose (models only; nothing here touches Gmail).

The model supplies words: a search query and flags. It can never supply a message or thread id, an
API URL, an HTTP method, a token, a path or a command: such keys make the action invalid, and the
query is rebuilt from a whitelist of read-only Gmail search operators. Messages are identified by code
(search, then ask if ambiguous), never by an id the model made up.
"""

from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from integrations.gmail.models import GmailQueryError
from integrations.gmail.query import sanitize_query


class GmailActionName(StrEnum):
    SEARCH = "gmail_search"
    GET_MESSAGE = "gmail_get_message"
    GET_THREAD = "gmail_get_thread"
    SUMMARIZE = "gmail_summarize"
    CLASSIFY = "gmail_classify"


GMAIL_ACTION_NAMES = frozenset(a.value for a in GmailActionName)

# Keys the model must never supply. Their presence makes the whole action invalid.
FORBIDDEN_KEYS = frozenset({
    "id", "message_id", "thread_id", "messageid", "threadid", "attachment_id", "url", "uri", "endpoint", "api",
    "method", "http_method", "headers", "token", "access_token", "refresh_token", "authorization", "credentials",
    "path", "filepath", "file", "command", "cmd", "shell", "sql", "scope", "scopes", "to", "recipient", "body",
})


class InvalidGmailAction(ValueError):
    """The proposed action is not a valid Gmail action. The message never contains model text."""


class _Args(BaseModel):
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)

    @field_validator("*", mode="before")
    @classmethod
    def _blank_is_none(cls, value: Any) -> Any:
        return None if isinstance(value, str) and not value.strip() else value


class _Query(_Args):
    query: str = Field(default="", max_length=300)

    @field_validator("query", mode="before")
    @classmethod
    def _valid_query(cls, value: Any) -> str:
        if value is None:
            return ""
        if not isinstance(value, str):
            raise ValueError("query must be text")
        try:
            return sanitize_query(value)
        except GmailQueryError:
            raise ValueError("unsupported search") from None


class GmailSearchArgs(_Query):
    max_results: int | None = Field(default=None, ge=1, le=50)


class _Target(_Query):
    """Identifies one email by words. `latest` picks the newest match instead of asking which one."""

    latest: bool = False

    @field_validator("latest", mode="before")
    @classmethod
    def _latest(cls, value: Any) -> Any:
        return False if value is None else value

    @model_validator(mode="after")
    def _needs_something(self) -> "_Target":
        if not self.query and not self.latest:
            raise ValueError("describe the email or ask for the latest one")
        return self


class GmailGetMessageArgs(_Target):
    pass


class GmailGetThreadArgs(_Target):
    pass


class GmailClassifyArgs(_Target):
    pass


class GmailSummarizeArgs(_Target):
    thread: bool = False
    focus: Literal["summary", "action_items", "key_points"] = "summary"

    @field_validator("thread", mode="before")
    @classmethod
    def _thread(cls, value: Any) -> Any:
        return False if value is None else value

    @field_validator("focus", mode="before")
    @classmethod
    def _focus(cls, value: Any) -> Any:
        aliases = {"action": "action_items", "actions": "action_items", "points": "key_points", "important": "key_points"}
        return aliases.get(value.strip().lower(), value.strip().lower()) if isinstance(value, str) else "summary"


ARGUMENT_MODELS: dict[GmailActionName, type[BaseModel]] = {
    GmailActionName.SEARCH: GmailSearchArgs,
    GmailActionName.GET_MESSAGE: GmailGetMessageArgs,
    GmailActionName.GET_THREAD: GmailGetThreadArgs,
    GmailActionName.SUMMARIZE: GmailSummarizeArgs,
    GmailActionName.CLASSIFY: GmailClassifyArgs,
}


class GmailAction(BaseModel):
    """A validated proposal. Executes nothing."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    name: GmailActionName
    arguments: BaseModel


def parse_gmail_action(raw: object) -> GmailAction:
    """Validate the model's `action` object. Raises InvalidGmailAction on anything unexpected."""
    if not isinstance(raw, dict):
        raise InvalidGmailAction("action is not an object")
    name = raw.get("name")
    if not isinstance(name, str) or name.strip().lower() not in GMAIL_ACTION_NAMES:
        raise InvalidGmailAction("unknown action name")
    action = GmailActionName(name.strip().lower())
    arguments = raw.get("arguments", {})
    if arguments is None:
        arguments = {}
    if not isinstance(arguments, dict):
        raise InvalidGmailAction("action arguments are not an object")
    if {str(k).strip().lower() for k in arguments} & FORBIDDEN_KEYS:
        raise InvalidGmailAction("arguments contain a field the model may not supply")
    try:
        parsed = ARGUMENT_MODELS[action].model_validate(arguments)
    except ValidationError as exc:
        problems = ", ".join(sorted({".".join(str(p) for p in e["loc"]) or "arguments" for e in exc.errors()}))
        raise InvalidGmailAction(f"invalid arguments for {action.value} ({problems})") from None
    return GmailAction(name=action, arguments=parsed)
