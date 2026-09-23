"""Typed security models: risk, scope, status, permission request, results.

`PermissionRequest` keeps the Phase 0 fields (tool_name, action, requested_by)
and adds what an authorization decision needs. It is immutable; the
PermissionManager holds the authoritative status, and a request presented by
a caller is only ever a reference to that stored record.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import IntEnum, StrEnum
from uuid import uuid4


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class RiskLevel(IntEnum):
    """Potential impact of an action. Ordered: LOW < MEDIUM < HIGH < CRITICAL."""

    LOW = 1  # read-only local information
    MEDIUM = 2  # creating/modifying an external resource
    HIGH = 3  # sending an email/message
    CRITICAL = 4  # destructive or irreversible system action


class PermissionScope(StrEnum):
    ONE_TIME = "one_time"
    SESSION = "session"
    PERSISTENT = "persistent"


class PermissionStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    DENIED = "denied"
    EXPIRED = "expired"
    CANCELLED = "cancelled"
    CONSUMED = "consumed"  # a ONE_TIME approval that has been used


class ReasonCode(StrEnum):
    """Why a request/authorization ended the way it did (also used in the audit log)."""

    OK = "ok"
    REQUEST_CREATED = "request_created"
    AUTO_POLICY = "auto_policy"
    USER_APPROVED = "user_approved"
    USER_DENIED = "user_denied"
    USER_CANCELLED = "user_cancelled"
    UNKNOWN_TOOL = "unknown_tool"
    UNKNOWN_PERMISSION = "unknown_permission"
    NOT_APPROVED = "not_approved"
    EXPIRED = "expired"
    ALREADY_USED = "already_used"
    ACTION_MISMATCH = "action_mismatch"
    SESSION_MISMATCH = "session_mismatch"
    SESSION_ENDED = "session_ended"
    MISSING_SCOPE = "missing_scope"
    SCOPE_NOT_ALLOWED = "scope_not_allowed"
    POLICY_DENIED = "policy_denied"
    MALFORMED = "malformed"
    MANAGER_UNAVAILABLE = "manager_unavailable"
    MANAGER_ERROR = "manager_error"


class PermissionDenied(Exception):
    """Raised when a tool invocation is not authorized."""


class PermissionStateError(Exception):
    """Raised when an approve/deny/cancel/expire call is not valid for the request."""

    def __init__(self, code: ReasonCode, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class PermissionRequest:
    tool_name: str
    action: str
    requested_by: str = "agent"
    description: str = ""
    risk: RiskLevel = RiskLevel.HIGH  # fail-safe: unknown risk is treated as high
    scope: PermissionScope = PermissionScope.ONE_TIME
    session_id: str | None = None
    # SHA-256 over the canonicalized (tool, action, parameters); the parameters
    # themselves are never stored. See docs/security-and-permissions.md.
    action_digest: str = ""
    request_id: str = field(default_factory=lambda: uuid4().hex)
    created_at: datetime = field(default_factory=utcnow)
    expires_at: datetime | None = None
    status: PermissionStatus = PermissionStatus.PENDING

    def __post_init__(self) -> None:
        for name in ("created_at", "expires_at"):
            value = getattr(self, name)
            if value is not None and value.tzinfo is None:
                raise ValueError(f"{name} must be timezone-aware")


@dataclass(frozen=True)
class ToolSecurityInfo:
    """What the security layer knows about a tool. It comes from the tool's
    registration, never from LLM output."""

    name: str
    requires_permission: bool = True
    risk: RiskLevel = RiskLevel.HIGH
    allowed_scopes: tuple[PermissionScope, ...] = (PermissionScope.ONE_TIME,)


@dataclass(frozen=True)
class AuthorizationResult:
    allowed: bool
    code: ReasonCode
    reason: str
    request_id: str | None = None
