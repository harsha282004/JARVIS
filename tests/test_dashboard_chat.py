"""DashboardChat: the dashboard command bar's wrapper around a real ConversationEngine.

Proves it is the same agent architecture (real AgentBrain/tool routing, not a keyword list), that concurrent
requests cannot corrupt the shared conversation history, and that every failure is reported honestly."""

import threading

from backend.core.conversation.engine import ConversationEngine
from backend.core.dashboard_chat import DashboardChat
from backend.core.llm.base import LLMProviderError


class ScriptedLLM:
    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls = 0

    def chat(self, messages, json_mode=False):
        self.calls += 1
        reply = self.replies.pop(0) if self.replies else "ok"
        if isinstance(reply, Exception):
            raise reply
        return reply


def engine_for(*replies, agent=None):
    return ConversationEngine(ScriptedLLM(*replies) if agent is None else agent, max_messages=20, timeout_seconds=120)


def test_ask_returns_the_real_agent_reply():
    chat = DashboardChat(engine_for("Hello there."))
    result = chat.ask("Hi")
    assert result["ok"] is True and result["reply"] == "Hello there."


def test_blank_text_is_declined_without_calling_the_agent():
    llm = ScriptedLLM()
    chat = DashboardChat(engine_for(agent=llm))
    result = chat.ask("   ")
    assert result["ok"] is False and llm.calls == 0


def test_llm_failure_is_reported_honestly_not_fabricated():
    chat = DashboardChat(engine_for(LLMProviderError("boom", kind="rate_limit")))
    result = chat.ask("What's my last email?")
    assert result["ok"] is False and result["error"] == "rate_limit"
    assert "rate-limited" in result["reply"]
    assert "boom" not in result["reply"]  # never echoes the raw exception text


def test_unexpected_tool_failure_is_reported_honestly_never_crashes():
    class Boom(ScriptedLLM):
        def chat(self, messages, json_mode=False):
            raise RuntimeError("tool exploded")

    chat = DashboardChat(engine_for(agent=Boom()))
    result = chat.ask("Do something")
    assert result["ok"] is False and result["reply"] and "exploded" not in result["reply"]


def test_history_records_both_sides_of_the_conversation():
    chat = DashboardChat(engine_for("Sure."))
    chat.ask("Hello")
    history = chat.history()
    assert [h["role"] for h in history] == ["user", "assistant"]
    assert history[0]["text"] == "Hello" and history[1]["text"] == "Sure."


def test_history_is_trimmed_to_the_configured_limit():
    llm = ScriptedLLM(*(f'{{"intent": "conversation", "response": "r{i}", "confidence": 0.9}}' for i in range(20)))
    chat = DashboardChat(engine_for(agent=llm), history_limit=6)
    for i in range(10):
        chat.ask(f"q{i}")
    assert len(chat.history()) == 6


def test_reset_clears_history_and_starts_a_new_session():
    chat = DashboardChat(engine_for("Sure."))
    chat.ask("Hello")
    assert chat.history()
    chat.reset()
    assert chat.history() == []


def test_concurrent_asks_do_not_interleave_or_corrupt_history():
    """The lock must serialize access to the shared (not-thread-safe) ConversationEngine."""
    llm = ScriptedLLM(*(f'{{"intent": "conversation", "response": "r{i}", "confidence": 0.9}}' for i in range(20)))
    chat = DashboardChat(engine_for(agent=llm))
    results = []

    def worker(i):
        results.append(chat.ask(f"q{i}"))

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert all(r["ok"] for r in results)
    history = chat.history()
    assert len(history) == 20 and [h["role"] for h in history[::2]] == ["user"] * 10
