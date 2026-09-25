"""ConfirmationEngine: the one place where a sensitive action waits for the user's explicit yes to THAT action.

    request(...)  ->  states exactly what will happen ("add these 3 work blocks to your 'Personal' calendar: ...") and waits
    respond(...)  ->  a clear "yes" runs exactly the action that was described; a "no" cancels it; anything else drops it

Guarantees:
  * a confirmation is bound to a digest of the exact action and parameters; the action is re-hashed before it runs, so a different
    action can never ride on an earlier "yes" (also covered by the tests);
  * a vague answer ("maybe", "sure why not, and also send it") is neither yes nor no: the pending action is dropped, never executed;
  * a "yes" only counts when it comes from the USER (spoken or typed), never from email, document, tool or memory text;
  * destructive actions need an explicit answer that names the action ("confirm delete"); a bare "yes" only asks again;
  * one open confirmation per conversation session, valid for a short time, used at most once;
  * every request, answer and outcome is written to the action audit log.
"""

import hashlib
import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from agent.tasks.executor import classify_confirmation
from backend.core.action_audit import ActionAuditLog, ActionResult, Confirmation
from backend.core.logging import get_logger
from backend.core.security.approval import ApprovalClass, requires_confirmation, requires_explicit_confirmation
from backend.core.security.trust import TrustLevel, may_authorize

logger = get_logger(__name__)

CONFIRMATION_TTL_SECONDS = 120.0
_EXPLICIT_YES = re.compile(r"^(?:yes,?\s+)?(?:please\s+)?(?:delete|remove|erase|confirm)(?:\s+(?:it|that|them|all|delete|deletion))?(?:\s+please)?$", re.I)


@dataclass(frozen=True)
class ActionReport:
    """What running an action actually achieved. `ok` is only True when the result was verified."""

    ok: bool
    message: str
    verified: bool = False
    partial: bool = False


@dataclass
class PendingAction:
    action_class: ApprovalClass
    tool: str
    summary: str
    params: dict
    digest: str
    run: Callable[[], ActionReport]
    session_id: str
    expires_at: datetime
    source: str


def action_digest(action_class: ApprovalClass, tool: str, params: dict) -> str:
    canonical = json.dumps({"class": action_class.value, "tool": tool, "params": params}, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class ConfirmationEngine:
    def __init__(self, audit: ActionAuditLog | None = None, clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
                 ttl_seconds: float = CONFIRMATION_TTL_SECONDS):
        self._audit = audit
        self._clock = clock
        self._ttl = timedelta(seconds=ttl_seconds)
        self._pending: dict[str, PendingAction] = {}

    # ---- requesting ----------------------------------------------------------------------------------------------------

    def request(self, *, action_class: ApprovalClass, tool: str, summary: str, params: dict, run: Callable[[], ActionReport],
                session_id: str, source: str = "user_request") -> str:
        """Register an action that needs confirmation and return the prompt to say. Actions that need no confirmation are refused
        here: they must not be routed through a confirmation that could be skipped or faked."""
        if not requires_confirmation(action_class):
            raise ValueError(f"{action_class.value} does not need confirmation; do not route it through the confirmation engine")
        previous = self._pending.pop(session_id, None)
        if previous is not None:
            self._log(previous, ActionResult.DECLINED, Confirmation.NONE, "superseded by a new request")
        pending = PendingAction(action_class, tool, summary, dict(params), action_digest(action_class, tool, params), run, session_id,
                                self._clock() + self._ttl, source)
        self._pending[session_id] = pending
        logger.info("Confirmation requested (class=%s)", action_class.value)
        return summary

    def has_pending(self, session_id: str) -> bool:
        pending = self._pending.get(session_id)
        return pending is not None and self._clock() < pending.expires_at

    def pending_summary(self, session_id: str) -> str | None:
        return self._pending[session_id].summary if self.has_pending(session_id) else None

    def cancel(self, session_id: str) -> bool:
        return self._pending.pop(session_id, None) is not None

    # ---- answering -----------------------------------------------------------------------------------------------------

    def respond(self, text: str, session_id: str, *, level: TrustLevel = TrustLevel.USER) -> str | None:
        """The reply to the user's answer, or None when there is nothing pending or the message is not an answer (handle it normally)."""
        pending = self._pending.get(session_id)
        if pending is None:
            return None
        if self._clock() >= pending.expires_at:
            self._pending.pop(session_id, None)
            self._log(pending, ActionResult.DECLINED, Confirmation.NONE, "confirmation expired")
            return None
        if not may_authorize(level):  # external text can never authorize anything
            self._pending.pop(session_id, None)
            self._log(pending, ActionResult.DENIED, Confirmation.NONE, f"confirmation attempted by {level.value} content")
            return None
        answer = classify_confirmation(text)
        explicit = bool(_EXPLICIT_YES.match(" ".join(text.replace("'", "").split())))
        if requires_explicit_confirmation(pending.action_class):
            if answer is True and not explicit:
                return f"That would permanently change your data. To go ahead, say 'confirm delete'. {pending.summary}"
            if explicit:
                answer = True
        if answer is None:
            self._pending.pop(session_id, None)  # not an answer: drop it, never execute on ambiguity
            self._log(pending, ActionResult.DECLINED, Confirmation.NONE, "answer was not a clear yes or no")
            return None
        self._pending.pop(session_id, None)
        if answer is False:
            self._log(pending, ActionResult.DECLINED, Confirmation.USER, "user declined")
            return "Okay, I won't do that."
        if action_digest(pending.action_class, pending.tool, pending.params) != pending.digest:
            self._log(pending, ActionResult.DENIED, Confirmation.USER, "parameters changed after the confirmation was requested")
            return "I'm not allowed to do that."
        try:
            report = pending.run()
        except Exception as exc:  # noqa: BLE001 - the conversation must survive any failure, and a failure is never reported as success
            logger.error("Confirmed action failed (%s)", type(exc).__name__)
            self._log(pending, ActionResult.FAILED, Confirmation.USER, type(exc).__name__)
            return "Sorry, something went wrong, and I can't confirm that it was done."
        result = ActionResult.SUCCESS if report.ok and report.verified else ActionResult.UNVERIFIED if report.ok else ActionResult.FAILED
        self._log(pending, result, Confirmation.USER, report.message[:160])
        return report.message

    def _log(self, pending: PendingAction, result: ActionResult, confirmation: Confirmation, detail: str) -> None:
        if self._audit is not None:
            self._audit.record(tool=pending.tool, action=pending.action_class.value, result=result, confirmation=confirmation,
                               source=pending.source, detail=detail, refs={"digest": pending.digest[:12]})
