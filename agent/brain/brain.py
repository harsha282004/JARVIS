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
        self, user_text: str, context: Sequence[Message], memory_context: str = ""
    ) -> AgentRequest:
        """Wrap a user message and the conversation context supplied by
        ConversationEngine, attaching the tool descriptions this brain knows."""
        return AgentRequest(
            user_text=user_text, context=list(context), tools=self._tools, memory_context=memory_context,
            documents_enabled=self._documents_enabled,
        )

    def decide(self, request: AgentRequest) -> AgentDecision:
        """Return a structured decision. Raises LLMProviderError if the LLM is
        unavailable (nothing is fabricated); malformed model output is handled
        here and yields a safe fallback decision with `error` set."""
        logger.info("Agent request received (context_messages=%d)", len(request.context))
        messages = [
            Message(Role.SYSTEM, build_system_prompt(request.tools, request.memory_context, request.documents_enabled)),
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
        selections = [self._select(name, catalog) for name in output.tools]
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
        )

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
