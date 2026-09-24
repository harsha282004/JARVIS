"""JARVIS-owned Gmail models. Raw Gmail API objects never leave the parser.

Everything in a GmailMessage that came from the mailbox (subject, sender, body, filenames) is
untrusted external text: it is data to be summarized, never instructions.
"""

from datetime import datetime, timezone
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class GmailError(Exception):
    """Base class. `user_message` is safe to say aloud; the exception text never contains email
    content, tokens or credentials (only the exception type is ever logged)."""

    user_message = "Something went wrong while talking to Gmail."

    def __init__(self, detail: str = ""):
        super().__init__(detail or self.user_message)


class GmailNotConfigured(GmailError):
    user_message = (
        "Gmail isn't set up yet. Add your Google OAuth client file and run python scripts/gmail_cli.py auth. "
        "See docs/gmail-intelligence.md."
    )


class GmailAuthError(GmailError):
    user_message = "I couldn't sign in to Gmail. Please run python scripts/gmail_cli.py auth again."


class GmailAuthRevoked(GmailAuthError):
    user_message = "Gmail access was revoked or has expired. Please run python scripts/gmail_cli.py auth again."


class GmailPermissionDenied(GmailError):
    user_message = "Google didn't allow that request. The Gmail read-only permission may be missing."


class GmailRateLimited(GmailError):
    user_message = "Gmail is rate limiting requests right now. Please try again in a minute."


class GmailUnavailable(GmailError):
    user_message = "I can't reach Gmail right now. Please check your connection and try again."


class GmailNotFound(GmailError):
    user_message = "That email no longer exists or can't be found."


class GmailResponseError(GmailError):
    user_message = "Gmail sent back something I couldn't understand."


class GmailQueryError(GmailError):
    user_message = "I couldn't turn that into a valid Gmail search."


class EmailCategory(StrEnum):
    """JARVIS's own heuristic label. It is not Gmail's and not objectively correct."""

    IMPORTANT = "important"
    ACTION_REQUIRED = "action_required"
    INFORMATIONAL = "informational"
    PROMOTIONAL = "promotional"
    PERSONAL = "personal"
    UNKNOWN = "unknown"


class GmailAddress(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str = ""
    email: str = ""

    @property
    def display(self) -> str:
        return self.name or self.email or "an unknown sender"


class GmailAttachment(BaseModel):
    """Metadata only. Attachments are never downloaded, opened or executed."""

    filename: str
    mime_type: str = "application/octet-stream"
    size: int = Field(default=0, ge=0)
    attachment_id: str | None = None
    message_id: str = ""


class GmailMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    message_id: str
    thread_id: str
    sender: GmailAddress | None = None
    recipients: list[GmailAddress] = Field(default_factory=list)
    cc: list[GmailAddress] = Field(default_factory=list)
    bcc: list[GmailAddress] = Field(default_factory=list)
    subject: str = ""
    timestamp: datetime | None = None
    labels: list[str] = Field(default_factory=list)
    snippet: str = ""
    plain_text_body: str = ""
    html_body: str | None = None  # kept as text only; never rendered or executed
    attachments: list[GmailAttachment] = Field(default_factory=list)
    headers: dict[str, str] = Field(default_factory=dict)  # only the few headers intelligence needs

    @property
    def is_unread(self) -> bool:
        return "UNREAD" in self.labels

    @property
    def has_attachments(self) -> bool:
        return bool(self.attachments)

    @property
    def sort_key(self) -> tuple[datetime, str]:
        return (self.timestamp or datetime.min.replace(tzinfo=timezone.utc), self.message_id)

    def describe(self) -> str:
        """A short one-line description for spoken lists: who, what, when."""
        return f"{self.sender.display if self.sender else 'an unknown sender'}: {self.subject or '(no subject)'}"


class GmailThread(BaseModel):
    model_config = ConfigDict(extra="forbid")

    thread_id: str
    messages: list[GmailMessage] = Field(default_factory=list)  # oldest first

    @property
    def subject(self) -> str:
        return next((m.subject for m in self.messages if m.subject), "")

    @property
    def participants(self) -> list[GmailAddress]:
        seen: dict[str, GmailAddress] = {}
        for m in self.messages:
            for a in ([m.sender] if m.sender else []) + m.recipients + m.cc:
                seen.setdefault(a.email.lower() or a.name, a)
        return list(seen.values())


class GmailSearchResult(BaseModel):
    query: str
    messages: list[GmailMessage] = Field(default_factory=list)  # newest first, as Gmail returns them
    next_page_token: str | None = None
    estimated_total: int = 0
    truncated: bool = False  # more matches exist than were returned

    @property
    def count(self) -> int:
        return len(self.messages)


class EmailClassification(BaseModel):
    category: EmailCategory
    reasons: list[str] = Field(default_factory=list)  # short, content-free rule names


class AuthStatus(BaseModel):
    configured: bool  # a client secrets file exists
    authorized: bool  # a usable token exists
    detail: str = ""
    extra: dict[str, Any] = Field(default_factory=dict)
