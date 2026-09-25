"""Hub models: integration status, permissions, error classification, normalized items and normalized tool results.

Everything an integration produces for the rest of JARVIS is a `NormalizedItem` (with provenance) or a `ToolResult`; nothing outside an
adapter sees a Google/GitHub/Telegram-specific model. Errors from any integration are classified into a small set of kinds so JARVIS can say
something meaningful ("my Gmail connection has expired") instead of "something went wrong".
"""

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class IntegrationStatus(StrEnum):
    CONNECTED = "connected"  # authenticated, never synced yet
    AUTHENTICATING = "authenticating"
    SYNCING = "syncing"
    HEALTHY = "healthy"  # last sync succeeded
    DEGRADED = "degraded"  # temporary trouble (network, rate limit, server error): retrying
    DISCONNECTED = "disconnected"  # not set up, or authentication is needed
    ERROR = "error"  # something failed that retrying will not fix by itself
    DISABLED = "disabled"  # switched off by the user: JARVIS does not touch it


class ErrorKind(StrEnum):
    AUTH_ERROR = "AUTH_ERROR"
    PERMISSION_ERROR = "PERMISSION_ERROR"
    RATE_LIMIT = "RATE_LIMIT"
    NETWORK_ERROR = "NETWORK_ERROR"
    SERVER_ERROR = "SERVER_ERROR"
    INVALID_REQUEST = "INVALID_REQUEST"
    NOT_FOUND = "NOT_FOUND"
    CONFIGURATION_ERROR = "CONFIGURATION_ERROR"
    UNKNOWN_ERROR = "UNKNOWN_ERROR"


TRANSIENT_KINDS = frozenset({ErrorKind.RATE_LIMIT, ErrorKind.NETWORK_ERROR, ErrorKind.SERVER_ERROR})
NEEDS_USER_KINDS = frozenset({ErrorKind.AUTH_ERROR, ErrorKind.CONFIGURATION_ERROR, ErrorKind.PERMISSION_ERROR})


class HubError(Exception):
    """A classified integration failure. `message` is safe to say aloud; it never contains content, tokens or credentials."""

    def __init__(self, kind: ErrorKind, message: str, retry_after: float | None = None):
        super().__init__(message)
        self.kind = kind
        self.message = message
        self.retry_after = retry_after


# Class-name suffix -> kind. Matching by name (over the whole MRO) keeps this module independent of every integration's exceptions.
_SUFFIX_KINDS: list[tuple[str, ErrorKind]] = [
    ("NotConfigured", ErrorKind.CONFIGURATION_ERROR),
    ("AuthRevoked", ErrorKind.AUTH_ERROR),
    ("AuthError", ErrorKind.AUTH_ERROR),
    ("PermissionDenied", ErrorKind.PERMISSION_ERROR),
    ("RateLimited", ErrorKind.RATE_LIMIT),
    ("Unavailable", ErrorKind.NETWORK_ERROR),
    ("NotFound", ErrorKind.NOT_FOUND),
    ("QueryError", ErrorKind.INVALID_REQUEST),
    ("Invalid", ErrorKind.INVALID_REQUEST),
    ("ResponseError", ErrorKind.SERVER_ERROR),
    ("ConflictError", ErrorKind.INVALID_REQUEST),
    ("UnsupportedCapability", ErrorKind.INVALID_REQUEST),
]
_DEFAULT_MESSAGES = {
    ErrorKind.AUTH_ERROR: "The connection has expired or was revoked. Please reconnect it.",
    ErrorKind.PERMISSION_ERROR: "The service didn't allow that. The permission may not have been granted.",
    ErrorKind.RATE_LIMIT: "The service is rate limiting requests right now. I'll try again later.",
    ErrorKind.NETWORK_ERROR: "I can't reach the service right now.",
    ErrorKind.SERVER_ERROR: "The service had a problem or sent something I couldn't understand.",
    ErrorKind.INVALID_REQUEST: "The service rejected that request.",
    ErrorKind.NOT_FOUND: "That item can't be found any more.",
    ErrorKind.CONFIGURATION_ERROR: "It isn't set up yet.",
    ErrorKind.UNKNOWN_ERROR: "Something unexpected went wrong.",
}


def classify_error(exc: BaseException, label: str = "") -> HubError:
    """Classify any exception. Integration exceptions carry a speakable `user_message`, which is used; otherwise a generic one is."""
    if isinstance(exc, HubError):
        return exc
    kind: ErrorKind | None = None
    for cls in type(exc).__mro__:
        for suffix, mapped in _SUFFIX_KINDS:
            if cls.__name__.endswith(suffix):
                kind = mapped
                break
        if kind:
            break
    if kind is None:
        name = type(exc).__name__
        if isinstance(exc, (ConnectionError, TimeoutError)) or name in ("ConnectError", "ReadTimeout", "ConnectTimeout", "NetworkError", "TransportError", "RemoteProtocolError"):
            kind = ErrorKind.NETWORK_ERROR
        elif isinstance(exc, PermissionError):
            kind = ErrorKind.PERMISSION_ERROR
        elif isinstance(exc, FileNotFoundError):
            kind = ErrorKind.CONFIGURATION_ERROR
        elif isinstance(exc, (ValueError, KeyError, TypeError)) or name in ("ValidationError", "JSONDecodeError"):
            kind = ErrorKind.SERVER_ERROR  # malformed data from the service
        else:
            kind = ErrorKind.UNKNOWN_ERROR
    retry_after = getattr(exc, "retry_after", None)
    spoken = getattr(exc, "user_message", None)
    message = spoken if isinstance(spoken, str) and spoken else _DEFAULT_MESSAGES[kind]
    if label and message == _DEFAULT_MESSAGES[kind]:
        message = f"{label}: {message}"
    return HubError(kind, message, float(retry_after) if isinstance(retry_after, (int, float)) else None)


# ---- permissions -------------------------------------------------------------------------------------------------------------------


class Permission(StrEnum):
    READ_EMAIL = "READ_EMAIL"
    SEARCH_EMAIL = "SEARCH_EMAIL"
    READ_ATTACHMENT = "READ_ATTACHMENT"
    READ_EVENTS = "READ_EVENTS"
    CREATE_EVENT = "CREATE_EVENT"
    UPDATE_EVENT = "UPDATE_EVENT"
    DELETE_EVENT = "DELETE_EVENT"
    READ_REPOSITORIES = "READ_REPOSITORIES"
    READ_COMMITS = "READ_COMMITS"
    READ_ISSUES = "READ_ISSUES"
    READ_PULL_REQUESTS = "READ_PULL_REQUESTS"
    READ_MESSAGES = "READ_MESSAGES"
    SEARCH_MESSAGES = "SEARCH_MESSAGES"
    READ_DOCUMENTS = "READ_DOCUMENTS"
    INDEX_DOCUMENTS = "INDEX_DOCUMENTS"


# Sensitive: never granted by default and always separately controlled (and every use still needs the user's confirmation).
WRITE_PERMISSIONS = frozenset({Permission.CREATE_EVENT, Permission.UPDATE_EVENT, Permission.DELETE_EVENT})
# Reading attachments and indexing files copy personal content into JARVIS: also opt-in.
OPT_IN_PERMISSIONS = WRITE_PERMISSIONS | {Permission.READ_ATTACHMENT, Permission.INDEX_DOCUMENTS}


# ---- normalized data ---------------------------------------------------------------------------------------------------------------


class ItemKind(StrEnum):
    EMAIL = "email"
    MESSAGE = "message"
    EVENT = "event"
    TASK = "task"
    DEADLINE = "deadline"
    DOCUMENT = "document"
    PROJECT = "project"
    REPOSITORY = "repository"
    COMMIT = "commit"
    ISSUE = "issue"
    PULL_REQUEST = "pull_request"
    MEETING = "meeting"


@dataclass(frozen=True)
class NormalizedItem:
    """One piece of external information in JARVIS's own shape, with where it came from."""

    kind: ItemKind
    source: str  # "gmail", "calendar", "github", "telegram", "documents"
    source_id: str  # the id in that system (message id, `calendar/event`, `owner/repo#12`, file path)
    timestamp: datetime | None  # when the source item happened/was written
    title: str
    summary: str = ""  # short; never a whole email or document
    metadata: dict[str, Any] = field(default_factory=dict)
    confidence: str = "high"  # low | medium | high: certainty of an extraction; retrieved facts are high
    external_id: str | None = None  # the id needed for a safe update/delete (calendar event id, ...)
    retrieved_at: datetime = field(default_factory=utcnow)

    @property
    def item_id(self) -> str:
        return hashlib.sha1(f"{self.source}|{self.kind.value}|{self.source_id}".encode()).hexdigest()[:24]

    @property
    def content_hash(self) -> str:
        """Changes when the item's content changes, not when it is merely re-retrieved."""
        body = json.dumps([self.title, self.summary, self.timestamp.isoformat() if self.timestamp else None, self.metadata], sort_keys=True, default=str)
        return hashlib.sha1(body.encode()).hexdigest()

    def provenance(self) -> dict[str, Any]:
        return {"source_type": self.source, "source_id": self.source_id, "source_timestamp": self.timestamp.isoformat() if self.timestamp else None,
                "retrieved_at": self.retrieved_at.isoformat(), "confidence": self.confidence}

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.item_id, "kind": self.kind.value, "title": self.title, "summary": self.summary, "metadata": self.metadata,
                "external_id": self.external_id, **self.provenance()}


@dataclass
class ToolResult:
    """The one shape every hub tool returns."""

    success: bool
    source: str
    data: Any = None
    metadata: dict[str, Any] = field(default_factory=dict)
    error: dict[str, str] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"success": self.success, "source": self.source, "data": self.data, "metadata": self.metadata, "error": self.error}

    @classmethod
    def ok(cls, source: str, data: Any, **metadata: Any) -> "ToolResult":
        return cls(True, source, data, metadata, None)

    @classmethod
    def fail(cls, source: str, kind: ErrorKind, message: str, **metadata: Any) -> "ToolResult":
        return cls(False, source, None, metadata, {"type": kind.value, "message": message})

    @classmethod
    def from_exception(cls, source: str, exc: BaseException, label: str = "") -> "ToolResult":
        err = classify_error(exc, label)
        return cls.fail(source, err.kind, err.message)
