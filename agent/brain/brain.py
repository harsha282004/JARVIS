"""AgentBrain: turns a user request plus conversation context into a structured decision.

    ConversationEngine -> AgentBrain -> LLMProvider

The brain reasons, classifies, plans and selects tools *by name*. It holds
only `ToolDescriptor`s, never Tool objects, and imports nothing that can
run code, so it has no path to the OS or any external system. Executing a
plan (via PermissionManager and a Tool) is a later phase.
"""

from collections.abc import Sequence

from agent.brain.models import (
    AgentDecision,
    AgentError,
    AgentErrorCode,
    AgentRequest,
    Intent,
    ToolSelection,
)
from agent.brain.parsing import InvalidAgentOutput, LLMDecisionOutput, parse_decision_output
from agent.brain.prompts import RETRY_PROMPT, build_system_prompt
from agent.planner.planner import Planner
from agent.events.intents import EVENT_ACTION_NAMES, EventAction, InvalidEventAction, parse_event_action
from integrations.calendar.intents import (
    CALENDAR_ACTION_NAMES,
    CalendarAction,
    InvalidCalendarAction,
    parse_calendar_action,
)
from agent.tasks.intents import InvalidTaskAction, TaskAction, parse_task_action
from integrations.gmail.intents import (
    GMAIL_ACTION_NAMES,
    GmailAction,
    InvalidGmailAction,
    parse_gmail_action,
)
from agent.tools.base import ToolDescriptor
from backend.core.llm.base import LLMProvider
from backend.core.llm.messages import Message, Role
from backend.core.logging import get_logger

logger = get_logger(__name__)

# One repair attempt if the model's first reply is not a valid decision.
MAX_ATTEMPTS = 2

ACTION_RESPONSE = (
    "I understand what you're asking, but I can't carry out actions like that yet."
)
UNSUPPORTED_RESPONSE = "Sorry, I can't help with that."
# Placeholder only: ConversationEngine replaces it with the grounded answer (or an honest unavailable message).
DOCUMENT_LOOKUP_RESPONSE = "Let me check your documents."
FALLBACK_RESPONSE = "Sorry, I had trouble working that out. Could you say it again?"

_DIRECT_INTENTS = {Intent.CONVERSATION, Intent.INFORMATION_REQUEST, Intent.CLARIFICATION_REQUIRED}


class AgentBrain:
    def __init__(
        self,
        llm: LLMProvider,
        tools: Sequence[ToolDescriptor],
        max_plan_steps: int,
        documents_enabled: bool = False,
    ):
        self._llm = llm
        self._documents_enabled = documents_enabled
        self._tools = list(tools)
        self._planner = Planner(max_plan_steps)

    def build_request(
        self, user_text: str, context: Sequence[Message], memory_context: str = "", graph_context: str = ""
    ) -> AgentRequest:
        """Wrap a user message and the conversation context supplied by
        ConversationEngine, attaching the tool descriptions this brain knows."""
        return AgentRequest(
            user_text=user_text, context=list(context), tools=self._tools, memory_context=memory_context,
            documents_enabled=self._documents_enabled, graph_context=graph_context,
        )

    def decide(self, request: AgentRequest) -> AgentDecision:
        """Return a structured decision. Raises LLMProviderError if the LLM is
        unavailable (nothing is fabricated); malformed model output is handled
        here and yields a safe fallback decision with `error` set."""
        logger.info("Agent request received (context_messages=%d)", len(request.context))
        messages = [
            Message(Role.SYSTEM, build_system_prompt(
                    request.tools, request.memory_context, request.documents_enabled, request.graph_context
                )),
            *request.context,
            Message(Role.USER, request.user_text),
        ]

        last_problem = "no attempt made"
        for attempt in range(1, MAX_ATTEMPTS + 1):
            raw = self._llm.chat(messages, json_mode=True)
            try:
                decision = self._interpret(raw, request)
            except InvalidAgentOutput as exc:
                last_problem = str(exc)
                logger.warning("Agent output validation failed (attempt %d): %s", attempt, exc)
                messages = [*messages, Message(Role.ASSISTANT, raw), Message(Role.USER, RETRY_PROMPT)]
                continue
            self._log_decision(decision)
            return decision

        logger.error("Agent gave no valid decision; using safe fallback")
        return AgentDecision(
            intent=Intent.CONVERSATION,
            action_required=False,
            response=FALLBACK_RESPONSE,
            confidence=0.0,
            reasoning_summary="Could not interpret the model output.",
            error=AgentError(code=AgentErrorCode.INVALID_OUTPUT, detail=last_problem),
        )

    def _interpret(self, raw: str, request: AgentRequest) -> AgentDecision:
        output = parse_decision_output(raw)
        if output.action is not None and output.intent is Intent.INFORMATION_REQUEST:
            output = output.model_copy(update={"intent": Intent.ACTION_REQUEST})  # "any unread mail?" with a Gmail action
        if output.intent in _DIRECT_INTENTS:
            response = output.response.strip()
            if not response:
                raise InvalidAgentOutput(f"empty response for intent {output.intent.value}")
            return AgentDecision(
                intent=output.intent,
                action_required=False,
                response=response,
                confidence=output.confidence,
                reasoning_summary=output.summary,
            )
        if output.intent is Intent.DOCUMENT_QUESTION:
            return AgentDecision(
                intent=output.intent,
                action_required=False,
                response=DOCUMENT_LOOKUP_RESPONSE,
                confidence=output.confidence,
                reasoning_summary=output.summary,
                search_query=output.query or request.user_text[:300],
            )
        if output.intent is Intent.UNSUPPORTED_REQUEST:
            return AgentDecision(
                intent=output.intent,
                action_required=False,
                response=UNSUPPORTED_RESPONSE,
                confidence=output.confidence,
                reasoning_summary=output.summary,
            )
        return self._action_decision(output, request)

    def _action_decision(self, output: LLMDecisionOutput, request: AgentRequest) -> AgentDecision:
        catalog = {tool.name.lower(): tool for tool in request.tools}
        task_action, gmail_action, event_action, calendar_action = self._proposed_action(output, catalog)
        proposed = task_action or gmail_action or event_action or calendar_action
        names = list(output.tools)
        if proposed is not None and proposed.name.value not in {n.lower() for n in names}:
            names.append(proposed.name.value)  # the action itself names the tool it needs
        selections = [self._select(name, catalog) for name in names]
        # Fail-safe: permission is required unless every tool is known and says it is not.
        requires_permission = not selections or any(
            (not s.available) or s.requires_permission for s in selections
        )
        plan = self._planner.build_plan(
            goal=output.summary or "Carry out the user's request",
            prepare_steps=output.steps,
            tools=[s.name for s in selections],
            requires_permission=requires_permission,
        )
        return AgentDecision(
            intent=Intent.ACTION_REQUEST,
            action_required=True,
            plan=plan,
            response=ACTION_RESPONSE,
            selected_tools=selections,
            requires_permission=requires_permission,
            confidence=output.confidence,
            reasoning_summary=output.summary,
            task_action=task_action,
            gmail_action=gmail_action,
            event_action=event_action,
            calendar_action=calendar_action,
        )

    @staticmethod
    def _proposed_action(
        output: LLMDecisionOutput, catalog: dict[str, ToolDescriptor]
    ) -> tuple[TaskAction | None, GmailAction | None, EventAction | None, CalendarAction | None]:
        """Validate the model's proposed action (task/reminder or Gmail). An invalid one makes the whole output
        invalid (retry, then the safe fallback). A valid action for a tool that is not available (e.g. Gmail is
        turned off) is dropped."""
        if output.action is None:
            return None, None, None, None
        name = output.action.get("name")
        try:
            if isinstance(name, str) and name.strip().lower() in GMAIL_ACTION_NAMES:
                gmail = parse_gmail_action(output.action)
                return None, (gmail if gmail.name.value in catalog else None), None, None
            if isinstance(name, str) and name.strip().lower() in EVENT_ACTION_NAMES:
                event = parse_event_action(output.action)
                return None, None, (event if event.name.value in catalog else None), None
            if isinstance(name, str) and name.strip().lower() in CALENDAR_ACTION_NAMES:
                calendar = parse_calendar_action(output.action)
                return None, None, None, (calendar if calendar.name.value in catalog else None)
            task = parse_task_action(output.action)
        except (InvalidTaskAction, InvalidGmailAction, InvalidEventAction, InvalidCalendarAction) as exc:
            raise InvalidAgentOutput(f"invalid action ({exc})") from None
        return (task if task.name.value in catalog else None), None, None, None

    @staticmethod
    def _select(name: str, catalog: dict[str, ToolDescriptor]) -> ToolSelection:
        descriptor = catalog.get(name.lower())
        if descriptor is None:
            return ToolSelection(name=" ".join(name.split())[:64], available=False, requires_permission=True)
        return ToolSelection(
            name=descriptor.name,
            available=True,
            requires_permission=descriptor.requires_permission,
        )

    @staticmethod
    def _log_decision(decision: AgentDecision) -> None:
        logger.info(
            "Agent intent=%s confidence=%.2f action_required=%s summary=%r",
            decision.intent.value,
            decision.confidence,
            decision.action_required,
            decision.reasoning_summary,
        )
        if decision.action_required:
            logger.info(
                "Agent action planned (tools=%s, missing=%s, requires_permission=%s, steps=%d)",
                [t.name for t in decision.selected_tools],
                decision.missing_tools,
                decision.requires_permission,
                len(decision.plan.steps) if decision.plan else 0,
            )
