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
        "Gmail OAuth credentials not configured. Put your Google OAuth client JSON in the secrets folder and connect Gmail from the dashboard "
        "(or run python scripts/gmail_cli.py auth). See docs/integrations/GMAIL.md."
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


class GmailProfile(BaseModel):
    """The connected mailbox. `email_address` is the account itself: it is shown redacted everywhere it leaves the local API (see `redact_address`)."""

    model_config = ConfigDict(extra="forbid")

    email_address: str
    messages_total: int = 0
    threads_total: int = 0


class GmailLabel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    name: str
    type: str = "user"  # system | user
    messages_total: int | None = None
    messages_unread: int | None = None
    threads_unread: int | None = None


class GmailThreadSummary(BaseModel):
    """One row of a thread listing: the id and Gmail's own snippet. The messages are fetched only when the thread is opened."""

    model_config = ConfigDict(extra="forbid")

    thread_id: str
    snippet: str = ""


class GmailThreadList(BaseModel):
    query: str = ""
    threads: list[GmailThreadSummary] = Field(default_factory=list)
    next_page_token: str | None = None
    estimated_total: int = 0

    @property
    def count(self) -> int:
        return len(self.threads)


class UnreadCounts(BaseModel):
    """Exact counts from the Gmail label counters (not an estimate from a search)."""

    unread_total: int = 0
    inbox_unread: int = 0
    important_unread: int = 0


def redact_address(email: str) -> str:
    """"harsha282004@gmail.com" -> "h***@gmail.com": enough for the owner to recognise the account, useless to anyone else."""
    if "@" not in email:
        return "an unknown account"
    local, _, domain = email.partition("@")
    return f"{local[:1]}***@{domain}"


class EmailClassification(BaseModel):
    category: EmailCategory
    reasons: list[str] = Field(default_factory=list)  # short, content-free rule names


from integrations.google_oauth import AuthStatus  # noqa: E402,F401  (shared with Calendar; re-exported)
