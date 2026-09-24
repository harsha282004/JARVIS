"""The structured messaging action the LLM may propose (models only; nothing here touches a messaging provider).

The model supplies words: who, which conversation, a topic and a day. It can never supply a message or conversation id,
a provider, an API URL, a token or cookie, a method, a path, a command, SQL or any text to send: such keys make the
action invalid. Messages and conversations are identified by code (read, then ask if ambiguous), never by an id the
model made up. All actions are read-only: there is no send, reply, edit or delete action.
"""

import re
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator


class MessageActionName(StrEnum):
    LIST = "message_list"
    SEARCH = "message_search"
    GET = "message_get"
    CONVERSATION_LIST = "conversation_list"
    CONVERSATION_GET = "conversation_get"
    SUMMARIZE = "message_summarize"


MESSAGE_ACTION_NAMES = frozenset(a.value for a in MessageActionName)

# Keys the model must never supply. Their presence makes the whole action invalid.
FORBIDDEN_KEYS = frozenset({
    "id", "message_id", "messageid", "conversation_id", "conversationid", "chat_id", "chatid", "thread_id", "user_id",
    "attachment_id", "file_id", "provider", "url", "uri", "endpoint", "api", "method", "http_method", "headers", "params",
    "token", "bot_token", "access_token", "refresh_token", "cookie", "cookies", "session", "session_id", "authorization",
    "credentials", "path", "filepath", "file", "command", "cmd", "shell", "sql", "to", "recipient", "recipients",
    "reply", "reply_to", "send", "body", "content", "text", "offset", "webhook",
})

_SCOPE_ALIASES = {
    "latest": "latest", "recent": "latest", "newest": "latest", "last": "latest", "now": "latest", "new": "latest",
    "today": "today", "yesterday": "yesterday", "week": "this_week", "this week": "this_week", "this_week": "this_week",
}
_CONTROL = re.compile(r"[\x00-\x1f\x7f<>]")


class InvalidMessageAction(ValueError):
    """The proposed action is not a valid messaging action. The message never contains model text."""


class _Args(BaseModel):
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)

    @field_validator("*", mode="before")
    @classmethod
    def _blank_is_none(cls, value: Any) -> Any:
        return None if isinstance(value, str) and not value.strip() else value


def _plain(value: Any) -> Any:
    """Words only: control characters and angle brackets are dropped, so a description can never carry markup."""
    return " ".join(_CONTROL.sub(" ", value).split()) if isinstance(value, str) else value


def _flag(value: Any) -> Any:
    return False if value is None else value


class _Scoped(_Args):
    scope: Literal["latest", "today", "yesterday", "this_week"] = "latest"
    on: str | None = Field(default=None, max_length=100)  # a day, as said: "Friday", "March 5"

    @field_validator("scope", mode="before")
    @classmethod
    def _scope(cls, value: Any) -> Any:
        if not isinstance(value, str):
            return "latest"
        return _SCOPE_ALIASES.get(value.strip().lower().replace("-", " "), "latest")

    @field_validator("on", mode="before")
    @classmethod
    def _on(cls, value: Any) -> Any:
        return _plain(value)


class _Who(_Scoped):
    sender: str | None = Field(default=None, max_length=100)  # a person's name, as said
    conversation: str | None = Field(default=None, max_length=100)  # a conversation/group NAME, never an id

    @field_validator("sender", "conversation", mode="before")
    @classmethod
    def _names(cls, value: Any) -> Any:
        return _plain(value)


class MessageListArgs(_Who):
    limit: int | None = Field(default=None, ge=1, le=50)


class MessageSearchArgs(_Who):
    query: str = Field(min_length=1, max_length=100)
    limit: int | None = Field(default=None, ge=1, le=50)

    @field_validator("query", mode="before")
    @classmethod
    def _query(cls, value: Any) -> Any:
        return _plain(value)


class MessageGetArgs(_Who):
    query: str | None = Field(default=None, max_length=100)
    latest: bool = False

    _latest = field_validator("latest", mode="before")(classmethod(lambda cls, v: _flag(v)))

    @field_validator("query", mode="before")
    @classmethod
    def _query(cls, value: Any) -> Any:
        return _plain(value)

    @model_validator(mode="after")
    def _needs_something(self) -> "MessageGetArgs":
        if not (self.query or self.sender or self.conversation or self.latest):
            raise ValueError("describe the message or ask for the latest one")
        return self


class ConversationListArgs(_Args):
    query: str | None = Field(default=None, max_length=100)  # part of a conversation's name
    limit: int | None = Field(default=None, ge=1, le=50)

    @field_validator("query", mode="before")
    @classmethod
    def _query(cls, value: Any) -> Any:
        return _plain(value)


class ConversationGetArgs(_Args):
    conversation: str | None = Field(default=None, max_length=100)
    latest: bool = False
    limit: int | None = Field(default=None, ge=1, le=50)

    _latest = field_validator("latest", mode="before")(classmethod(lambda cls, v: _flag(v)))

    @field_validator("conversation", mode="before")
    @classmethod
    def _name(cls, value: Any) -> Any:
        return _plain(value)

    @model_validator(mode="after")
    def _needs_something(self) -> "ConversationGetArgs":
        if not (self.conversation or self.latest):
            raise ValueError("name the conversation or ask for the latest one")
        return self


class MessageSummarizeArgs(_Who):
    query: str | None = Field(default=None, max_length=100)
    focus: Literal["summary", "action_items", "key_points"] = "summary"

    @field_validator("query", mode="before")
    @classmethod
    def _query(cls, value: Any) -> Any:
        return _plain(value)

    @field_validator("focus", mode="before")
    @classmethod
    def _focus(cls, value: Any) -> Any:
        aliases = {"action": "action_items", "actions": "action_items", "asking": "action_items", "points": "key_points", "important": "key_points"}
        return aliases.get(value.strip().lower(), value.strip().lower()) if isinstance(value, str) else "summary"


ARGUMENT_MODELS: dict[MessageActionName, type[BaseModel]] = {
    MessageActionName.LIST: MessageListArgs,
    MessageActionName.SEARCH: MessageSearchArgs,
    MessageActionName.GET: MessageGetArgs,
    MessageActionName.CONVERSATION_LIST: ConversationListArgs,
    MessageActionName.CONVERSATION_GET: ConversationGetArgs,
    MessageActionName.SUMMARIZE: MessageSummarizeArgs,
}


class MessageAction(BaseModel):
    """A validated proposal. Executes nothing."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    name: MessageActionName
    arguments: BaseModel


def parse_message_action(raw: object) -> MessageAction:
    """Validate the model's `action` object. Raises InvalidMessageAction on anything unexpected."""
    if not isinstance(raw, dict):
        raise InvalidMessageAction("action is not an object")
    name = raw.get("name")
    if not isinstance(name, str) or name.strip().lower() not in MESSAGE_ACTION_NAMES:
        raise InvalidMessageAction("unknown action name")
    action = MessageActionName(name.strip().lower())
    arguments = raw.get("arguments", {})
    if arguments is None:
        arguments = {}
    if not isinstance(arguments, dict):
        raise InvalidMessageAction("action arguments are not an object")
    if {str(k).strip().lower() for k in arguments} & FORBIDDEN_KEYS:
        raise InvalidMessageAction("arguments contain a field the model may not supply")
    try:
        parsed = ARGUMENT_MODELS[action].model_validate(arguments)
    except ValidationError as exc:
        problems = ", ".join(sorted({".".join(str(p) for p in e["loc"]) or "arguments" for e in exc.errors()}))
        raise InvalidMessageAction(f"invalid arguments for {action.value} ({problems})") from None
    return MessageAction(name=action, arguments=parsed)
