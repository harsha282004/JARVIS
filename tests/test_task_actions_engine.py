"""Task/reminder actions end to end: AgentBrain -> validated action -> PermissionManager -> tool -> service -> database.

The LLM is scripted (no Ollama). The database is an isolated SQLite one, never your PostgreSQL.
"""

import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from agent.brain.brain import ACTION_RESPONSE, FALLBACK_RESPONSE, AgentBrain
from agent.brain.models import Intent
from agent.brain.prompts import build_system_prompt
from agent.tasks.executor import (
    DECLINED_REPLY,
    DENIED_REPLY,
    STORAGE_REPLY,
    TaskActionExecutor,
    classify_confirmation,
)
from agent.tasks.intents import InvalidTaskAction, TaskActionName, parse_task_action
from agent.tasks.models import Frequency, Recurrence, ReminderStatus, TaskPriority, TaskStatus, TaskStorageError
from agent.tasks.tools import TaskToolContext, build_task_tools
from backend.core.conversation.engine import ConversationEngine
from backend.core.llm.base import LLMProvider
from backend.core.security import PermissionDenied, PermissionManager, PermissionScope, PermissionStatus, SecurityEventType
from tests.task_helpers import IST, Clock, ist, make_parser, make_services

NOW = datetime(2030, 3, 4, 9, 0, tzinfo=timezone.utc)  # Monday 14:30 IST
ROOT = Path(__file__).resolve().parents[1]


class ScriptedLLM(LLMProvider):
    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls = []

    def chat(self, messages, json_mode=False):
        self.calls.append(list(messages))
        reply = self.replies.pop(0)
        return reply if isinstance(reply, str) else json.dumps(reply)


def act(name, summary="task request", **arguments):
    return {"intent": "action_request", "tools": [name], "summary": summary,
            "action": {"name": name, "arguments": arguments}}


class Stack:
    def __init__(self, session_factory, *replies, tasks_enabled=True, reminders_enabled=True):
        self.tasks, self.reminders, self.repo, self.clock = make_services(session_factory)
        self.tools = build_task_tools(TaskToolContext(
            self.tasks if tasks_enabled else None, self.reminders if reminders_enabled else None,
            make_parser(), self.clock))
        self.descriptors = [t.descriptor() for t in self.tools]
        self.permissions = PermissionManager(tools=[d.security_info() for d in self.descriptors], clock=self.clock)
        self.llm = ScriptedLLM(*replies)
        self.agent = AgentBrain(self.llm, tools=self.descriptors, max_plan_steps=8)
        self.executor = TaskActionExecutor(self.tools, self.permissions, clock=self.clock)
        self.engine = ConversationEngine(
            self.llm, max_messages=20, timeout_seconds=120, agent=self.agent, permissions=self.permissions,
            actions=self.executor, clock=self.clock)

    def say(self, text):
        return self.engine.respond(text)

    def tool(self, name):
        return next(t for t in self.tools if t.name == name)


@pytest.fixture
def stack(session_factory):
    def make(*replies, **kw):
        return Stack(session_factory, *replies, **kw)

    return make


# ---- AgentBrain: structured intents -------------------------------------------------------------------


def test_reminder_request_becomes_a_structured_action_request(stack):
    s = stack(act("create_reminder", message="submit my report", when="tomorrow at 9 AM"))
    s.say("Remind me tomorrow at 9 AM to submit my report.")
    decision = s.engine.last_decision
    assert decision.intent is Intent.ACTION_REQUEST and decision.action_required
    assert decision.task_action.name is TaskActionName.CREATE_REMINDER
    assert decision.task_action.arguments.message == "submit my report"
    assert decision.task_action.arguments.when == "tomorrow at 9 AM"  # kept as the user's words, not computed by the model
    assert [t.name for t in decision.selected_tools] == ["create_reminder"] and decision.selected_tools[0].available


def test_task_request_becomes_a_structured_action_request(stack):
    s = stack(act("create_task", title="finish my report"))
    s.say("Create a task to finish my report.")
    assert s.engine.last_decision.task_action.name is TaskActionName.CREATE_TASK
    assert s.engine.last_decision.task_action.arguments.title == "finish my report"


def test_the_brain_only_decides_it_never_touches_the_database(stack):
    s = stack(act("create_task", title="finish my report"))
    request = s.agent.build_request("Create a task to finish my report.", [])
    decision = s.agent.decide(request)
    assert decision.task_action is not None
    assert s.tasks.list_tasks() == []  # nothing was created by deciding
    brain_source = (ROOT / "agent" / "brain" / "brain.py").read_text(encoding="utf-8")
    assert not re.search(r"^\s*(from|import)\s+agent\.tasks\.(service|repository|tools|executor)", brain_source, re.M)


@pytest.mark.parametrize(
    "bad_action",
    [
        {"name": "drop_all_tables", "arguments": {}},               # not a known action
        {"name": "create_reminder", "arguments": {}},               # missing the required message
        {"name": "create_reminder", "arguments": "message: x"},     # arguments not an object
        {"name": "complete_task", "arguments": {"task_id": "42"}},  # an id is not accepted: a description is required
        "create_reminder",                                          # action is not an object
        {"arguments": {"message": "x"}},                            # no name
        {"name": "create_task", "arguments": {"title": "x" * 500}}, # over-long
    ],
)
def test_invalid_structured_output_never_reaches_the_executor(stack, bad_action):
    reply = {"intent": "action_request", "tools": ["create_reminder"], "summary": "s", "action": bad_action}
    s = stack(reply, reply)  # the brain retries once, then falls back safely
    assert s.say("Remind me to do something") == FALLBACK_RESPONSE
    assert s.engine.last_decision.error is not None and s.engine.last_decision.task_action is None
    assert s.tasks.list_tasks() == [] and s.reminders.list_reminders() == []
    assert len(s.llm.calls) == 2


def test_the_brain_recovers_when_the_retry_is_valid(stack):
    bad = act("create_reminder")
    bad["action"]["name"] = "made_up_tool"
    s = stack(bad, act("create_reminder", message="call Mom", when="in 30 minutes"))
    assert s.say("Remind me in 30 minutes to call Mom").startswith("Okay, I'll remind you today at 3:00 PM")


def test_an_action_for_a_disabled_subsystem_is_dropped(stack):
    s = stack(act("create_task", title="x"), tasks_enabled=False)  # only reminder tools are available
    assert s.say("Create a task") == ACTION_RESPONSE  # the Phase 4 behavior: declined, nothing executed
    assert s.engine.last_decision.task_action is None


def test_without_an_executor_actions_are_still_declined(session_factory):
    s = Stack(session_factory, act("create_task", title="x"))
    engine = ConversationEngine(s.llm, max_messages=20, timeout_seconds=120, agent=s.agent, permissions=s.permissions)
    assert engine.respond("Create a task to x") == ACTION_RESPONSE
    assert s.tasks.list_tasks() == []


def test_prompt_lists_task_tools_only_when_they_exist():
    assert "Task and reminder actions" not in build_system_prompt([])
    from agent.tasks.tools import CreateReminderTool

    descriptor = CreateReminderTool(TaskToolContext(None, None, make_parser(), Clock())).descriptor()
    prompt = build_system_prompt([descriptor])
    assert "Task and reminder actions" in prompt and "create_reminder" in prompt
    assert "Never convert them to dates" in prompt and "Never invent ids" in prompt


# ---- creating things ----------------------------------------------------------------------------------


def test_remind_me_tomorrow_at_9am(stack):
    s = stack(act("create_reminder", message="submit my assignment", when="tomorrow at 9 AM"))
    reply = s.say("Remind me to submit my assignment tomorrow at 9 AM.")
    assert reply == "Okay, I'll remind you tomorrow at 9:00 AM: submit my assignment."
    [r] = s.reminders.list_reminders()
    assert r.scheduled_at == ist(2030, 3, 5, 9).astimezone(timezone.utc) and r.timezone == "Asia/Kolkata"
    assert r.source == "conversation" and r.session_id == s.engine.session.session_id


def test_remind_me_in_30_minutes(stack):
    s = stack(act("create_reminder", message="call Mom", when="in 30 minutes"))
    assert s.say("Remind me in 30 minutes to call Mom.") == "Okay, I'll remind you today at 3:00 PM: call Mom."
    assert s.reminders.list_reminders()[0].scheduled_at == NOW + timedelta(minutes=30)


def test_every_monday_at_8am_creates_one_recurring_reminder(stack):
    s = stack(act("create_reminder", message="review my weekly goals", recurrence="every Monday at 8 AM"))
    reply = s.say("Remind me every Monday at 8 AM to review my weekly goals.")
    assert reply == "Okay, I'll remind you every Monday at 8:00 AM: review my weekly goals."
    [r] = s.reminders.list_reminders()
    assert r.recurrence.frequency is Frequency.WEEKLY and r.recurrence.weekdays == (0,) and r.recurrence.hour == 8
    assert r.scheduled_at == ist(2030, 3, 11, 8).astimezone(timezone.utc)


def test_a_recurrence_the_model_put_in_when_is_still_recognised(stack):
    s = stack(act("create_reminder", message="stand up", when="every day at 8 AM"))
    assert "every day at 8:00 AM" in s.say("Remind me every day at 8 AM to stand up")
    assert s.reminders.list_reminders()[0].recurrence.frequency is Frequency.DAILY


def test_create_a_task(stack):
    s = stack(act("create_task", title="finish the JARVIS documentation", priority="high", due="friday at 5 pm"))
    reply = s.say("Create a high priority task to finish the JARVIS documentation by Friday 5 PM.")
    assert reply == "Okay, I've added the task: finish the JARVIS documentation, due Friday at 5:00 PM."
    [t] = s.tasks.list_tasks()
    assert t.priority is TaskPriority.HIGH and t.due_at == ist(2030, 3, 8, 17).astimezone(timezone.utc)
    assert t.source == "conversation" and t.session_id == s.engine.session.session_id


def test_a_task_with_a_reminder_is_created_atomically(stack):
    s = stack(act("create_task", title="submit report", due="tomorrow at 9 AM", remind=True))
    assert s.say("Create a task to submit my report tomorrow at 9 and remind me").endswith("I'll remind you then.")
    [t] = s.tasks.list_tasks()
    [r] = s.reminders.list_reminders(task_id=t.task_id)
    assert r.scheduled_at == t.due_at


def test_a_task_without_a_due_date_is_fine(stack):
    s = stack(act("create_task", title="tidy my desk", priority="not-a-priority"))
    assert s.say("Add a task to tidy my desk") == "Okay, I've added the task: tidy my desk."
    assert s.tasks.list_tasks()[0].priority is TaskPriority.MEDIUM  # an unknown priority word is ignored


@pytest.mark.parametrize(
    "arguments, question",
    [
        ({"message": "call Mom"}, "When should I remind you"),
        ({"message": "call Mom", "when": "whenever"}, "couldn't understand that time"),
        ({"message": "call Mom", "when": "tomorrow at 9"}, "AM or 9 PM"),
        ({"message": "call Mom", "when": "tomorrow"}, "What time tomorrow"),
        ({"message": "call Mom", "when": "today at 9 am"}, "already passed"),
        ({"message": "call Mom", "recurrence": "every Monday"}, "What time should it repeat"),
        ({"message": "call Mom", "recurrence": "sometimes"}, "couldn't understand how often"),
    ],
)
def test_unclear_times_are_asked_about_and_nothing_is_created(stack, arguments, question):
    s = stack(act("create_reminder", **arguments))
    assert question in s.say("Remind me to call Mom")
    assert s.reminders.list_reminders() == []


def test_task_reminder_needs_a_real_time(stack):
    s = stack(act("create_task", title="x", remind=True))
    assert "What time should I remind you" in s.say("Create a task x and remind me")
    assert s.tasks.list_tasks() == []


# ---- querying ----------------------------------------------------------------------------------------------


def seed(s):
    s.tasks.create_task("Submit assignment", due_at=ist(2030, 3, 3, 10))       # overdue
    s.tasks.create_task("Review notes", due_at=ist(2030, 3, 4, 20))            # today
    s.tasks.create_task("Plan trip", due_at=ist(2030, 3, 6, 10))               # later this week
    s.tasks.create_task("Read book")                                          # no date
    done = s.tasks.create_task("Old chore")
    s.tasks.complete_task(done.task_id)


def test_what_tasks_do_i_have_today(stack):
    s = stack(act("list_tasks", scope="today"))
    seed(s)
    reply = s.say("What tasks do I have today?")
    assert "1 overdue: Submit assignment, due yesterday at 10:00 AM" in reply
    assert "1 due today: Review notes, due today at 8:00 PM" in reply
    assert "Plan trip" not in reply and "Old chore" not in reply


@pytest.mark.parametrize(
    "scope, present, absent",
    [
        ("overdue", ["Submit assignment"], ["Review notes", "Read book"]),
        ("incomplete", ["Submit assignment", "Review notes", "Plan trip", "Read book"], ["Old chore"]),
        ("upcoming", ["Review notes", "Plan trip"], ["Read book", "Submit assignment"]),
    ],
)
def test_task_queries_use_status_and_time_filters(stack, scope, present, absent):
    s = stack(act("list_tasks", scope=scope))
    seed(s)
    reply = s.say("show my tasks")
    assert all(p in reply for p in present) and not any(a in reply for a in absent)


def test_empty_task_list_is_reported_honestly(stack):
    s = stack(act("list_tasks", scope="today"))
    assert s.say("What do I have to do today?") == "You have no tasks due today."


def test_reminder_queries(stack):
    s = stack(act("list_reminders", scope="next"), act("list_reminders", scope="tomorrow"),
              act("list_reminders", scope="today"), act("list_reminders", scope="upcoming"))
    s.reminders.create_reminder("later today", ist(2030, 3, 4, 20))
    s.reminders.create_reminder("tomorrow morning", ist(2030, 3, 5, 9))
    s.reminders.create_reminder("weekly goals", recurrence=Recurrence(
        frequency=Frequency.WEEKLY, hour=8, weekdays=(0,)))
    assert s.say("What's my next reminder?") == "Your next reminder is later today (today at 8:00 PM)."
    assert s.say("What reminders do I have tomorrow?") == "You have 1 reminders tomorrow: tomorrow morning (tomorrow at 9:00 AM)."
    assert "later today" in s.say("reminders today?")
    upcoming = s.say("all my reminders")
    assert "weekly goals (every Monday at 8:00 AM)" in upcoming


# ---- completing --------------------------------------------------------------------------------------------


def test_mark_a_task_completed(stack):
    s = stack(act("complete_task", query="JARVIS documentation"))
    task = s.tasks.create_task("Finish the JARVIS documentation")
    s.tasks.create_task("Buy milk")
    assert s.say("Mark my JARVIS documentation task as completed.") == "Done. I've marked the task completed: Finish the JARVIS documentation."
    done = s.tasks.get_task(task.task_id)
    assert done.status is TaskStatus.COMPLETED and done.completed_at == s.clock()


def test_ambiguous_completion_asks_and_changes_nothing(stack):
    s = stack(act("complete_task", query="report"))
    s.tasks.create_task("Write report for physics")
    s.tasks.create_task("Write report for chemistry")
    reply = s.say("Mark my report task as completed")
    assert reply.startswith("I found 2 tasks that could match:") and "physics" in reply and "chemistry" in reply
    assert reply.endswith("Which one do you mean?")
    assert {t.status for t in s.tasks.list_tasks()} == {TaskStatus.PENDING}


def test_unknown_task_is_reported_not_guessed(stack):
    s = stack(act("complete_task", query="quantum computing"))
    s.tasks.create_task("Write report")
    assert s.say("Complete my quantum computing task") == "I couldn't find an open task matching that."
    assert s.tasks.list_tasks()[0].status is TaskStatus.PENDING


def test_a_model_supplied_id_is_ignored(stack):
    s = stack(act("complete_task", query="report", task_id="hallucinated-id-123"))
    task = s.tasks.create_task("Write report")
    assert "Done." in s.say("Complete my report")
    assert s.tasks.get_task(task.task_id).status is TaskStatus.COMPLETED


# ---- cancelling: needs the user's confirmation ---------------------------------------------------------------


def test_cancelling_a_reminder_asks_first_then_cancels_on_yes(stack):
    s = stack(act("cancel_reminder", query="assignment", when="9 AM"))
    r = s.reminders.create_reminder("submit my assignment", ist(2030, 3, 5, 9))
    s.reminders.create_reminder("submit my assignment (evening copy)", ist(2030, 3, 5, 18))
    asked = s.say("Cancel my 9 AM assignment reminder.")
    assert asked == "Do you want me to cancel the reminder: submit my assignment (tomorrow at 9:00 AM)? Say yes to confirm."
    assert s.reminders.get_reminder(r.reminder_id).status is ReminderStatus.SCHEDULED  # nothing changed yet
    assert s.engine.last_permission_requests[0].status is PermissionStatus.PENDING

    assert s.say("Yes") == "Okay, I've cancelled that reminder."
    assert s.reminders.get_reminder(r.reminder_id).status is ReminderStatus.CANCELLED
    assert len(s.llm.calls) == 1  # the confirmation was read by code, not sent to the model
    assert s.engine.last_decision is None


def test_saying_no_keeps_the_reminder(stack):
    s = stack(act("cancel_reminder", query="assignment"))
    r = s.reminders.create_reminder("submit my assignment", ist(2030, 3, 5, 9))
    s.say("Cancel my assignment reminder")
    assert s.say("No") == DECLINED_REPLY
    assert s.reminders.get_reminder(r.reminder_id).status is ReminderStatus.SCHEDULED


def test_the_confirmation_is_one_shot(stack):
    s = stack(act("cancel_reminder", query="assignment"), {"intent": "conversation", "response": "Hello!"},
              {"intent": "conversation", "response": "Yes to what?"})
    r = s.reminders.create_reminder("submit my assignment", ist(2030, 3, 5, 9))
    s.say("Cancel my assignment reminder")
    assert s.say("What's the weather like?") == "Hello!"  # not an answer: handled as a new request, pending dropped
    assert s.say("yes") == "Yes to what?"  # the old question is gone; a late "yes" cancels nothing
    assert s.reminders.get_reminder(r.reminder_id).status is ReminderStatus.SCHEDULED


def test_an_unanswered_confirmation_expires(stack):
    s = stack(act("cancel_reminder", query="assignment"), {"intent": "conversation", "response": "Sure."})
    r = s.reminders.create_reminder("submit my assignment", ist(2030, 3, 5, 9))
    s.say("Cancel my assignment reminder")
    s.clock.advance(seconds=61)
    assert s.say("yes") == "Sure."  # too late: an ordinary message now
    assert s.reminders.get_reminder(r.reminder_id).status is ReminderStatus.SCHEDULED


def test_ambiguous_cancellation_asks_which_one_without_a_confirmation(stack):
    s = stack(act("cancel_reminder", query="assignment"), {"intent": "conversation", "response": "OK."})
    a = s.reminders.create_reminder("submit assignment one", ist(2030, 3, 5, 9))
    b = s.reminders.create_reminder("submit assignment two", ist(2030, 3, 5, 10))
    reply = s.say("Cancel my assignment reminder")
    assert reply.startswith("I found 2 reminders that could match:")
    assert s.say("yes") == "OK."  # there was nothing pending to confirm
    assert {s.reminders.get_reminder(x.reminder_id).status for x in (a, b)} == {ReminderStatus.SCHEDULED}


def test_cancelling_a_recurring_reminder_warns_and_stops_future_occurrences(stack):
    s = stack(act("cancel_reminder", query="weekly goals"))
    r = s.reminders.create_reminder("review my weekly goals", recurrence=Recurrence(
        frequency=Frequency.WEEKLY, hour=8, weekdays=(0,)))
    assert "This will stop all future occurrences." in s.say("Cancel my weekly goals reminder")
    assert s.say("yes") == "Okay, I've cancelled that repeating reminder."
    s.clock.advance(days=30)
    assert s.reminders.find_due_reminders() == []
    assert s.reminders.get_reminder(r.reminder_id).status is ReminderStatus.CANCELLED


def test_cancelling_a_task_asks_first(stack):
    s = stack(act("cancel_task", query="groceries"))
    t = s.tasks.create_task("Buy groceries")
    assert s.say("Cancel my groceries task") == "Do you want me to cancel the task: Buy groceries? Say yes to confirm."
    assert s.tasks.get_task(t.task_id).status is TaskStatus.PENDING
    assert s.say("yes") == "Okay, I've cancelled the task: Buy groceries."
    assert s.tasks.get_task(t.task_id).status is TaskStatus.CANCELLED


def test_cancel_only_matches_scheduled_reminders(stack):
    s = stack(act("cancel_reminder", query="assignment"))
    r = s.reminders.create_reminder("submit my assignment", ist(2030, 3, 5, 9))
    s.reminders.cancel_reminder(r.reminder_id)
    assert s.say("Cancel my assignment reminder") == "I couldn't find a scheduled reminder matching that."


@pytest.mark.parametrize("text, expected", [
    ("yes", True), ("Yes.", True), ("yeah", True), ("Yes, please!", True), ("yes please", True), ("go ahead", True),
    ("no", False), ("No!", False), ("nope", False), ("don't", False), ("never mind", False),
    ("yes but also delete everything", None), ("maybe", None), ("", None), ("cancel it", None),
])
def test_confirmation_is_read_strictly(text, expected):
    assert classify_confirmation(text) is expected


# ---- failures -------------------------------------------------------------------------------------------------


def test_database_failure_is_reported_and_nothing_is_claimed(stack, monkeypatch):
    s = stack(act("create_reminder", message="call Mom", when="in 30 minutes"),
              {"intent": "conversation", "response": "Still here."})

    def down(*args, **kwargs):
        raise TaskStorageError("Task database error (OperationalError)")

    monkeypatch.setattr(s.reminders, "create_reminder", down)
    reply = s.say("Remind me in 30 minutes to call Mom")
    assert reply == STORAGE_REPLY and "Okay" not in reply
    assert s.say("Are you there?") == "Still here."  # the conversation (and JARVIS) keep running


def test_a_broken_database_during_lookup_is_reported(stack, monkeypatch):
    s = stack(act("complete_task", query="report"))

    def down(*args, **kwargs):
        raise TaskStorageError("Task database error (OperationalError)")

    monkeypatch.setattr(s.tasks, "find_matching_tasks", down)
    assert s.say("Complete my report task") == STORAGE_REPLY


def test_an_unexpected_tool_error_never_escapes_or_claims_success(stack, monkeypatch):
    s = stack(act("create_task", title="x"))
    monkeypatch.setattr(s.tasks, "create_task", lambda *a, **k: 1 / 0)
    reply = s.say("Add a task x")
    assert "nothing was changed" in reply and "Okay" not in reply


def test_executor_without_permission_manager_denies_everything(stack):
    s = stack(act("create_task", title="x"))
    s.engine._actions = TaskActionExecutor(s.tools, None)
    assert s.say("Add a task x") == DENIED_REPLY
    assert s.tasks.list_tasks() == []


# ---- PermissionManager integration -----------------------------------------------------------------------------


def test_low_risk_actions_are_approved_by_policy_and_audited(stack):
    s = stack(act("create_task", title="x"))
    s.say("Add a task x")
    [request] = s.engine.last_permission_requests
    assert request.tool_name == "create_task" and request.status is PermissionStatus.APPROVED
    events = [(e.event_type, e.code.value) for e in s.permissions.audit.events() if e.tool_name == "create_task"]
    assert (SecurityEventType.PERMISSION_APPROVED, "auto_policy") in events
    assert any(t is SecurityEventType.AUTHORIZATION_ALLOWED for t, _ in events)


def test_documented_permission_policy(stack):
    s = stack()
    policy = {d.name: (d.requires_permission, d.risk.name) for d in s.descriptors}
    assert policy == {
        "create_task": (False, "LOW"), "list_tasks": (False, "LOW"), "complete_task": (False, "LOW"),
        "cancel_task": (True, "MEDIUM"),
        "create_reminder": (False, "LOW"), "list_reminders": (False, "LOW"), "cancel_reminder": (True, "MEDIUM"),
    }
    assert all(d.allowed_scopes == [PermissionScope.ONE_TIME] for d in s.descriptors)


def test_a_tool_cannot_run_without_authorization(stack):
    s = stack()
    r = s.reminders.create_reminder("x", ist(2030, 3, 5, 9))
    tool = s.tool("cancel_reminder")
    with pytest.raises(PermissionDenied):
        tool.execute(None, "any", reminder_id=r.reminder_id)
    with pytest.raises(PermissionDenied):
        tool.execute(s.permissions, "no-such-request", reminder_id=r.reminder_id)
    pending = s.permissions.request_permission("cancel_reminder", "execute", parameters={"reminder_id": r.reminder_id})
    assert pending.status is PermissionStatus.PENDING  # cancelling needs a human
    with pytest.raises(PermissionDenied):
        tool.execute(s.permissions, pending.request_id, reminder_id=r.reminder_id)
    assert s.reminders.get_reminder(r.reminder_id).status is ReminderStatus.SCHEDULED


def test_an_approval_is_bound_to_the_exact_parameters_and_used_once(stack):
    s = stack()
    a = s.reminders.create_reminder("a", ist(2030, 3, 5, 9))
    b = s.reminders.create_reminder("b", ist(2030, 3, 5, 10))
    tool = s.tool("cancel_reminder")
    request = s.permissions.request_permission("cancel_reminder", "execute", parameters={"reminder_id": a.reminder_id})
    approved = s.permissions.approve(request, actor="user")
    with pytest.raises(PermissionDenied):  # approved for `a`, not for `b`
        tool.execute(s.permissions, approved.request_id, reminder_id=b.reminder_id)
    request2 = s.permissions.request_permission("cancel_reminder", "execute", parameters={"reminder_id": a.reminder_id})
    approved2 = s.permissions.approve(request2, actor="user")
    tool.execute(s.permissions, approved2.request_id, reminder_id=a.reminder_id)
    with pytest.raises(PermissionDenied):  # one-time: cannot be replayed
        tool.execute(s.permissions, approved2.request_id, reminder_id=a.reminder_id)
    assert s.reminders.get_reminder(b.reminder_id).status is ReminderStatus.SCHEDULED


def test_unknown_task_tools_are_denied(stack):
    s = stack()
    for name in ("delete_task", "run_sql", "execute", "drop_tasks"):
        assert s.permissions.request_permission(name, "execute").status is PermissionStatus.DENIED


def test_an_action_request_for_another_tool_stays_denied(stack):
    s = stack({"intent": "action_request", "tools": ["email"], "summary": "send an email"})
    assert s.say("Send an email to Bob") == ACTION_RESPONSE
    [request] = s.engine.last_permission_requests
    assert request.tool_name == "email" and request.status is PermissionStatus.DENIED


# ---- no arbitrary SQL / code -------------------------------------------------------------------------------------


def test_hostile_text_is_only_ever_stored_as_text(stack, session_factory):
    payload = "x'); DROP TABLE tasks; --"
    s = stack(act("create_task", title=payload, notes="__import__('os').system('calc')", sql="DROP TABLE reminders"))
    assert payload in s.say("Add a task")
    assert s.tasks.list_tasks()[0].title == payload
    assert s.reminders.list_reminders() == []  # both tables still exist and work
    assert "sql" not in {f for f in s.tool("create_task").input_schema}


def test_the_task_package_has_no_dynamic_execution_or_raw_sql():
    forbidden = re.compile(
        r"\b(eval|exec|__import__|os\.system|subprocess|popen|pickle|importlib)\b|(?<!re\.)\bcompile\(|\btext\(|\.execute\(\s*[\"']"
    )
    offenders = []
    for path in (ROOT / "agent" / "tasks").glob("*.py"):
        source = path.read_text(encoding="utf-8")
        code = "\n".join(line for line in source.splitlines() if not line.lstrip().startswith(("#", '"', "'")))
        if path.name == "executor.py":  # the only match: Tool.execute() is the permission gate, not code execution
            code = code.replace("tool.execute(", "")
        for match in forbidden.finditer(code):
            offenders.append((path.name, match.group(0)))
    assert offenders == []


def test_task_tools_do_not_import_the_database_layer():
    for name in ("tools.py", "executor.py", "intents.py"):
        source = (ROOT / "agent" / "tasks" / name).read_text(encoding="utf-8")
        assert "sqlalchemy" not in source.lower() and "backend.models" not in source and "SessionLocal" not in source


def test_no_tool_or_argument_can_name_a_table_id_or_sql():
    from agent.tasks.intents import ARGUMENT_MODELS

    for model in ARGUMENT_MODELS.values():
        assert not {"id", "task_id", "reminder_id", "sql", "query_sql"} & (set(model.model_fields) - {"query"})


def test_parse_task_action_rejects_junk_without_echoing_it():
    for raw in (None, [], {"name": 5}, {"name": "create_task", "arguments": {"title": ""}}):
        with pytest.raises(InvalidTaskAction):
            parse_task_action(raw)
    with pytest.raises(InvalidTaskAction) as exc:
        parse_task_action({"name": "create_task", "arguments": {"title": "s3cret" * 100}})
    assert "s3cret" not in str(exc.value)


def test_logs_do_not_contain_task_content(stack, caplog):
    import logging

    s = stack(act("create_task", title="my-private-task-title", due="tomorrow at 9 AM", remind=True),
              act("cancel_task", query="private"))
    with caplog.at_level(logging.DEBUG):
        s.say("Add task my-private-task-title")
        s.say("Cancel my private task")
        s.say("yes")
    assert "my-private-task-title" not in caplog.text
