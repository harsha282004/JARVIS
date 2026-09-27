"""DashboardChat: the dashboard's "Ask JARVIS anything" command bar, talking to the SAME agent architecture as voice.

    Dashboard command bar -> DashboardChat -> ConversationEngine -> AgentBrain -> Planner -> Tool Router
        -> PermissionManager -> Tool (Gmail/Calendar/Tasks/...) -> Result -> AgentBrain -> reply -> dashboard

This is not a second, simplified chatbot: `ConversationEngine` is built the exact same way voice builds its own
(`voice.bootstrap.build_conversation_engine`, the same function `build_voice_engine` calls), so it carries the same
LLM provider, the same registered tools, the same `AgentBrain` (LLM-driven intent/tool routing, never a keyword
list), and the same `PermissionManager`. It is a SEPARATE instance and conversation session from the voice engine's
own, because `ConversationEngine` is documented as not thread-safe and is normally driven by the single voice worker
thread; giving the dashboard its own instance (with its own lock, since HTTP requests can still overlap each other)
avoids a real concurrency bug without weakening what "the same architecture" means -- every tool, permission check
and security property is identical, only the conversation history is separate, exactly as two independent
conversations naturally would be.
"""

import threading
import time
from dataclasses import dataclass, field
from typing import Any

from backend.core.conversation.engine import ConversationEngine
from backend.core.llm.base import LLMProviderError, describe_llm_error
from backend.core.logging import get_logger

logger = get_logger(__name__)


@dataclass
class ChatTurn:
    role: str  # "user" | "assistant"
    text: str
    at: float = field(default_factory=time.time)


class DashboardChat:
    """Thread-safe wrapper: at most one `respond()` call runs at a time (concurrent dashboard requests queue briefly
    rather than corrupting the shared conversation history), and every failure is reported honestly, never fabricated."""

    def __init__(self, engine: ConversationEngine, history_limit: int = 40):
        self._engine = engine
        self._lock = threading.Lock()
        self._history_limit = history_limit
        self._history: list[ChatTurn] = []

    def ask(self, text: str) -> dict[str, Any]:
        text = (text or "").strip()
        if not text:
            return {"ok": False, "reply": "Say something and I'll help.", "error": None}
        with self._lock:
            self._history.append(ChatTurn("user", text))
            try:
                reply = self._engine.respond(text)
            except LLMProviderError as exc:
                kind = getattr(exc, "kind", "unavailable")
                logger.error("Dashboard chat LLM request failed (kind=%s): %s", kind, exc)
                message = describe_llm_error(exc)
                self._history.append(ChatTurn("assistant", message))
                self._trim()
                return {"ok": False, "reply": message, "error": kind}
            except Exception as exc:  # noqa: BLE001 - a tool/agent-layer failure must still answer honestly, never crash the dashboard
                logger.exception("Dashboard chat request failed")
                message = "Something went wrong handling that. Please try again."
                self._history.append(ChatTurn("assistant", message))
                self._trim()
                return {"ok": False, "reply": message, "error": type(exc).__name__}
            decision = self._engine.last_decision
            intent = decision.intent.value if decision is not None else None
            tool = self._engine.last_action_name
            self._history.append(ChatTurn("assistant", reply))
            self._trim()
            return {"ok": True, "reply": reply, "intent": intent, "tool": tool, "error": None}

    def _trim(self) -> None:
        if len(self._history) > self._history_limit:
            del self._history[: len(self._history) - self._history_limit]

    def history(self) -> list[dict[str, Any]]:
        with self._lock:
            return [{"role": t.role, "text": t.text, "at": t.at} for t in self._history]

    def reset(self) -> None:
        """Start a fresh conversation (dashboard "New chat"). Never called mid-response: `ask()` holds the lock."""
        with self._lock:
            self._engine.reset()
            self._history.clear()
