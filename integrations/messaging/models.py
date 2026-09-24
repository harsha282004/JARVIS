"""JARVIS-owned messaging models. Raw provider objects never leave a provider's parser.

Everything in a Message that came from a messaging platform (text, names, titles, file names) is untrusted
external text: it is data to be shown or summarized, never instructions.
"""

from datetime import datetime, timezone
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator


class MessagingError(Exception):
    """Base class. `user_message` is safe to say aloud; the exception text never contains message content,
    tokens or credentials (only the exception type is ever logged)."""

    user_message = "Something went wrong while reading your messages."

    def __init__(self, detail: str = ""):
        super().__init__(detail or self.user_message)


class MessagingNotConfigured(MessagingError):
    user_message = (
        "Messaging isn't set up yet. Create a Telegram bot with BotFather, save its token and enable messaging. "
        "See docs/messaging-integration.md."
    )


class MessagingAuthError(MessagingError):
    user_message = "I couldn't sign in to your messaging provider. Please check the bot token. See docs/messaging-integration.md."


class MessagingAuthRevoked(MessagingAuthError):
    user_message = "The messaging token was revoked or is no longer valid. Please create a new one. See docs/messaging-integration.md."


class MessagingRateLimited(MessagingError):
    user_message = "The messaging provider is rate limiting requests right now. Please try again in a minute."


class MessagingUnavailable(MessagingError):
    user_message = "I can't reach the messaging provider right now. Please check your connection and try again."


class MessagingProviderConflict(MessagingError):
    user_message = (
        "The messaging provider says another program is already reading this bot's messages, or a webhook is set, "
        "so I can't read them. See docs/messaging-integration.md."
    )


class MessagingResponseError(MessagingError):
    user_message = "The messaging provider sent back something I couldn't understand."


class ConversationNotFound(MessagingError):
    user_message = "That conversation can't be found."


class MessageNotFound(MessagingError):
    user_message = "That message can't be found any more."


class UnsupportedCapability(MessagingError):
    """The provider cannot do this. Raised instead of pretending."""

    user_message = "That messaging provider doesn't support that."

    def __init__(self, provider: str = "", capability: str = ""):
        self.provider, self.capability = provider, capability
        super().__init__(f"{provider} does not support {capability}")


class MessagingSummaryUnavailable(MessagingError):
    user_message = "I found the messages but couldn't summarize them right now."


class ConversationKind(StrEnum):
    PRIVATE = "private"
    GROUP = "group"
    CHANNEL = "channel"
    UNKNOWN = "unknown"


class MessageCategory(StrEnum):
    """JARVIS's own heuristic label. It is not the provider's and not objectively correct."""

    IMPORTANT = "important"
    ACTION_REQUIRED = "action_required"
    INFORMATIONAL = "informational"
    PERSONAL = "personal"
    GROUP = "group"
    UNKNOWN = "unknown"


class Capability(StrEnum):
    """What a provider can do. There is deliberately no send, edit or delete capability in this phase."""

    CONVERSATIONS = "conversations"  # list / get conversations
    MESSAGES = "messages"  # recent messages, one message
    SEARCH = "search"  # provider-native message search


def _utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


class Person(BaseModel):
    model_config = ConfigDict(frozen=True)

    person_id: str = Field(default="", max_length=64)
    name: str = Field(default="", max_length=200)
    username: str = Field(default="", max_length=100)
    is_bot: bool = False

    @property
    def display(self) -> str:
        return self.name or (f"@{self.username}" if self.username else "") or "an unknown sender"


class Attachment(BaseModel):
    """Metadata only. JARVIS never downloads, opens or executes an attachment."""

    model_config = ConfigDict(frozen=True)

    attachment_id: str = Field(default="", max_length=256)
    filename: str = Field(default="", max_length=300)
    mime_type: str = Field(default="", max_length=120)
    size: int | None = Field(default=None, ge=0)
    kind: str = Field(default="file", max_length=30)  # file, photo, audio, video, voice, ...


class ReplyRef(BaseModel):
    model_config = ConfigDict(frozen=True)

    message_id: str = Field(max_length=200)
    sender: Person | None = None


class Message(BaseModel):
    message_id: str = Field(min_length=1, max_length=200)
    conversation_id: str = Field(min_length=1, max_length=200)
    provider: str = Field(min_length=1, max_length=40)
    sender: Person | None = None
    recipients: list[Person] = Field(default_factory=list, max_length=50)
    timestamp: datetime | None = None
    text: str = Field(default="", max_length=20000)
    attachments: list[Attachment] = Field(default_factory=list, max_length=50)
    reply_to: ReplyRef | None = None
    conversation_title: str = Field(default="", max_length=200)
    conversation_kind: ConversationKind = ConversationKind.UNKNOWN
    is_unread: bool | None = None  # None: the provider cannot say
    source: dict[str, str] = Field(default_factory=dict)  # small, non-sensitive provider metadata (e.g. forwarded)

    @field_validator("timestamp")
    @classmethod
    def _aware(cls, value: datetime | None) -> datetime | None:
        return _utc(value)

    @property
    def has_attachments(self) -> bool:
        return bool(self.attachments)


class Conversation(BaseModel):
    conversation_id: str = Field(min_length=1, max_length=200)
    provider: str = Field(min_length=1, max_length=40)
    title: str = Field(default="", max_length=200)
    kind: ConversationKind = ConversationKind.UNKNOWN
    participants: list[Person] = Field(default_factory=list, max_length=50)
    last_message_at: datetime | None = None
    unread_count: int | None = Field(default=None, ge=0)  # None: the provider cannot say

    @field_validator("last_message_at")
    @classmethod
    def _aware(cls, value: datetime | None) -> datetime | None:
        return _utc(value)

    @property
    def display(self) -> str:
        if self.title:
            return self.title
        names = [p.display for p in self.participants[:3]]
        return ", ".join(names) or "an unnamed conversation"


class MessageQuery(BaseModel):
    """A structured search. Plain words only: no operators, ids or provider syntax are ever passed through."""

    model_config = ConfigDict(frozen=True)

    text: str = Field(default="", max_length=100)
    sender: str = Field(default="", max_length=100)
    conversation_id: str | None = None
    since: datetime | None = None
    until: datetime | None = None
    limit: int = Field(default=20, ge=1, le=100)


class MessagePage(BaseModel):
    """Newest first. `truncated`: more may exist than were read."""

    messages: list[Message] = Field(default_factory=list)
    truncated: bool = False
    # "provider": the provider searched its full history. "recent_window": only the messages the provider lets
    # JARVIS read (e.g. Telegram bots see the last ~24 hours), filtered locally.
    scope: str = "provider"

    @property
    def count(self) -> int:
        return len(self.messages)


class ProviderIdentity(BaseModel):
    provider: str
    account: str = ""  # a non-secret label (e.g. the bot's @username)


class ClassificationResult(BaseModel):
    category: MessageCategory
    reasons: list[str] = Field(default_factory=list)


class ActionCandidate(BaseModel):
    """Something a message seems to ask the reader to do, quoted from the message. Nothing is created from it."""

    text: str
    deadline_text: str | None = None
