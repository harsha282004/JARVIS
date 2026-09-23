"""Unit tests for AgentBrain with a scripted fake LLM.

They check classification handling, plans, tool selection, permission
flags, validation of malformed output, and that nothing is ever executed.
They do not prove a real model classifies well (see the integration test).
"""

import json
import pathlib
import re

import pytest

from agent.brain import brain as brain_module
from agent.brain.brain import ACTION_RESPONSE, FALLBACK_RESPONSE, UNSUPPORTED_RESPONSE, AgentBrain
from agent.brain.models import AgentErrorCode, Intent
from agent.planner.models import StepKind
from agent.tools.base import Tool, ToolDescriptor
from backend.core.llm.base import LLMProvider, LLMProviderError
from backend.core.llm.messages import Message, Role


class ScriptedLLM(LLMProvider):
    """Returns the scripted replies in order and records every call."""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls = []

    def chat(self, messages, json_mode=False):
        self.calls.append((list(messages), json_mode))
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply if isinstance(reply, str) else json.dumps(reply)


def out(intent, **fields):
    return {"intent": intent, **fields}


def make(*replies, tools=(), max_steps=8):
    llm = ScriptedLLM(*replies)
    return AgentBrain(llm, tools, max_steps), llm


def decide(brain, text="hello", context=()):
    return brain.decide(brain.build_request(text, context))


EMAIL_TOOL = ToolDescriptor(name="email", description="Send an email", requires_permission=True)
NOTES_TOOL = ToolDescriptor(name="notes", description="Read notes", requires_permission=False)


def test_conversation_intent_is_answered_directly():
    brain, _ = make(out("conversation", response="Hello! How can I help?", confidence=0.95, summary="Greeting."))
    d = decide(brain, "Hello JARVIS")
    assert d.intent is Intent.CONVERSATION and not d.action_required
    assert d.response == "Hello! How can I help?"
    assert d.plan is None and d.selected_tools == [] and d.error is None
    assert d.reasoning_summary == "Greeting."


def test_information_request_is_answered_without_action():
    brain, _ = make(out("information_request", response="Paris.", confidence=0.9))
    d = decide(brain, "What is the capital of France?")
    assert d.intent is Intent.INFORMATION_REQUEST and d.action_required is False
    assert d.response == "Paris."


def test_action_request_with_missing_tool_produces_plan_and_permission_flag():
    brain, _ = make(
        out("action_request", steps=["Identify the recipient", "Prepare the message"], tools=["email"],
            confidence=0.8, summary="User wants to send an email.")
    )
    d = decide(brain, "Send an email to John saying hello")
    assert d.intent is Intent.ACTION_REQUEST and d.action_required is True
    assert d.missing_tools == ["email"]
    assert d.requires_permission is True
    assert [s.kind for s in d.plan.steps] == [
        StepKind.PREPARE, StepKind.PREPARE, StepKind.PERMISSION, StepKind.EXECUTE,
    ]
    assert d.response == ACTION_RESPONSE  # never claims the action was done
    assert "sent" not in d.response.lower()


def test_action_response_ignores_llm_supplied_response_text():
    brain, _ = make(out("action_request", response="Done! Email sent.", tools=["email"]))
    d = decide(brain)
    assert d.response == ACTION_RESPONSE


def test_tool_selection_matches_registered_tool_case_insensitively():
    brain, _ = make(out("action_request", tools=["EMAIL"]), tools=[EMAIL_TOOL])
    d = decide(brain)
    assert [(t.name, t.available, t.requires_permission) for t in d.selected_tools] == [("email", True, True)]
    assert d.missing_tools == []


def test_registered_tool_without_permission_requirement_does_not_flag_permission():
    brain, _ = make(out("action_request", tools=["notes"]), tools=[NOTES_TOOL])
    d = decide(brain)
    assert d.requires_permission is False
    assert [s.kind for s in d.plan.steps] == [StepKind.PREPARE, StepKind.EXECUTE]


def test_action_with_no_tools_named_still_requires_permission():
    brain, _ = make(out("action_request"))
    assert decide(brain).requires_permission is True


def test_mixed_known_and_unknown_tools_require_permission():
    brain, _ = make(out("action_request", tools=["notes", "calendar"]), tools=[NOTES_TOOL])
    d = decide(brain)
    assert d.requires_permission is True and d.missing_tools == ["calendar"]


def test_clarification_returns_a_question():
    brain, _ = make(out("clarification_required", response="Who should I send it to?", confidence=0.7))
    d = decide(brain, "Send it to him.")
    assert d.intent is Intent.CLARIFICATION_REQUIRED and d.action_required is False
    assert d.response == "Who should I send it to?"
    assert d.plan is None and d.selected_tools == []


def test_unsupported_request_never_pretends_to_act():
    brain, _ = make(out("unsupported_request", response="Sure, opening Chrome now!", tools=["browser"]))
    d = decide(brain, "Open Chrome and play this YouTube video")
    assert d.intent is Intent.UNSUPPORTED_REQUEST
    assert d.response == UNSUPPORTED_RESPONSE
    assert d.selected_tools == [] and d.plan is None


def test_plan_length_is_capped_by_configuration():
    brain, _ = make(out("action_request", steps=[f"step {i}" for i in range(20)], tools=["email"]), max_steps=4)
    assert len(decide(brain).plan.steps) == 4


def test_llm_is_asked_for_json_with_tool_catalog_and_untouched_user_message():
    brain, llm = make(out("conversation", response="hi"), tools=[EMAIL_TOOL])
    decide(brain, "Hello JARVIS")
    messages, json_mode = llm.calls[0]
    assert json_mode is True
    assert messages[0].role is Role.SYSTEM
    assert "email: Send an email" in messages[0].content and "requires permission" in messages[0].content
    assert messages[-1] == Message(Role.USER, "Hello JARVIS", messages[-1].timestamp)


def test_empty_tool_catalog_is_stated_in_prompt():
    brain, llm = make(out("conversation", response="hi"))
    decide(brain)
    assert "none: no tools are available" in llm.calls[0][0][0].content


def test_conversation_context_is_forwarded_in_order():
    context = [Message(Role.USER, "Find information about Python."), Message(Role.ASSISTANT, "It is a language.")]
    brain, llm = make(out("information_request", response="Decorators wrap functions."))
    decide(brain, "Now explain decorators.", context)
    messages = llm.calls[0][0]
    assert [(m.role.value, m.content) for m in messages[1:]] == [
        ("user", "Find information about Python."),
        ("assistant", "It is a language."),
        ("user", "Now explain decorators."),
    ]


def test_brain_keeps_no_history_of_its_own():
    brain, llm = make(out("conversation", response="a"), out("conversation", response="b"))
    decide(brain, "first")
    decide(brain, "second")
    second_messages = llm.calls[1][0]
    assert all(m.content != "first" for m in second_messages)


def test_json_in_code_fence_and_with_prose_is_accepted():
    fenced = "```json\n" + json.dumps(out("conversation", response="hi")) + "\n```"
    prose = "Here you go: " + json.dumps(out("conversation", response="hi there"))
    brain, _ = make(fenced, prose)
    assert decide(brain).response == "hi"
    assert decide(brain).response == "hi there"


def test_intent_is_normalized_and_unknown_keys_ignored():
    brain, _ = make(out(" Information_Request ", response="ok", python="import os; os.system('x')"))
    assert decide(brain).intent is Intent.INFORMATION_REQUEST


def test_malformed_json_is_retried_once_then_succeeds():
    brain, llm = make("not json at all", out("conversation", response="Recovered."))
    d = decide(brain)
    assert d.response == "Recovered." and d.error is None
    assert len(llm.calls) == 2
    retry_messages = llm.calls[1][0]
    assert retry_messages[-2].role is Role.ASSISTANT and retry_messages[-1].role is Role.USER


def test_persistently_invalid_output_falls_back_safely_with_structured_error():
    brain, llm = make("garbage", '{"intent": "take_over_the_world"}')
    d = decide(brain, "Send an email to John")
    assert d.response == FALLBACK_RESPONSE
    assert d.intent is Intent.CONVERSATION and d.action_required is False
    assert d.plan is None and d.selected_tools == []
    assert d.error.code is AgentErrorCode.INVALID_OUTPUT
    assert "take_over_the_world" not in d.error.detail  # raw model output is not echoed
    assert len(llm.calls) == 2


@pytest.mark.parametrize(
    "bad",
    [
        "[]",
        '{"intent": "conversation", "response": ""}',  # direct intent needs a response
        '{"intent": "conversation", "response": "x", "confidence": 7}',
        '{"intent": "conversation", "response": "x", "steps": "not a list"}',
        '{"response": "no intent"}',
        "x" * 30_000,
    ],
)
def test_schema_violations_are_rejected(bad):
    brain, _ = make(bad, bad)
    d = decide(brain)
    assert d.error is not None and d.response == FALLBACK_RESPONSE


def test_llm_failure_propagates_and_nothing_is_fabricated():
    brain, _ = make(LLMProviderError("Ollama down"))
    with pytest.raises(LLMProviderError):
        decide(brain)


def test_llm_failure_on_retry_also_propagates():
    brain, _ = make("garbage", LLMProviderError("Ollama down"))
    with pytest.raises(LLMProviderError):
        decide(brain)


def test_brain_never_executes_tools():
    calls = []

    class RecordingTool(Tool):
        name = "email"
        description = "Send an email"

        def run(self, **kwargs):
            calls.append(kwargs)

    brain, _ = make(out("action_request", tools=["email"], steps=["x"]), tools=[RecordingTool().descriptor()])
    d = decide(brain, "Send an email to John")
    assert d.action_required and calls == []


def test_hostile_tool_names_and_code_are_only_inert_data():
    hostile = "email; rm -rf / && python -c 'import os'"
    brain, _ = make(out("action_request", tools=[hostile], steps=["__import__('os').system('calc')"]))
    d = decide(brain)
    assert d.missing_tools == [" ".join(hostile.split())[:64]]
    assert all(t.available is False for t in d.selected_tools)


def test_agent_package_contains_no_execution_or_network_primitives():
    forbidden = re.compile(
        r"\b(subprocess|os\.system|os\.popen|eval\(|exec\(|importlib|__import__|socket|httpx|requests|"
        r"pathlib|shutil|ctypes|\.run\()"
    )
    sources = list(pathlib.Path(brain_module.__file__).parent.glob("*.py"))
    sources += list(pathlib.Path(brain_module.__file__).parents[1].joinpath("planner").glob("*.py"))
    offenders = [
        f"{p.name}: {m.group(0)}"
        for p in sources
        for m in [forbidden.search(p.read_text(encoding="utf-8"))]
        if m
    ]
    assert offenders == []
