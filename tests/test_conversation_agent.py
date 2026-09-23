"""ConversationEngine + AgentBrain integration, with a scripted fake LLM."""

import json

import pytest

from agent.brain.brain import ACTION_RESPONSE, AgentBrain
from agent.brain.models import Intent
from backend.core.conversation.engine import ConversationEngine
from backend.core.llm.base import LLMProvider, LLMProviderError
from backend.core.llm.messages import Role


class ScriptedLLM(LLMProvider):
    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls = []

    def chat(self, messages, json_mode=False):
        self.calls.append(list(messages))
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return json.dumps(reply)


def make(*replies):
    llm = ScriptedLLM(*replies)
    agent = AgentBrain(llm, tools=[], max_plan_steps=8)
    return ConversationEngine(llm, max_messages=20, timeout_seconds=120, agent=agent), llm


def texts(messages):
    return [(m.role.value, m.content) for m in messages]


def test_reply_is_the_agent_decision_response_and_decision_is_kept():
    engine, _ = make({"intent": "information_request", "response": "Paris."})
    assert engine.respond("Capital of France?") == "Paris."
    assert engine.last_decision.intent is Intent.INFORMATION_REQUEST


def test_history_is_owned_by_conversation_engine_and_forwarded_to_the_brain():
    engine, llm = make(
        {"intent": "information_request", "response": "Python is a language."},
        {"intent": "information_request", "response": "Decorators wrap functions."},
    )
    engine.respond("Find information about Python.")
    engine.respond("Now explain decorators.")

    second = llm.calls[1]
    assert second[0].role is Role.SYSTEM
    assert texts(second[1:]) == [
        ("user", "Find information about Python."),
        ("assistant", "Python is a language."),
        ("user", "Now explain decorators."),
    ]
    assert texts(engine.session.messages)[-1] == ("assistant", "Decorators wrap functions.")


def test_action_decision_is_spoken_honestly_and_recorded():
    engine, _ = make({"intent": "action_request", "tools": ["email"], "steps": ["Find John"]})
    reply = engine.respond("Send an email to John.")
    assert reply == ACTION_RESPONSE
    assert engine.last_decision.action_required and engine.last_decision.requires_permission
    assert texts(engine.session.messages)[-1] == ("assistant", ACTION_RESPONSE)


def test_clarification_reply_then_follow_up_sees_the_question():
    engine, llm = make(
        {"intent": "clarification_required", "response": "Who should I send it to?"},
        {"intent": "action_request", "tools": ["email"]},
    )
    engine.respond("Send it to him.")
    engine.respond("To John.")
    assert ("assistant", "Who should I send it to?") in texts(llm.calls[1])


def test_llm_failure_leaves_conversation_untouched_and_next_turn_works():
    engine, _ = make(
        {"intent": "conversation", "response": "Hi!"},
        LLMProviderError("Ollama down"),
        {"intent": "conversation", "response": "Still here."},
    )
    engine.respond("Hello")
    before = texts(engine.session.messages)

    with pytest.raises(LLMProviderError):
        engine.respond("Are you there?")
    assert texts(engine.session.messages) == before

    assert engine.respond("Are you there?") == "Still here."


def test_invalid_model_output_yields_safe_reply_without_crashing():
    engine, _ = make({"bad": 1}, {"bad": 2})
    reply = engine.respond("Do something risky")
    assert reply  # safe fallback text
    assert engine.last_decision.error is not None
    assert engine.last_decision.action_required is False


def test_without_an_agent_behavior_is_the_plain_llm_path():
    class PlainLLM(LLMProvider):
        def chat(self, messages, json_mode=False):
            assert json_mode is False
            return "plain reply"

    engine = ConversationEngine(PlainLLM(), max_messages=20, timeout_seconds=120)
    assert engine.respond("hi") == "plain reply"
    assert engine.last_decision is None
