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
    # What the conversation history should keep instead of `reply` (Gmail replies contain untrusted email text).
    history_text: str | None = None


MAX_CLARIFY_WORDS = 8          # a longer utterance is a new request, not the answer to a question
CLARIFICATION_TTL_SECONDS = 90.0
_PART_OF_DAY = {"morning": "9 AM", "this morning": "9 AM", "in the morning": "9 AM", "afternoon": "3 PM", "in the afternoon": "3 PM",
                "evening": "6 PM", "in the evening": "6 PM", "tonight": "9 PM", "night": "9 PM", "noon": "12 PM"}
_NEW_REQUEST = re.compile(r"^(?:remind|add|create|schedule|set|cancel|delete|what|when|where|who|why|how|show|list|tell|read|open|send|check|"
                          r"do i|are there|is there|can you|could you|please)\b", re.I)


@dataclass
class _PendingClarify:
    action: TaskAction
    field: str
    merge: bool
    expires_at: datetime
    attempts: int = 1


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
        self._clarify: dict[str, _PendingClarify] = {}  # one open question ("What time tomorrow?") per session

    # ---- pending state (asked by the voice layer, never used to authorize anything) ------------------------------------------

    def awaiting(self, session_id: str) -> str | None:
        """What JARVIS is waiting for the user to answer: "confirmation", "clarification" or None."""
        now = self._clock()
        pending = self._pending.get(session_id)
        if pending is not None and now < pending.expires_at:
            return "confirmation"
        clarify = self._clarify.get(session_id)
        if clarify is not None and now < clarify.expires_at:
            return "clarification"
        return None

    def cancel_pending(self, session_id: str) -> bool:
        """The user said "cancel"/"never mind": drop any open question. Nothing is executed or approved."""
        had = self.awaiting(session_id) is not None
        pending = self._pending.pop(session_id, None)
        if pending is not None and self._permissions is not None:
            try:
                self._permissions.deny(pending.request, actor="user")
            except Exception:  # noqa: BLE001 - dropping it is enough; nothing runs
                pass
        self._clarify.pop(session_id, None)
        return had

    def answer_clarification(self, text: str, session_id: str) -> "ActionOutcome | None":
        """A short reply to our own question ("Tomorrow." / "6 PM.") completes the earlier request. Anything that looks like
        a new request is not an answer: the question is dropped and the text is handled normally. Goes through the same
        resolve -> PermissionManager -> tool path as the original request."""
        pending = self._clarify.pop(session_id, None)
        if pending is None or self._clock() >= pending.expires_at:
            return None
        phrase = re.sub(r"[.!?,]+$", "", text.strip())
        words = phrase.split()
        if not words or len(words) > MAX_CLARIFY_WORDS or _NEW_REQUEST.match(phrase):
            return None
        phrase = _PART_OF_DAY.get(phrase.lower(), phrase)
        current = getattr(pending.action.arguments, pending.field, None)
        value = f"{current} {phrase}".strip() if pending.merge and current else phrase
        try:
            arguments = pending.action.arguments.model_copy(update={pending.field: value})
            action = TaskAction(name=pending.action.name, arguments=arguments)
            self._clarify[session_id] = pending  # so a repeated question counts this attempt
            outcome = self._execute(action, session_id)
            if self._clarify.get(session_id) is pending:
                del self._clarify[session_id]  # answered (or a question without a fillable field): nothing left open
        except Exception as exc:  # noqa: BLE001
            self._clarify.pop(session_id, None)
            return self._failure(exc)
        return outcome

    def correct_reminder(self, phrase: str, session_id: str) -> "ActionOutcome | None":
        """"Make that 6 PM" right after "remind me at 5": replace the reminder just created (cancel + create, through the same
        permission path). None when there is nothing recent to correct."""
        tool = self._tools.get("create_reminder")
        planner = getattr(tool, "plan_correction", None)
        if planner is None or self._permissions is None:
            return None
        try:
            plan = planner(session_id, phrase)
            if plan is None:
                return None
            if isinstance(plan, Clarify):
                return ActionOutcome(plan.message)
            params = {**plan.params, "origin_session": session_id}
            request = self._permissions.request_permission(
                tool_name=tool.name, action=EXECUTE_ACTION, description="Use the create_reminder tool (correction)",
                parameters=params, session_id=session_id, requested_by="agent",
            )
            if request.status is PermissionStatus.APPROVED:
                return self._run(tool, request, params, session_id)
            return ActionOutcome(DENIED_REPLY, request)
        except Exception as exc:  # noqa: BLE001
            return self._failure(exc)

    def execute(self, action: TaskAction, session_id: str) -> ActionOutcome:
        """Never raises. A confirmation still pending from an earlier request is dropped first."""
        self._pending.pop(session_id, None)
        self._clarify.pop(session_id, None)
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
            if resolution.field is not None:
                previous = self._clarify.get(session_id)
                attempts = previous.attempts + 1 if previous is not None else 1
                if attempts <= 3:  # after three failed answers stop asking; the user can start over
                    self._clarify[session_id] = _PendingClarify(
                        action, resolution.field, resolution.merge, self._clock() + timedelta(seconds=CLARIFICATION_TTL_SECONDS), attempts)
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
            return ActionOutcome(resolution.confirm_prompt, request, history_text=getattr(tool, "prompt_history_placeholder", None))
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
        return ActionOutcome(str(reply), request, executed=True, history_text=getattr(tool, "history_placeholder", None))

    @staticmethod
    def _failure(exc: Exception) -> ActionOutcome:
        if isinstance(exc, PermissionDenied):
            logger.warning("Task tool was not authorized")
            return ActionOutcome(DENIED_REPLY)
        spoken = getattr(exc, "user_message", None)
        if isinstance(spoken, str) and spoken:  # integration errors (Gmail) carry a speakable, content-free message
            logger.warning("Action failed (%s)", type(exc).__name__)
            return ActionOutcome(spoken)
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
