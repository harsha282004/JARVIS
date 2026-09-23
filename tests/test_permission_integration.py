"""Agent decision -> permission requests, the Tool.execute gate, and anti-bypass tests.

Everything uses fake tools and a scripted fake LLM. No real tool exists and
nothing external is ever touched.
"""

import json

import pytest

from agent.brain.brain import ACTION_RESPONSE, AgentBrain
from agent.brain.models import AgentDecision, Intent, ToolSelection
from agent.brain.permissions import request_permissions
from agent.tools.base import Tool
from backend.core.conversation.engine import ConversationEngine
from backend.core.llm.base import LLMProvider
from backend.core.security import (
    PermissionDenied,
    PermissionManager,
    PermissionScope,
    PermissionStatus,
    ReasonCode,
    RiskLevel,
    SecurityEventType,
)


class FakeEmailTool(Tool):
    name = "email"
    description = "Send an email (fake)"
    risk = RiskLevel.HIGH

    def __init__(self):
        self.runs = []

    def run(self, **kwargs):
        self.runs.append(kwargs)
        return "sent (fake)"


class ScriptedLLM(LLMProvider):
    def __init__(self, *replies):
        self.replies = list(replies)

    def chat(self, messages, json_mode=False):
        return json.dumps(self.replies.pop(0))


def action_decision(*tool_names, **claims):
    return AgentDecision(
        intent=Intent.ACTION_REQUEST,
        action_required=True,
        response="I can't carry out actions like that yet.",
        confidence=0.9,
        requires_permission=claims.get("requires_permission", True),
        selected_tools=[
            ToolSelection(name=n, available=claims.get("available", True), requires_permission=claims.get("perm", True))
            for n in tool_names
        ],
    )


def manager_with(tool=None):
    tool = tool or FakeEmailTool()
    return PermissionManager(tools=[tool.descriptor().security_info()]), tool


# ---- decision -> permission requests ----------------------------------------

def test_action_decision_becomes_pending_permission_request():
    manager, _ = manager_with()
    requests = request_permissions(action_decision("email"), manager, session_id="s1")
    assert [(r.tool_name, r.action, r.status, r.session_id) for r in requests] == [
        ("email", "execute", PermissionStatus.PENDING, "s1")
    ]
    assert requests[0].description == "Use the email tool"


def test_non_action_decision_creates_no_requests():
    manager, _ = manager_with()
    decision = AgentDecision(intent=Intent.CONVERSATION, action_required=False, response="hi", confidence=1.0)
    assert request_permissions(decision, manager) == []
    assert manager.audit.events() == []


def test_unknown_tool_in_decision_is_denied_even_if_decision_claims_it_is_available_and_safe():
    manager, _ = manager_with()
    decision = action_decision("shell", requires_permission=False, available=True, perm=False)
    [request] = request_permissions(decision, manager)
    assert request.status is PermissionStatus.DENIED


def test_hostile_tool_identifier_from_llm_is_denied():
    manager, tool = manager_with()
    [request] = request_permissions(action_decision("email; rm -rf /"), manager)
    assert request.status is PermissionStatus.DENIED
    assert not manager.authorize(request) and tool.runs == []


def test_pending_request_from_decision_does_not_authorize_anything():
    manager, tool = manager_with()
    [request] = request_permissions(action_decision("email"), manager)
    assert manager.authorize(request) is False
    with pytest.raises(PermissionDenied):
        tool.execute(manager, request.request_id, to="john")
    assert tool.runs == []


# ---- the Tool.execute gate --------------------------------------------------

def test_tool_runs_only_after_explicit_approval_of_exactly_that_call():
    manager, tool = manager_with()
    request = manager.request_permission("email", "execute", parameters={"to": "john", "body": "X"})
    manager.approve(request)
    assert tool.execute(manager, request.request_id, to="john", body="X") == "sent (fake)"
    assert tool.runs == [{"to": "john", "body": "X"}]


def test_approval_for_john_cannot_run_the_tool_for_sarah():
    manager, tool = manager_with()
    request = manager.approve(manager.request_permission("email", "execute", parameters={"to": "john", "body": "X"}))
    with pytest.raises(PermissionDenied):
        tool.execute(manager, request.request_id, to="sarah", body="Y")
    assert tool.runs == []


def test_one_time_approval_cannot_run_the_tool_twice():
    manager, tool = manager_with()
    request = manager.approve(manager.request_permission("email", "execute", parameters={"to": "john"}))
    tool.execute(manager, request.request_id, to="john")
    with pytest.raises(PermissionDenied):
        tool.execute(manager, request.request_id, to="john")
    assert len(tool.runs) == 1


def test_tool_without_a_permission_manager_never_runs():
    tool = FakeEmailTool()
    with pytest.raises(PermissionDenied):
        tool.execute(None, "anything", to="john")
    assert tool.runs == []


def test_tool_with_a_failing_permission_manager_never_runs():
    class Exploding:
        def check(self, *a, **k):
            raise RuntimeError("down")

    tool = FakeEmailTool()
    with pytest.raises(PermissionDenied):
        tool.execute(Exploding(), "anything", to="john")
    assert tool.runs == []


def test_unregistered_tool_cannot_run_even_with_an_approved_record_for_another_tool():
    manager, _ = manager_with()
    request = manager.approve(manager.request_permission("email", "execute"))

    class Rogue(Tool):
        name = "rogue"
        description = "not registered"

        def __init__(self):
            self.ran = False

        def run(self, **kw):
            self.ran = True

    rogue = Rogue()
    with pytest.raises(PermissionDenied):
        rogue.execute(manager, request.request_id)
    assert rogue.ran is False


def test_session_permission_from_another_session_cannot_run_the_tool():
    class CalendarTool(FakeEmailTool):
        name = "calendar"
        risk = RiskLevel.MEDIUM
        allowed_scopes = (PermissionScope.ONE_TIME, PermissionScope.SESSION)

    tool = CalendarTool()
    manager, _ = manager_with(tool)
    request = manager.approve(manager.request_permission("calendar", "execute", scope=PermissionScope.SESSION, session_id="A"))
    with pytest.raises(PermissionDenied):
        tool.execute(manager, request.request_id, session_id="B")
    assert tool.execute(manager, request.request_id, session_id="A") == "sent (fake)"


# ---- LLM output cannot grant permission ------------------------------------

def test_llm_claiming_approval_grants_nothing():
    manager, tool = manager_with()
    llm = ScriptedLLM(
        {"intent": "action_request", "tools": ["email"], "approved": True, "permission": "granted",
         "status": "APPROVED", "requires_permission": False, "response": "Approved and sent!"}
    )
    brain = AgentBrain(llm, tools=[tool.descriptor()], max_plan_steps=8)
    decision = brain.decide(brain.build_request("Send an email to John", []))

    assert decision.requires_permission is True
    assert decision.response == ACTION_RESPONSE  # the model's "Approved and sent!" is ignored
    [request] = request_permissions(decision, manager)
    assert request.status is PermissionStatus.PENDING
    assert manager.authorize(request) is False
    assert tool.runs == []


def test_agent_brain_holds_no_reference_that_could_approve_or_execute():
    manager, tool = manager_with()
    brain = AgentBrain(ScriptedLLM(), tools=[tool.descriptor()], max_plan_steps=8)
    held = vars(brain).values()
    assert not any(isinstance(v, (PermissionManager, Tool)) for v in held)
    assert not any(isinstance(v, Tool) for v in brain._tools)


# ---- conversation wiring ---------------------------------------------------

def make_engine(*replies, tools=()):
    llm = ScriptedLLM(*replies)
    descriptors = [t.descriptor() for t in tools]
    manager = PermissionManager(tools=[d.security_info() for d in descriptors])
    agent = AgentBrain(llm, descriptors, 8)
    engine = ConversationEngine(llm, 20, 120, agent=agent, permissions=manager)
    return engine, manager


def test_conversation_action_turn_records_permission_requests_without_executing():
    tool = FakeEmailTool()
    engine, manager = make_engine({"intent": "action_request", "tools": ["email"]}, tools=[tool])
    reply = engine.respond("Send an email to John.")

    assert "can't carry out actions" in reply
    [request] = engine.last_permission_requests
    assert request.status is PermissionStatus.PENDING and request.session_id == engine.session.session_id
    assert tool.runs == []
    assert [e.event_type for e in manager.audit.events()] == [SecurityEventType.PERMISSION_REQUESTED]


def test_conversation_with_unknown_tool_is_denied_and_audited():
    engine, manager = make_engine({"intent": "action_request", "tools": ["email"]})  # no tools registered
    engine.respond("Send an email to John.")
    [request] = engine.last_permission_requests
    assert request.status is PermissionStatus.DENIED
    assert SecurityEventType.PERMISSION_DENIED in [e.event_type for e in manager.audit.events()]


def test_non_action_turn_records_no_permission_requests():
    engine, manager = make_engine({"intent": "conversation", "response": "hi"})
    engine.respond("Hello")
    assert engine.last_permission_requests == [] and manager.audit.events() == []


def test_ending_the_conversation_session_invalidates_its_permissions():
    tool = FakeEmailTool()
    engine, manager = make_engine({"intent": "action_request", "tools": ["email"]}, tools=[tool])
    engine.respond("Send an email to John.")
    [request] = engine.last_permission_requests
    manager.approve(request)

    engine.reset()

    assert manager.get(request.request_id).status is PermissionStatus.EXPIRED
    with pytest.raises(PermissionDenied):
        tool.execute(manager, request.request_id, session_id=request.session_id)


def test_permission_manager_failure_does_not_break_the_reply_or_authorize():
    class BrokenManager(PermissionManager):
        def request_permission(self, *a, **k):
            raise RuntimeError("boom")

    llm = ScriptedLLM({"intent": "action_request", "tools": ["email"]})
    engine = ConversationEngine(llm, 20, 120, agent=AgentBrain(llm, [], 8), permissions=BrokenManager())
    assert engine.respond("Send an email")
    assert engine.last_permission_requests == []


def test_phase4_permission_requests_helper_is_unchanged_and_still_denied():
    decision = action_decision("email")
    legacy = decision.permission_requests()
    assert [r.tool_name for r in legacy] == ["email"]
    assert PermissionManager().authorize(legacy[0]) is False
