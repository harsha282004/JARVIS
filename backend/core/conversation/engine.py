"""ConversationEngine: session, message history, context building, timeout, reset.

Sits between VoiceEngine and the LLM. It knows nothing about audio, wake
words or Ollama: it turns a user's text into a context-aware request and
records the exchange. With an `AgentBrain` (Phase 4) the request goes to the
brain, which classifies it and decides the reply; without one it goes
straight to the `LLMProvider`. This class remains the only owner of history. State is in memory only and
the engine is not thread-safe (it is used from the single voice worker).
"""

from collections.abc import Callable, Sequence
from datetime import datetime, timedelta, timezone

from agent.brain.brain import AgentBrain
from agent.brain.models import AgentDecision, Intent
from agent.brain.permissions import request_permissions
from agent.knowledge_graph.context import GraphContextProvider, build_graph_block
from agent.memory.context import build_memory_block
from agent.memory.service import MemoryService
from agent.rag.grounding import RAG_DISABLED_RESPONSE
from agent.rag.models import RAGAnswer
from agent.rag.service import RagService
from agent.tasks.executor import TaskActionExecutor
from backend.core.conversation.models import ConversationSession
from backend.core.conversation.prompts import SYSTEM_PROMPT
from backend.core.llm.base import LLMProvider
from backend.core.llm.messages import Message, Role
from backend.core.logging import get_logger
from backend.core.events import EventBus, SystemEvent
from backend.core.security import PermissionManager, PermissionRequest

logger = get_logger(__name__)

MIN_MAX_MESSAGES = 2  # room for the current user message plus one reply


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class ConversationEngine:
    def __init__(
        self,
        llm: LLMProvider,
        max_messages: int,
        timeout_seconds: float,
        system_prompt: str = SYSTEM_PROMPT,
        clock: Callable[[], datetime] = _utcnow,
        agent: AgentBrain | None = None,
        permissions: PermissionManager | None = None,
        memory: MemoryService | None = None,
        rag: RagService | None = None,
        graph: GraphContextProvider | None = None,
        actions: TaskActionExecutor | None = None,
        intelligence=None,
        bus: EventBus | None = None,
    ):
        if max_messages < MIN_MAX_MESSAGES:
            raise ValueError(f"max_messages must be at least {MIN_MAX_MESSAGES}")
        self._llm = llm
        self._max_messages = max_messages
        self._timeout = timedelta(seconds=timeout_seconds)
        self._system_prompt = system_prompt
        self._clock = clock
        self._agent = agent
        self._permissions = permissions
        self._memory = memory
        self._rag = rag
        self._graph = graph
        self._actions = actions
        self._intelligence = intelligence  # Phase 17: deterministic cross-source answers (agent.intelligence.router.IntelligenceRouter)
        self._intel_service = getattr(intelligence, "service", None)
        self._bus = bus
        self._session: ConversationSession | None = None
        self.last_decision: AgentDecision | None = None
        self.last_permission_requests: list[PermissionRequest] = []
        self.last_rag_answer: RAGAnswer | None = None
        self._action_handled = False
        self._history_override: str | None = None

    @property
    def session(self) -> ConversationSession | None:
        """The current session, or None (IDLE) if there is none or it timed out."""
        self._expire_if_inactive()
        return self._session

    @property
    def is_active(self) -> bool:
        return self.session is not None

    def respond(self, user_text: str) -> str:
        """Add `user_text` to the conversation and return the assistant reply.

        Nothing is modified until the LLM succeeds, so a failure
        (LLMProviderError) leaves the conversation exactly as it was (a first
        turn that fails creates no session) and the next turn can proceed;
        the failed user message is not kept.
        """
        text = user_text.strip()
        if not text:
            raise ValueError("user_text must not be blank")

        session = self.session
        is_new = session is None
        if session is None:
            session = self._new_session()
        user_message = Message(Role.USER, text, self._clock())

        self.last_rag_answer = None
        memory_block = self._memory_block(text)
        graph_block = self._graph_block(text)
        reply = self._generate_reply(session, user_message, memory_block, graph_block)

        if is_new:
            self._session = session
            logger.info("Conversation session started (session=%s)", session.session_id)
        session.add(user_message)
        # Replies built from email text are kept out of the history: the model must never read attacker-written text as context.
        session.add(Message(Role.ASSISTANT, self._history_override or reply, self._clock()))
        session.messages[:] = self._window(session.messages)
        self._request_permissions(session.session_id)
        self._remember(text)
        self._observe(text, reply)
        logger.info(
            "Conversation turn completed (session=%s, messages=%d)",
            session.session_id,
            len(session.messages),
        )
        return reply

    def reset(self) -> None:
        """End the current session and discard its history; the next turn starts a new one."""
        if self._session is not None:
            self._end_session("reset")

    def _answer_from_documents(
        self, decision: AgentDecision, user_message: Message, history: Sequence[Message], memory_block: str
    ) -> str:
        """Answer a document question from the user's indexed documents (Phase 7).

        History stays owned by this engine and is only passed through. The
        answer is never stored as personal memory. If the LLM fails, the
        exception propagates and nothing is recorded, as for any other turn."""
        if self._rag is None:
            return RAG_DISABLED_RESPONSE
        answer = self._rag.answer(
            decision.search_query or user_message.content, history, user_message.content, memory_block
        )
        self.last_rag_answer = answer
        logger.info(
            "Document question answered (status=%s, sources=%d)", answer.status.value, len(answer.sources)
        )
        return answer.answer

    def _graph_block(self, text: str) -> str:
        """Relevant knowledge-graph facts as a delimited block. A graph failure means no graph
        context (never invented facts) and the turn continues."""
        if self._graph is None:
            return ""
        try:
            return build_graph_block(self._graph.context_for(text))
        except Exception as exc:  # noqa: BLE001 - the graph is optional; log the type only
            logger.warning("Graph retrieval failed; continuing without graph context (%s)", type(exc).__name__)
            return ""

    def _memory_block(self, text: str) -> str:
        """Relevant stored memories as a delimited block. Any memory failure means
        no memory context (never invented memories) and the turn continues."""
        if self._memory is None:
            return ""
        try:
            memories = self._memory.retrieve(text)
            if self._intel_service is not None:
                memories = self._intel_service.rerank_memories(text, memories)  # drop memories with no link to this request
            return build_memory_block(memories)
        except Exception as exc:  # noqa: BLE001 - memory is optional; log the type only (messages may echo content)
            logger.warning("Memory retrieval failed; continuing without memory (%s)", type(exc).__name__)
            return ""

    def _observe(self, user_text: str, reply: str) -> None:
        """Let the intelligence layer note which of the user's things this turn was about (so "when is it due?" can resolve "it"), and tell
        the rest of JARVIS what happened. Both are best effort."""
        if self._intel_service is not None:
            self._intel_service.observe_turn(user_text, reply)
        if self._bus is not None:
            executed = bool(self._action_handled and self.last_decision is not None)
            self._bus.publish(SystemEvent.AGENT_RESPONSE, executed=executed)

    def _remember(self, text: str) -> None:
        """After a completed turn, extract memories from the USER's words only.
        Skipped when the agent fell back to a safe reply. A failure saves nothing."""
        decision = self.last_decision
        if self._memory is None or (decision is not None and decision.error is not None):
            return
        try:
            self._memory.process_utterance(text)
        except Exception as exc:  # noqa: BLE001 - the completed conversation must survive a memory failure
            logger.warning("Memory extraction/storage failed; nothing saved (%s)", type(exc).__name__)

    def _generate_reply(
        self, session: ConversationSession, user_message: Message, memory_block: str = "", graph_block: str = ""
    ) -> str:
        self._action_handled = False
        self._history_override = None
        confirmed = self._answer_confirmation(session, user_message)
        if confirmed is not None:
            return confirmed
        handled = self._answer_from_intelligence(session, user_message)
        if handled is not None:
            return handled
        context = self._build_context(session, user_message, "\n\n".join(b for b in (memory_block, graph_block) if b))
        if self._agent is None:
            return self._llm.chat(context)
        # context = [system prompt, *history, current user message]; the brain
        # has its own prompt, so it gets the history and the message separately.
        request = self._agent.build_request(user_message.content, context[1:-1], memory_block, graph_block)
        decision = self._agent.decide(request)
        self.last_decision = decision
        if (decision.task_action or decision.gmail_action or decision.event_action or decision.calendar_action or decision.message_action or decision.proactive_action or decision.briefing_action) is not None and self._actions is not None:
            return self._carry_out_action(decision, session)
        if decision.intent is Intent.DOCUMENT_QUESTION:
            return self._answer_from_documents(
                decision, user_message, context[1:-1], "\n\n".join(b for b in (memory_block, graph_block) if b)
            )
        return decision.response

    def _answer_from_intelligence(self, session: ConversationSession, user_message: Message) -> str | None:
        """Phase 17: questions the personal intelligence layer answers deterministically (plans, focus, conflicts, "why?", references,
        the answer to its own pending confirmation). No LLM call is made, so this also works when the LLM or the internet is down.
        Its replies contain the user's own tasks/calendar/email titles, so like other such replies they stay out of the history."""
        if self._intelligence is None:
            return None
        try:
            reply = self._intelligence.handle(user_message.content, session.session_id)
        except Exception as exc:  # noqa: BLE001 - the layer is optional; a failure must not break the conversation
            logger.error("Intelligence layer failed (%s)", type(exc).__name__)
            return None
        if reply is None:
            return None
        self.last_decision = None
        self._action_handled = True
        self._history_override = reply.history_text
        self.last_permission_requests = []
        return reply.text

    def _answer_confirmation(self, session: ConversationSession, user_message: Message) -> str | None:
        """If the user is answering a pending "do you want me to cancel ...?" question, handle it here,
        deterministically and without the LLM. Anything that is not a clear yes/no is handled as a new request."""
        if self._actions is None:
            return None
        outcome = self._actions.confirm(user_message.content, session.session_id)
        if outcome is None:
            return None
        self.last_decision = None
        self._action_handled = True
        self._history_override = outcome.history_text  # replies built from email/event text stay out of the history
        self.last_permission_requests = [outcome.permission_request] if outcome.permission_request else []
        return outcome.reply

    def _carry_out_action(self, decision: AgentDecision, session: ConversationSession) -> str:
        """Hand a validated task/reminder action to the executor (PermissionManager -> tool -> service).
        The reply says only what actually happened."""
        action = (
            decision.task_action or decision.gmail_action or decision.event_action or decision.calendar_action
            or decision.message_action or decision.proactive_action or decision.briefing_action
        )
        assert self._actions is not None and action is not None
        outcome = self._actions.execute(action, session.session_id)
        self._action_handled = True
        self._history_override = outcome.history_text
        self.last_permission_requests = [outcome.permission_request] if outcome.permission_request else []
        logger.info("Action handled (action=%s, executed=%s)", action.name.value, outcome.executed)
        return outcome.reply

    def _request_permissions(self, session_id: str) -> None:
        """Ask the PermissionManager about the decision's tools. Never approves
        or runs anything; a failure here only means no request was recorded."""
        if self._action_handled:  # the executor already went through the PermissionManager for this turn
            return
        self.last_permission_requests = []
        decision = self.last_decision
        if self._permissions is None or decision is None or not decision.action_required:
            return
        try:
            self.last_permission_requests = request_permissions(decision, self._permissions, session_id)
        except Exception:  # noqa: BLE001 - a security-side failure must not break the reply; nothing was authorized
            logger.exception("Could not record permission requests; nothing was authorized")

    def _build_context(
        self, session: ConversationSession, user_message: Message, memory_block: str = ""
    ) -> Sequence[Message]:
        prompt = f"{self._system_prompt}\n\n{memory_block}" if memory_block and self._agent is None else self._system_prompt
        system = Message(Role.SYSTEM, prompt, self._clock())
        return [system, *self._window([*session.messages, user_message])]

    def _window(self, messages: Sequence[Message]) -> list[Message]:
        """Keep the most recent `max_messages`, starting on a user message.

        The newest message is never dropped. A leading assistant reply whose
        question was trimmed away is dropped too, as it has lost its context.
        """
        window = list(messages[-self._max_messages :])
        while len(window) > 1 and window[0].role is not Role.USER:
            window.pop(0)
        return window

    def _new_session(self) -> ConversationSession:
        now = self._clock()
        return ConversationSession(created_at=now, last_activity=now)

    def _expire_if_inactive(self) -> None:
        session = self._session
        if session is not None and self._clock() - session.last_activity > self._timeout:
            self._end_session("timeout")

    def _end_session(self, reason: str) -> None:
        session, self._session = self._session, None
        if self._permissions is not None:
            self._permissions.end_session(session.session_id)
        if self._intel_service is not None:
            self._intel_service.end_session(session.session_id)  # a pending confirmation never outlives its conversation
        logger.info("Conversation session ended (session=%s, reason=%s)", session.session_id, reason)
        session.end()
