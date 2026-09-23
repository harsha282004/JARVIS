"""PermissionManager: the mandatory gate between an agent decision and a tool.

    AgentDecision -> request_permission() -> PENDING -> approve()/deny() -> check() -> Tool

Invariants (see docs/security-and-permissions.md):
- Only this class produces APPROVED. A PermissionRequest handed in by a
  caller is only a reference: status, scope and binding are always read from
  the manager's own record, so a forged or altered request cannot authorize.
- Anything missing, unknown, malformed, expired, mismatched or erroring
  results in DENY. There is no fail-open path.
- An approval is bound to (tool, action, parameter digest, session).
"""

import re
import threading
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Any

from backend.core.logging import get_logger
from backend.core.security.audit import AuditLog, SecurityEvent, SecurityEventType
from backend.core.security.binding import compute_action_digest
from backend.core.security.models import (
    AuthorizationResult,
    PermissionDenied,
    PermissionRequest,
    PermissionScope,
    PermissionStateError,
    PermissionStatus,
    ReasonCode,
    RiskLevel,
    ToolSecurityInfo,
    utcnow,
)
from backend.core.security.policy import PermissionPolicy, PolicyDecision

logger = get_logger(__name__)

DEFAULT_EXPIRY_SECONDS = 300.0
MAX_RECORDS = 1000
MAX_NAME_CHARS = 64
MAX_DESCRIPTION_CHARS = 200
INVALID_NAME = "<invalid>"

_OPEN_STATES = {PermissionStatus.PENDING, PermissionStatus.APPROVED}


def _clean(text: object, limit: int) -> str:
    """Printable, single-line, length-limited form of a (possibly hostile) string."""
    if not isinstance(text, str):
        return ""
    return re.sub(r"\s+", " ", "".join(c for c in text if c.isprintable() or c.isspace())).strip()[:limit]


@dataclass
class _Record:
    request: PermissionRequest  # immutable original, as created
    status: PermissionStatus


class PermissionManager:
    def __init__(
        self,
        tools: Iterable[ToolSecurityInfo] = (),
        policy: PermissionPolicy | None = None,
        audit: AuditLog | None = None,
        default_expiry_seconds: float = DEFAULT_EXPIRY_SECONDS,
        clock: Callable[[], datetime] = utcnow,
    ):
        self._tools = {t.name: t for t in tools}
        self._policy = policy or PermissionPolicy()
        self.audit = audit or AuditLog()
        self._default_expiry = default_expiry_seconds
        self._clock = clock
        self._records: dict[str, _Record] = {}
        self._lock = threading.RLock()

    # ---- registration ------------------------------------------------

    def register_tool(self, info: ToolSecurityInfo) -> None:
        with self._lock:
            self._tools[info.name] = info

    # ---- requests ----------------------------------------------------

    def request_permission(
        self,
        tool_name: str,
        action: str,
        description: str = "",
        parameters: Mapping[str, Any] | None = None,
        scope: PermissionScope = PermissionScope.ONE_TIME,
        session_id: str | None = None,
        ttl_seconds: float | None = None,
        requested_by: str = "agent",
    ) -> PermissionRequest:
        """Create a request and return its snapshot. The outcome is PENDING
        (needs approval), DENIED (policy/malformed) or, for a registered
        LOW-risk tool that needs no permission, APPROVED by policy."""
        with self._lock:
            now = self._clock()
            name, act = _clean(tool_name, MAX_NAME_CHARS), _clean(action, MAX_NAME_CHARS)
            digest = ""
            denial: ReasonCode | None = None
            if not name or not act or not isinstance(scope, PermissionScope):
                name, act, denial = name or INVALID_NAME, act or INVALID_NAME, ReasonCode.MALFORMED
            else:
                try:
                    digest = compute_action_digest(name, act, parameters)
                except (TypeError, ValueError):
                    denial = ReasonCode.MALFORMED

            ttl = ttl_seconds if ttl_seconds is not None else self._default_expiry
            if scope is PermissionScope.PERSISTENT and ttl_seconds is None:
                ttl = None  # persistent grants only lapse if an expiry is given
            if ttl is not None and not ttl > 0:
                denial = denial or ReasonCode.MALFORMED

            info = self._tools.get(name)
            outcome = self._policy.evaluate(info, scope if isinstance(scope, PermissionScope) else PermissionScope.ONE_TIME)
            if denial is None and scope is PermissionScope.SESSION and not session_id:
                denial = ReasonCode.MISSING_SCOPE
            if denial is None and outcome.decision is PolicyDecision.DENY:
                denial = outcome.code

            request = PermissionRequest(
                tool_name=name,
                action=act,
                requested_by=_clean(requested_by, MAX_NAME_CHARS) or "agent",
                description=_clean(description, MAX_DESCRIPTION_CHARS),
                risk=info.risk if info else RiskLevel.HIGH,
                scope=scope if isinstance(scope, PermissionScope) else PermissionScope.ONE_TIME,
                session_id=session_id if isinstance(session_id, str) and session_id else None,
                action_digest=digest,
                created_at=now,
                expires_at=(now + timedelta(seconds=ttl)) if ttl is not None and ttl > 0 else None,
            )
            record = _Record(request, PermissionStatus.PENDING)
            self._records[request.request_id] = record
            self._prune()
            self._emit(SecurityEventType.PERMISSION_REQUESTED, request, "created", ReasonCode.REQUEST_CREATED)

            if denial is not None:
                record.status = PermissionStatus.DENIED
                self._emit(SecurityEventType.PERMISSION_DENIED, request, "denied", denial, actor="policy")
            elif outcome.decision is PolicyDecision.ALLOW:
                record.status = PermissionStatus.APPROVED
                self._emit(SecurityEventType.PERMISSION_APPROVED, request, "approved", ReasonCode.AUTO_POLICY, actor="policy")
            return self._snapshot(record)

    def get(self, request_id: str) -> PermissionRequest | None:
        with self._lock:
            record = self._records.get(request_id)
            if record is None:
                return None
            self._refresh(record)
            return self._snapshot(record)

    # ---- explicit decisions (the only ways to reach APPROVED/DENIED/...) ----

    def approve(
        self, request: PermissionRequest, actor: str = "user", confirm_persistent: bool = False
    ) -> PermissionRequest:
        with self._lock:
            record = self._matching_record(request, actor)
            self._require_status(record, request, actor, {PermissionStatus.PENDING}, "approve")
            if record.request.scope is PermissionScope.PERSISTENT and not confirm_persistent:
                self._reject(request, actor, ReasonCode.SCOPE_NOT_ALLOWED)
                raise PermissionStateError(
                    ReasonCode.SCOPE_NOT_ALLOWED, "Persistent permissions need explicit confirmation"
                )
            record.status = PermissionStatus.APPROVED
            self._emit(SecurityEventType.PERMISSION_APPROVED, record.request, "approved", ReasonCode.USER_APPROVED, actor=actor)
            return self._snapshot(record)

    def deny(self, request: PermissionRequest, actor: str = "user") -> PermissionRequest:
        return self._close(
            request, actor, {PermissionStatus.PENDING}, PermissionStatus.DENIED,
            SecurityEventType.PERMISSION_DENIED, "denied", ReasonCode.USER_DENIED, "deny",
        )

    def cancel(self, request: PermissionRequest, actor: str = "user") -> PermissionRequest:
        return self._close(
            request, actor, _OPEN_STATES, PermissionStatus.CANCELLED,
            SecurityEventType.PERMISSION_CANCELLED, "cancelled", ReasonCode.USER_CANCELLED, "cancel",
        )

    def expire(self, request: PermissionRequest, actor: str = "system") -> PermissionRequest:
        return self._close(
            request, actor, _OPEN_STATES, PermissionStatus.EXPIRED,
            SecurityEventType.PERMISSION_EXPIRED, "expired", ReasonCode.EXPIRED, "expire",
        )

    def expire_due(self) -> int:
        """Expire every open request whose time has passed. Returns how many."""
        with self._lock:
            return sum(self._refresh(r) for r in list(self._records.values()))

    def end_session(self, session_id: str) -> int:
        """Invalidate every open request bound to a conversation session."""
        count = 0
        with self._lock:
            for record in self._records.values():
                if record.request.session_id == session_id and record.status in _OPEN_STATES:
                    record.status = PermissionStatus.EXPIRED
                    self._emit(SecurityEventType.PERMISSION_EXPIRED, record.request, "expired", ReasonCode.SESSION_ENDED, actor="system")
                    count += 1
        return count

    # ---- the gate ----------------------------------------------------

    def check(
        self,
        request_id: str,
        *,
        tool_name: str,
        action: str,
        parameters: Mapping[str, Any] | None = None,
        session_id: str | None = None,
    ) -> AuthorizationResult:
        """Decide whether this exact action may run now. Fails closed.

        `parameters` must be the real arguments about to be used: they are
        re-hashed and compared with the digest that was approved. A ONE_TIME
        approval is consumed by the first ALLOW.
        """
        try:
            with self._lock:
                digest = compute_action_digest(tool_name, action, parameters)
                return self._check(request_id, tool_name, action, digest, session_id)
        except (TypeError, ValueError):
            return self._denied(request_id, tool_name, action, session_id, ReasonCode.MALFORMED, "Malformed authorization check")
        except Exception:  # noqa: BLE001 - security boundary: any internal error must deny
            logger.exception("Permission check failed internally; denying")
            return self._denied(request_id, tool_name, action, session_id, ReasonCode.MANAGER_ERROR, "Permission manager error")

    def authorize(self, request: PermissionRequest) -> bool:
        """Phase 0 compatible boolean form of `check` (uses the request's own digest)."""
        try:
            with self._lock:
                return self._check(
                    request.request_id, request.tool_name, request.action,
                    request.action_digest, request.session_id,
                ).allowed
        except Exception:  # noqa: BLE001 - security boundary: any internal error must deny
            logger.exception("Permission check failed internally; denying")
            return False

    def require(self, request: PermissionRequest) -> None:
        """Raise PermissionDenied if the request is not authorized."""
        if not self.authorize(request):
            raise PermissionDenied(
                f"Tool '{request.tool_name}' action '{request.action}' was not authorized"
            )

    # ---- internals ---------------------------------------------------

    def _check(
        self, request_id: str, tool_name: str, action: str, digest: str, session_id: str | None
    ) -> AuthorizationResult:
        name, act = _clean(tool_name, MAX_NAME_CHARS), _clean(action, MAX_NAME_CHARS)
        if not isinstance(request_id, str) or not request_id or not name or not act:
            return self._denied(request_id, name or INVALID_NAME, act or INVALID_NAME, session_id, ReasonCode.MALFORMED, "Malformed authorization check")
        record = self._records.get(request_id)
        if record is None:
            return self._denied(request_id, name, act, session_id, ReasonCode.UNKNOWN_PERMISSION, "No such permission")
        info = self._tools.get(name)
        if info is None:
            return self._denied(request_id, name, act, session_id, ReasonCode.UNKNOWN_TOOL, "Unknown tool")
        self._refresh(record)
        stored = record.request
        if (name, act, digest) != (stored.tool_name, stored.action, stored.action_digest):
            return self._denied(request_id, name, act, session_id, ReasonCode.ACTION_MISMATCH, "Does not match the approved action")
        if stored.scope is PermissionScope.SESSION and stored.session_id is None:
            return self._denied(request_id, name, act, session_id, ReasonCode.MISSING_SCOPE, "Session scope without a session")
        if stored.session_id is not None and session_id != stored.session_id:
            return self._denied(request_id, name, act, session_id, ReasonCode.SESSION_MISMATCH, "Approved for a different session")
        if record.status is PermissionStatus.EXPIRED:
            return self._denied(request_id, name, act, session_id, ReasonCode.EXPIRED, "Permission expired")
        if record.status is PermissionStatus.CONSUMED:
            return self._denied(request_id, name, act, session_id, ReasonCode.ALREADY_USED, "One-time permission already used")
        if record.status is not PermissionStatus.APPROVED:
            return self._denied(request_id, name, act, session_id, ReasonCode.NOT_APPROVED, f"Permission is {record.status.value}")
        if self._policy.evaluate(info, stored.scope).decision is PolicyDecision.DENY:
            return self._denied(request_id, name, act, session_id, ReasonCode.POLICY_DENIED, "Denied by policy")

        if stored.scope is PermissionScope.ONE_TIME:
            record.status = PermissionStatus.CONSUMED
        self._emit(SecurityEventType.AUTHORIZATION_ALLOWED, stored, "allowed", ReasonCode.OK)
        return AuthorizationResult(True, ReasonCode.OK, "Authorized", request_id)

    def _denied(
        self, request_id: object, tool_name: str, action: str, session_id: str | None,
        code: ReasonCode, reason: str,
    ) -> AuthorizationResult:
        rid = request_id if isinstance(request_id, str) else None
        self.audit.record(
            SecurityEvent(
                timestamp=self._clock(),
                event_type=SecurityEventType.AUTHORIZATION_DENIED,
                request_id=rid,
                tool_name=_clean(tool_name, MAX_NAME_CHARS) or INVALID_NAME,
                action=_clean(action, MAX_NAME_CHARS) or INVALID_NAME,
                result="denied",
                code=code,
                session_id=session_id if isinstance(session_id, str) else None,
            )
        )
        return AuthorizationResult(False, code, reason, rid)

    def _matching_record(self, request: PermissionRequest, actor: str) -> _Record:
        record = self._records.get(getattr(request, "request_id", None))
        if record is None:
            self._reject(request, actor, ReasonCode.UNKNOWN_PERMISSION)
            raise PermissionStateError(ReasonCode.UNKNOWN_PERMISSION, "No such permission request")
        stored = record.request
        fields = ("tool_name", "action", "action_digest", "scope", "session_id")
        if any(getattr(request, f) != getattr(stored, f) for f in fields):
            self._reject(request, actor, ReasonCode.ACTION_MISMATCH)
            raise PermissionStateError(
                ReasonCode.ACTION_MISMATCH, "The request does not match the original request"
            )
        return record

    def _require_status(
        self, record: _Record, request: PermissionRequest, actor: str,
        allowed: set[PermissionStatus], verb: str,
    ) -> None:
        self._refresh(record)
        if record.status not in allowed:
            code = ReasonCode.EXPIRED if record.status is PermissionStatus.EXPIRED else ReasonCode.NOT_APPROVED
            self._reject(request, actor, code)
            raise PermissionStateError(code, f"Cannot {verb} a request that is {record.status.value}")

    def _close(
        self, request: PermissionRequest, actor: str, allowed: set[PermissionStatus],
        new_status: PermissionStatus, event: SecurityEventType, result: str,
        code: ReasonCode, verb: str,
    ) -> PermissionRequest:
        with self._lock:
            record = self._matching_record(request, actor)
            self._require_status(record, request, actor, allowed, verb)
            record.status = new_status
            self._emit(event, record.request, result, code, actor=actor)
            return self._snapshot(record)

    def _refresh(self, record: _Record) -> int:
        """Lazily expire an open record whose time has passed. Returns 1 if it did."""
        expires = record.request.expires_at
        if record.status in _OPEN_STATES and expires is not None and self._clock() >= expires:
            record.status = PermissionStatus.EXPIRED
            self._emit(SecurityEventType.PERMISSION_EXPIRED, record.request, "expired", ReasonCode.EXPIRED, actor="system")
            return 1
        return 0

    def _snapshot(self, record: _Record) -> PermissionRequest:
        return replace(record.request, status=record.status)

    def _reject(self, request: PermissionRequest, actor: str, code: ReasonCode) -> None:
        self.audit.record(
            SecurityEvent(
                timestamp=self._clock(),
                event_type=SecurityEventType.AUTHORIZATION_DENIED,
                request_id=_clean(getattr(request, "request_id", ""), 64) or None,
                tool_name=_clean(getattr(request, "tool_name", ""), MAX_NAME_CHARS) or INVALID_NAME,
                action=_clean(getattr(request, "action", ""), MAX_NAME_CHARS) or INVALID_NAME,
                result="rejected",
                code=code,
                actor=_clean(actor, MAX_NAME_CHARS),
            )
        )

    def _emit(
        self, event_type: SecurityEventType, request: PermissionRequest, result: str,
        code: ReasonCode, actor: str = "",
    ) -> None:
        self.audit.record(
            SecurityEvent(
                timestamp=self._clock(),
                event_type=event_type,
                request_id=request.request_id,
                tool_name=request.tool_name,
                action=request.action,
                result=result,
                code=code,
                session_id=request.session_id,
                actor=actor,
            )
        )

    def _prune(self) -> None:
        if len(self._records) <= MAX_RECORDS:
            return
        for rid in [r for r, rec in self._records.items() if rec.status not in _OPEN_STATES]:
            del self._records[rid]
            if len(self._records) <= MAX_RECORDS:
                return


def check_authorization(
    manager: PermissionManager | None,
    request_id: str,
    *,
    tool_name: str,
    action: str,
    parameters: Mapping[str, Any] | None = None,
    session_id: str | None = None,
) -> AuthorizationResult:
    """Fail-closed wrapper used by tools: no manager, or a manager that raises, means DENY."""
    if manager is None:
        return AuthorizationResult(False, ReasonCode.MANAGER_UNAVAILABLE, "Permission manager unavailable", None)
    try:
        return manager.check(
            request_id, tool_name=tool_name, action=action, parameters=parameters, session_id=session_id
        )
    except Exception:  # noqa: BLE001 - security boundary: any failure must deny
        logger.exception("Permission manager raised during a check; denying")
        return AuthorizationResult(False, ReasonCode.MANAGER_ERROR, "Permission manager error", None)
