"""ConversationEngine: session, message history, context building, timeout, reset.

Sits between VoiceEngine and LLMProvider. It knows nothing about audio,
wake words or Ollama: it turns a user's text into a context-aware request
for any `LLMProvider` and records the exchange. State is in memory only and
the engine is not thread-safe (it is used from the single voice worker).
"""

from collections.abc import Callable, Sequence
from datetime import datetime, timedelta, timezone

from backend.core.conversation.models import ConversationSession
from backend.core.conversation.prompts import SYSTEM_PROMPT
from backend.core.llm.base import LLMProvider
from backend.core.llm.messages import Message, Role
from backend.core.logging import get_logger

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
    ):
        if max_messages < MIN_MAX_MESSAGES:
            raise ValueError(f"max_messages must be at least {MIN_MAX_MESSAGES}")
        self._llm = llm
        self._max_messages = max_messages
        self._timeout = timedelta(seconds=timeout_seconds)
        self._system_prompt = system_prompt
        self._clock = clock
        self._session: ConversationSession | None = None

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

        reply = self._llm.chat(self._build_context(session, user_message))

        if is_new:
            self._session = session
            logger.info("Conversation session started (session=%s)", session.session_id)
        session.add(user_message)
        session.add(Message(Role.ASSISTANT, reply, self._clock()))
        session.messages[:] = self._window(session.messages)
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

    def _build_context(
        self, session: ConversationSession, user_message: Message
    ) -> Sequence[Message]:
        system = Message(Role.SYSTEM, self._system_prompt, self._clock())
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
        logger.info("Conversation session ended (session=%s, reason=%s)", session.session_id, reason)
        session.end()
