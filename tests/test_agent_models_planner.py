"""Unit tests for the Agent Brain models, Tool descriptors and the Planner."""

import pytest
from pydantic import ValidationError

from agent.brain.models import AgentDecision, AgentRequest, Intent, ToolSelection
from agent.planner.models import StepKind
from agent.planner.planner import Planner
from agent.tools.base import Tool, ToolDescriptor
from backend.core.llm.messages import Message, Role
from backend.core.security import PermissionManager


def test_agent_request_creation_with_context_and_tools():
    request = AgentRequest(
        user_text="Now explain decorators.",
        context=[Message(Role.USER, "Find information about Python."), Message(Role.ASSISTANT, "ok")],
        tools=[ToolDescriptor(name="email", description="Send email")],
    )
    assert request.user_text == "Now explain decorators."
    assert [m.role for m in request.context] == [Role.USER, Role.ASSISTANT]
    assert request.tools[0].requires_permission is True  # fail-safe default


def test_agent_request_rejects_blank_text():
    with pytest.raises(ValidationError):
        AgentRequest(user_text="")


def test_tool_descriptor_comes_from_tool_without_exposing_run():
    class DummyTool(Tool):
        name = "dummy"
        description = "does nothing"
        input_schema = {"type": "object"}

        def run(self, **kwargs):  # pragma: no cover - must never be called
            raise AssertionError("run must not be called")

    descriptor = DummyTool().descriptor()
    assert isinstance(descriptor, ToolDescriptor)
    assert (descriptor.name, descriptor.requires_permission) == ("dummy", True)
    assert not hasattr(descriptor, "run")


def test_valid_direct_decision():
    decision = AgentDecision(
        intent=Intent.INFORMATION_REQUEST, action_required=False, response="Paris.", confidence=0.9
    )
    assert decision.plan is None and decision.selected_tools == [] and decision.error is None


@pytest.mark.parametrize(
    "kwargs",
    [
        {"intent": Intent.ACTION_REQUEST, "action_required": False},  # action must be flagged
        {"intent": Intent.CONVERSATION, "action_required": True},  # only actions are action_required
        {"intent": Intent.CONVERSATION, "action_required": False, "requires_permission": True},
        {"intent": Intent.CONVERSATION, "action_required": False, "response": ""},
        {"intent": Intent.CONVERSATION, "action_required": False, "confidence": 1.5},
    ],
)
def test_invalid_decisions_are_rejected(kwargs):
    base = {"response": "hi", "confidence": 0.5}
    with pytest.raises(ValidationError):
        AgentDecision(**{**base, **kwargs})


def test_non_action_decision_cannot_carry_tools():
    with pytest.raises(ValidationError):
        AgentDecision(
            intent=Intent.CONVERSATION,
            action_required=False,
            response="hi",
            confidence=0.5,
            selected_tools=[ToolSelection(name="x", available=True, requires_permission=False)],
        )


def test_permission_requests_are_denied_by_the_existing_permission_manager():
    decision = AgentDecision(
        intent=Intent.ACTION_REQUEST,
        action_required=True,
        response="no",
        confidence=0.8,
        requires_permission=True,
        selected_tools=[
            ToolSelection(name="email", available=False, requires_permission=True),
            ToolSelection(name="notes", available=True, requires_permission=False),
        ],
    )
    requests = decision.permission_requests()
    assert [r.tool_name for r in requests] == ["email"]
    assert decision.missing_tools == ["email"]
    assert all(PermissionManager().authorize(r) is False for r in requests)


def test_planner_builds_ordered_plan_with_permission_step():
    plan = Planner(max_steps=8).build_plan(
        goal="Send an email to John",
        prepare_steps=["Identify the recipient", "Prepare the message"],
        tools=["email"],
        requires_permission=True,
    )
    assert [(s.order, s.kind.value, s.description, s.tool) for s in plan.steps] == [
        (1, "prepare", "Identify the recipient", None),
        (2, "prepare", "Prepare the message", None),
        (3, "permission", "Ask the user for permission before acting", None),
        (4, "execute", "Use the email tool", "email"),
    ]


def test_planner_is_deterministic():
    planner = Planner(max_steps=8)
    args = dict(goal="g", prepare_steps=["a", "b"], tools=["t"], requires_permission=True)
    assert planner.build_plan(**args) == planner.build_plan(**args)


def test_planner_without_permission_has_no_permission_step():
    plan = Planner(8).build_plan("g", ["a"], ["notes"], requires_permission=False)
    assert [s.kind for s in plan.steps] == [StepKind.PREPARE, StepKind.EXECUTE]


def test_planner_fallbacks_when_model_gives_no_steps_or_tools():
    plan = Planner(8).build_plan("g", [], [], requires_permission=True)
    assert [s.kind for s in plan.steps] == [StepKind.PREPARE, StepKind.PERMISSION, StepKind.EXECUTE]
    assert plan.steps[-1].tool is None


def test_planner_cleans_and_dedupes_prepare_steps():
    plan = Planner(8).build_plan("g", ["  do   x ", "do x", "", "y" * 500], [], False)
    prepare = [s.description for s in plan.steps if s.kind is StepKind.PREPARE]
    assert prepare[0] == "do x" and len(prepare) == 2 and len(prepare[1]) == 200


def test_planner_respects_max_steps_and_keeps_essential_steps():
    plan = Planner(max_steps=3).build_plan("g", ["a", "b", "c", "d"], ["email"], True)
    assert len(plan.steps) == 3
    assert [s.kind for s in plan.steps] == [StepKind.PREPARE, StepKind.PERMISSION, StepKind.EXECUTE]
    assert [s.order for s in plan.steps] == [1, 2, 3]


def test_planner_max_steps_smaller_than_essential_steps():
    plan = Planner(max_steps=1).build_plan("g", ["a"], ["email"], True)
    assert [s.kind for s in plan.steps] == [StepKind.PERMISSION]


def test_planner_rejects_zero_max_steps():
    with pytest.raises(ValueError):
        Planner(0)
