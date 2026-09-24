"""TaskActionExecutor: carries out a validated TaskAction, only through the PermissionManager.

    AgentBrain -> TaskAction (validated data)
        -> executor: tool.resolve() identifies the target (asks when unclear, never guesses)
        -> PermissionManager.request_permission() bound to the exact resolved parameters
        -> LOW-risk tools are auto-approved by policy; cancelling needs the user's explicit "yes" next turn
        -> Tool.execute() (re-checks the permission) -> TaskService / ReminderService -> database

The confirmation is read by this code from the user's next message (never from model output), and
approves through PermissionManager.approve(actor="user"). Nothing here raises to the caller and every
failure produces an honest reply that does not claim anything was done.
"""

import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from agent.tasks.intents import TaskAction
from agent.tasks.models import InvalidTransition, TaskError, TaskNotFound, TaskStorageError, TaskValidationError, utcnow
from agent.tasks.tools import Clarify, TaskTool
from agent.tools.base import EXECUTE_ACTION
from backend.core.logging import get_logger
from backend.core.security import PermissionDenied, PermissionManager, PermissionRequest, PermissionStatus

logger = get_logger(__name__)

CONFIRMATION_TTL_SECONDS = 60.0

STORAGE_REPLY = "I couldn't do that because my task database isn't available, so nothing was changed."
DENIED_REPLY = "I'm not allowed to do that."
UNAVAILABLE_REPLY = "I can't do that right now."
FAILED_REPLY = "Sorry, something went wrong and nothing was changed."
DECLINED_REPLY = "Okay, I won't do that."

_YES = {"yes", "yeah", "yep", "yup", "sure", "confirm", "confirmed", "ok", "okay", "please do", "do it", "go ahead",
        "yes please", "yes do it", "yes go ahead", "yes confirm", "yes i do", "correct", "affirmative"}
_NO = {"no", "nope", "nah", "dont", "do not", "never mind", "nevermind", "stop", "leave it", "no thanks", "no dont",
       "negative", "cancel that", "abort"}


def classify_confirmation(text: str) -> bool | None:
    """True for a clear yes, False for a clear no, None for anything else (deliberately strict)."""
    words = " ".join(re.sub(r"[^a-z ]", "", text.lower().replace("'", "")).split())
    if words in _YES:
        return True
    if words in _NO:
        return False
    return None


@dataclass(frozen=True)
class ActionOutcome:
    reply: str
    permission_request: PermissionRequest | None = None
    executed: bool = False


@dataclass
class _Pending:
    tool: TaskTool
    request: PermissionRequest
    params: dict
    expires_at: datetime


class TaskActionExecutor:
    def __init__(
        self,
        tools: Sequence[TaskTool],
        permissions: PermissionManager | None,
        clock: Callable[[], datetime] = utcnow,
        confirmation_ttl_seconds: float = CONFIRMATION_TTL_SECONDS,
    ):
        self._tools = {t.name: t for t in tools}
        self._permissions = permissions
        self._clock = clock
        self._ttl = timedelta(seconds=confirmation_ttl_seconds)
        self._pending: dict[str, _Pending] = {}  # one open confirmation per conversation session

    def execute(self, action: TaskAction, session_id: str) -> ActionOutcome:
        """Never raises. A confirmation still pending from an earlier request is dropped first."""
        self._pending.pop(session_id, None)
        try:
            return self._execute(action, session_id)
        except Exception as exc:  # noqa: BLE001 - the conversation must survive any tool failure
            return self._failure(exc)

    def _execute(self, action: TaskAction, session_id: str) -> ActionOutcome:
        tool = self._tools.get(action.name.value)
        if tool is None or self._permissions is None:
            return ActionOutcome(UNAVAILABLE_REPLY if tool is None else DENIED_REPLY)
        resolution = tool.resolve(action.arguments)
        if isinstance(resolution, Clarify):
            return ActionOutcome(resolution.message)
        params = {**resolution.params, "origin_session": session_id}
        request = self._permissions.request_permission(
            tool_name=tool.name, action=EXECUTE_ACTION, description=f"Use the {tool.name} tool",
            parameters=params, session_id=session_id, requested_by="agent",
        )
        if request.status is PermissionStatus.APPROVED:  # LOW risk, approved by policy
            return self._run(tool, request, params, session_id)
        if request.status is PermissionStatus.PENDING and resolution.confirm_prompt:
            self._pending[session_id] = _Pending(tool, request, params, self._clock() + self._ttl)
            logger.info("Confirmation requested (tool=%s)", tool.name)
            return ActionOutcome(resolution.confirm_prompt, request)
        logger.warning("Task action not permitted (tool=%s, status=%s)", tool.name, request.status.value)
        return ActionOutcome(DENIED_REPLY, request)

    def confirm(self, text: str, session_id: str) -> ActionOutcome | None:
        """Handle the user's answer to a pending confirmation. None means: this message is not an answer,
        so handle it normally (the confirmation is dropped, it never carries over to later turns)."""
        pending = self._pending.pop(session_id, None)
        if pending is None or self._clock() >= pending.expires_at or self._permissions is None:
            return None
        answer = classify_confirmation(text)
        if answer is None:
            return None
        try:
            if not answer:
                self._permissions.deny(pending.request, actor="user")
                return ActionOutcome(DECLINED_REPLY, pending.request)
            approved = self._permissions.approve(pending.request, actor="user")
            return self._run(pending.tool, approved, pending.params, session_id)
        except Exception as exc:  # noqa: BLE001
            return self._failure(exc)

    def _run(self, tool: TaskTool, request: PermissionRequest, params: dict, session_id: str) -> ActionOutcome:
        try:
            reply = tool.execute(self._permissions, request.request_id, session_id=session_id, **params)
        except Exception as exc:  # noqa: BLE001
            return ActionOutcome(self._failure(exc).reply, request)
        return ActionOutcome(str(reply), request, executed=True)

    @staticmethod
    def _failure(exc: Exception) -> ActionOutcome:
        if isinstance(exc, PermissionDenied):
            logger.warning("Task tool was not authorized")
            return ActionOutcome(DENIED_REPLY)
        if isinstance(exc, TaskStorageError):
            logger.error("Task database unavailable (%s)", type(exc).__name__)
            return ActionOutcome(STORAGE_REPLY)
        if isinstance(exc, TaskNotFound):
            return ActionOutcome("I can't find that any more, so nothing was changed.")
        if isinstance(exc, (InvalidTransition, TaskValidationError)):
            return ActionOutcome(f"I couldn't do that: {exc}.".replace("..", "."))
        if isinstance(exc, TaskError):
            return ActionOutcome(FAILED_REPLY)
        logger.error("Task action failed unexpectedly (%s)", type(exc).__name__)
        return ActionOutcome(FAILED_REPLY)
