"""Unit tests for ConversationEngine with a fake LLM and a fake clock.

They verify what the engine sends to the LLM and how session state changes.
They do not (and cannot) prove real conversational quality; see the
integration test for a real Ollama check.
"""

import uuid
from datetime import datetime, timedelta, timezone

import pytest

from backend.core.conversation.engine import ConversationEngine
from backend.core.conversation.models import SessionState
from backend.core.conversation.prompts import SYSTEM_PROMPT
from backend.core.llm.base import LLMProvider, LLMProviderError
from backend.core.llm.messages import Message, Role


class FakeLLM(LLMProvider):
    """Records every request; replies "reply N"; can be told to fail."""

    def __init__(self):
        self.requests: list[list[Message]] = []
        self.fail_next = False

    def chat(self, messages):
        self.requests.append(list(messages))
        if self.fail_next:
            self.fail_next = False
            raise LLMProviderError("boom")
        return f"reply {len(self.requests)}"


class FakeClock:
    def __init__(self):
        self.now = datetime(2030, 1, 1, tzinfo=timezone.utc)

    def __call__(self):
        return self.now

    def advance(self, seconds: float):
        self.now += timedelta(seconds=seconds)


def make(max_messages=20, timeout=120.0):
    llm, clock = FakeLLM(), FakeClock()
    engine = ConversationEngine(llm, max_messages, timeout, clock=clock)
    return engine, llm, clock


def roles_and_text(messages):
    return [(m.role.value, m.content) for m in messages]


def test_starts_idle_with_no_session():
    engine, _, _ = make()
    assert engine.session is None
    assert engine.is_active is False


def test_first_turn_creates_active_session_with_uuid_id():
    engine, _, clock = make()
    engine.respond("hello")

    session = engine.session
    assert session.is_active and session.state is SessionState.ACTIVE
    assert uuid.UUID(session.session_id).version == 4
    assert session.created_at == clock.now
    assert session.last_activity == clock.now


def test_user_and_assistant_messages_recorded_in_order_with_timestamps():
    engine, _, clock = make()
    engine.respond("first")
    clock.advance(5)
    engine.respond("second")

    messages = engine.session.messages
    assert roles_and_text(messages) == [
        ("user", "first"),
        ("assistant", "reply 1"),
        ("user", "second"),
        ("assistant", "reply 2"),
    ]
    assert [m.timestamp for m in messages] == sorted(m.timestamp for m in messages)
    assert engine.session.last_activity == clock.now


def test_first_request_is_system_prompt_plus_current_message():
    engine, llm, _ = make()
    engine.respond("What is Python?")

    assert roles_and_text(llm.requests[0]) == [
        ("system", SYSTEM_PROMPT),
        ("user", "What is Python?"),
    ]


def test_follow_up_receives_previous_turn():
    engine, llm, _ = make()
    engine.respond("What is Python?")
    engine.respond("Who created it?")

    assert roles_and_text(llm.requests[1]) == [
        ("system", SYSTEM_PROMPT),
        ("user", "What is Python?"),
        ("assistant", "reply 1"),
        ("user", "Who created it?"),
    ]


def test_second_conversation_topic_gets_its_own_context():
    engine, llm, _ = make()
    engine.respond("Tell me about Bengaluru.")
    engine.respond("What is its population?")

    contents = [m.content for m in llm.requests[1]]
    assert "Tell me about Bengaluru." in contents
    assert contents[-1] == "What is its population?"


def test_system_prompt_is_never_stored_in_history():
    engine, _, _ = make()
    engine.respond("hi")
    assert all(m.role is not Role.SYSTEM for m in engine.session.messages)


def test_blank_input_is_rejected_without_calling_llm():
    engine, llm, _ = make()
    with pytest.raises(ValueError):
        engine.respond("   ")
    assert llm.requests == []
    assert engine.session is None


def test_history_is_limited_to_max_messages_keeping_latest():
    engine, llm, _ = make(max_messages=4)
    for i in range(1, 6):
        engine.respond(f"q{i}")

    assert roles_and_text(engine.session.messages) == [
        ("user", "q4"),
        ("assistant", "reply 4"),
        ("user", "q5"),
        ("assistant", "reply 5"),
    ]
    # Next request: at most max_messages of history, ending with the new user message.
    engine.respond("q6")
    sent = roles_and_text(llm.requests[-1])
    assert sent[0][0] == "system"
    assert sent[-1] == ("user", "q6")
    assert len(sent) - 1 <= 4


def test_context_window_never_starts_with_orphaned_assistant_reply():
    engine, llm, _ = make(max_messages=3)
    for i in range(1, 5):
        engine.respond(f"q{i}")

    for request in llm.requests:
        history = request[1:]
        assert history[0].role is Role.USER
        assert history[-1].role is Role.USER


def test_current_user_message_and_latest_reply_survive_minimum_window():
    engine, llm, _ = make(max_messages=2)
    engine.respond("a")
    engine.respond("b")

    assert roles_and_text(llm.requests[1])[1:] == [("user", "b")]
    assert roles_and_text(engine.session.messages) == [("user", "b"), ("assistant", "reply 2")]


def test_max_messages_below_minimum_is_rejected():
    with pytest.raises(ValueError):
        ConversationEngine(FakeLLM(), max_messages=1, timeout_seconds=10)


def test_session_continues_within_timeout():
    engine, _, clock = make(timeout=120)
    engine.respond("one")
    first_id = engine.session.session_id
    clock.advance(119)
    assert engine.is_active
    engine.respond("two")
    assert engine.session.session_id == first_id


def test_timeout_ends_session_and_next_turn_starts_fresh():
    engine, llm, clock = make(timeout=120)
    engine.respond("What is Python?")
    old_session = engine.session
    old_id = old_session.session_id

    clock.advance(121)
    assert engine.is_active is False
    assert engine.session is None
    assert old_session.state is SessionState.ENDED
    assert old_session.messages == []

    engine.respond("Who created it?")
    assert engine.session.session_id != old_id
    assert roles_and_text(llm.requests[1]) == [("system", SYSTEM_PROMPT), ("user", "Who created it?")]


def test_activity_extends_the_timeout():
    engine, _, clock = make(timeout=100)
    engine.respond("one")
    clock.advance(80)
    engine.respond("two")
    clock.advance(80)  # 160s since start but only 80s since last activity
    assert engine.is_active


def test_reset_clears_history_and_starts_new_session_next_turn():
    engine, llm, _ = make()
    engine.respond("What is Python?")
    old_id = engine.session.session_id

    engine.reset()
    assert engine.session is None

    engine.respond("Who created it?")
    assert engine.session.session_id != old_id
    assert roles_and_text(llm.requests[1]) == [("system", SYSTEM_PROMPT), ("user", "Who created it?")]


def test_reset_without_session_is_safe_and_repeatable():
    engine, _, _ = make()
    engine.reset()
    engine.reset()
    assert engine.session is None


def test_failed_llm_leaves_history_untouched_and_next_turn_works():
    engine, llm, _ = make()
    engine.respond("What is Python?")
    before = roles_and_text(engine.session.messages)
    session_id = engine.session.session_id

    llm.fail_next = True
    with pytest.raises(LLMProviderError):
        engine.respond("Who created it?")

    assert roles_and_text(engine.session.messages) == before
    assert engine.session.session_id == session_id  # session preserved

    engine.respond("Who created it?")
    assert roles_and_text(llm.requests[-1])[1:] == [
        ("user", "What is Python?"),
        ("assistant", "reply 1"),
        ("user", "Who created it?"),
    ]  # the failed attempt left no trace in the retry's context
    assert roles_and_text(engine.session.messages) == before + [
        ("user", "Who created it?"),
        ("assistant", "reply 3"),
    ]


def test_failure_on_first_turn_stores_no_messages():
    engine, llm, _ = make()
    llm.fail_next = True
    with pytest.raises(LLMProviderError):
        engine.respond("hello")
    assert engine.session is None


def test_independent_engines_have_independent_sessions():
    engine_a, llm_a, _ = make()
    engine_b, llm_b, _ = make()
    engine_a.respond("about A")
    engine_b.respond("about B")

    assert engine_a.session.session_id != engine_b.session.session_id
    assert [m.content for m in engine_a.session.messages if m.role is Role.USER] == ["about A"]
    assert [m.content for m in engine_b.session.messages if m.role is Role.USER] == ["about B"]
    assert all("about B" not in m.content for m in llm_a.requests[0])


def test_session_ids_are_unique_across_sessions():
    engine, _, _ = make()
    ids = set()
    for _ in range(5):
        engine.respond("hi")
        ids.add(engine.session.session_id)
        engine.reset()
    assert len(ids) == 5


def test_works_with_any_llm_provider_implementation():
    class MinimalProvider(LLMProvider):
        def chat(self, messages):
            return "ok"

    engine = ConversationEngine(MinimalProvider(), max_messages=10, timeout_seconds=60)
    assert engine.respond("hi") == "ok"


def test_legacy_generate_still_works_through_chat():
    llm = FakeLLM()
    assert llm.generate("hello", system="sys") == "reply 1"
    assert roles_and_text(llm.requests[0]) == [("system", "sys"), ("user", "hello")]
