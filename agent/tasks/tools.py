"""Task/reminder tools: the only code that turns a validated action into a service call.

Each tool has two parts:
  resolve(args) -> Clarify | Ready   read-only: parse times, identify the target by matching words
                                     (never a model-supplied id) and produce concrete, JSON-typed parameters.
  run(**params) -> str               the mutation, reached only through Tool.execute, i.e. after the
                                     PermissionManager authorized exactly these parameters.

Tools call TaskService/ReminderService only: no SQL, no code execution, no files, no network.
Permission policy (also in docs/tasks-and-reminders.md):
  create_task, create_reminder, list_*, complete_task: LOW risk, no approval. They only add to or read the user's
      own local data, or make a change the user can undo (a completed task can be reopened), and the target is
      resolved by code and must be unique.
  cancel_task, cancel_reminder: MEDIUM risk, approval required. Cancelling cannot be undone (and cancelling a
      recurring reminder ends every future occurrence), so the user confirms by voice before anything changes.
"""

import re
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from typing import Any

from pydantic import BaseModel

from agent.tasks.formatting import format_recurrence, format_when
from agent.tasks.intents import (
    CancelReminderArgs,
    CancelTaskArgs,
    CompleteTaskArgs,
    CreateReminderArgs,
    CreateTaskArgs,
    ListRemindersArgs,
    ListTasksArgs,
    TaskActionName,
)
from agent.tasks.matching import query_tokens
from agent.tasks.models import Recurrence, Reminder, Task, TaskPriority, TaskValidationError
from agent.tasks.recurrence import next_occurrence
from agent.tasks.service import ReminderService, TaskService
from agent.tasks.timeparse import TimeParseError, TimeParser, extract_time_of_day
from agent.tools.base import Tool
from backend.core.logging import get_logger
from backend.core.security import PermissionScope, RiskLevel

logger = get_logger(__name__)

MAX_SPOKEN_ITEMS = 5
_FIELD = "string"


@dataclass(frozen=True)
class Clarify:
    """The request cannot be carried out yet; `message` is the question to ask the user."""

    message: str
    field: str | None = None  # the argument the user's short answer fills in ("when"); None = a free-form question
    merge: bool = False       # append the answer to the earlier value ("tomorrow" + "at 6 PM") instead of replacing it


@dataclass(frozen=True)
class Ready:
    """A fully resolved operation. `params` are concrete JSON values and are what the permission is bound to."""

    params: dict[str, Any]
    confirm_prompt: str | None = None  # spoken when the operation needs the user's confirmation


@dataclass(frozen=True)
class TaskToolContext:
    tasks: TaskService | None
    reminders: ReminderService | None
    parser: TimeParser
    clock: Any  # Callable[[], datetime]

    def now(self) -> datetime:
        return self.clock()


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def _from_iso(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError("timestamp must be timezone-aware")
    return parsed


def _end_of_day(value: datetime) -> datetime:
    return datetime.combine(value.date(), time(23, 59), tzinfo=value.tzinfo)


def _describe_task(task: Task, now: datetime, zone) -> str:
    text = task.title
    if task.due_at is not None:
        text += f", due {format_when(task.due_at, now, zone)}"
    if task.priority >= TaskPriority.HIGH:
        text += f" ({task.priority.name.lower()} priority)"
    return text


def _describe_reminder(reminder: Reminder, now: datetime) -> str:
    when = format_recurrence(reminder.recurrence) if reminder.recurrence else format_when(reminder.scheduled_at, now, reminder.zone)
    return f"{reminder.message} ({when})"


def _spoken_list(items: list[str]) -> str:
    shown = items[:MAX_SPOKEN_ITEMS]
    text = "; ".join(shown)
    if len(items) > len(shown):
        text += f"; and {len(items) - len(shown)} more"
    return text


def _candidates(kind: str, items: list[str]) -> str:
    return f"I found {len(items)} {kind} that could match: {_spoken_list(items)}. Which one do you mean?"


class TaskTool(Tool, ABC):
    """Common shape of the task/reminder tools."""

    action: TaskActionName
    allowed_scopes = (PermissionScope.ONE_TIME,)

    def __init__(self, context: TaskToolContext):
        self._ctx = context
        self.name = self.action.value

    @abstractmethod
    def resolve(self, args: BaseModel) -> Clarify | Ready:
        raise NotImplementedError

    def _tasks(self) -> TaskService:
        if self._ctx.tasks is None:
            raise TaskValidationError("Tasks are turned off.")
        return self._ctx.tasks

    def _reminders(self) -> ReminderService:
        if self._ctx.reminders is None:
            raise TaskValidationError("Reminders are turned off.")
        return self._ctx.reminders


class CreateTaskTool(TaskTool):
    action = TaskActionName.CREATE_TASK
    description = "Create a to-do task, optionally with a due time and a reminder at that time."
    input_schema = {
        "title": f"{_FIELD}, required: what to do",
        "notes": f"{_FIELD}, optional",
        "due": f"{_FIELD}, optional: the due time exactly as the user said it (e.g. 'tomorrow at 5 pm')",
        "priority": "low | medium | high | critical, optional",
        "remind": "boolean, optional: true if the user also wants a reminder at the due time",
    }
    requires_permission = False
    risk = RiskLevel.LOW

    def resolve(self, args: CreateTaskArgs) -> Clarify | Ready:
        ctx, now = self._ctx, self._ctx.now()
        due: datetime | None = None
        has_time = False
        if args.due:
            try:
                parsed = ctx.parser.parse(args.due, now)
            except TimeParseError as exc:
                return Clarify(str(exc))
            if parsed is None:
                return Clarify("I couldn't understand that due time. Could you say it like 'tomorrow at 5 PM'?")
            has_time = parsed.has_time
            due = parsed.value if has_time else _end_of_day(parsed.value)
        if args.remind:
            if ctx.reminders is None:
                return Clarify("Reminders are turned off, so I can't add one.")
            if due is None or not has_time:
                return Clarify("What time should I remind you?")
            if due <= now:
                return Clarify("That time has already passed. When should I remind you?")
        return Ready({
            "title": args.title, "notes": args.notes, "due_at": _iso(due) if due else None,
            "priority": args.priority.name if args.priority else None, "remind": args.remind,
        })

    def run(self, *, title: str, notes: str | None, due_at: str | None, priority: str | None, remind: bool,
            origin_session: str | None = None) -> str:
        tasks, now = self._tasks(), self._ctx.now()
        due = _from_iso(due_at) if due_at else None
        chosen = TaskPriority[priority] if priority else None
        if remind and due is not None:
            task, _ = tasks.create_task_with_reminder(
                title, due, notes=notes, priority=chosen, due_at=due, session_id=origin_session, source="conversation"
            )
        else:
            task = tasks.create_task(title, notes=notes, priority=chosen, due_at=due, session_id=origin_session,
                                     source="conversation")
        reply = f"Okay, I've added the task: {task.title}"
        if due is not None:
            reply += f", due {format_when(due, now, self._ctx.parser.zone)}"
        reply += ". I'll remind you then." if remind and due is not None else "."
        return reply


class CreateReminderTool(TaskTool):
    action = TaskActionName.CREATE_REMINDER
    description = "Schedule a reminder for a time, or a repeating reminder (daily, weekly, monthly)."
    input_schema = {
        "message": f"{_FIELD}, required: what to remind about (e.g. 'submit my assignment')",
        "when": f"{_FIELD}: the time exactly as the user said it (e.g. 'tomorrow at 9 AM', 'in 30 minutes')",
        "recurrence": f"{_FIELD}, only for repeating reminders, as the user said it (e.g. 'every Monday at 8 AM')",
    }
    requires_permission = False
    risk = RiskLevel.LOW

    def resolve(self, args: CreateReminderArgs) -> Clarify | Ready:
        ctx, now = self._ctx, self._ctx.now()
        if ctx.reminders is None:
            return Clarify("Reminders are turned off, so I can't set one.")
        recurrence_text = " ".join(t for t in (args.recurrence, args.when) if t)
        try:
            recurrence = ctx.parser.parse_recurrence(recurrence_text) if recurrence_text else None
            if recurrence is None and args.recurrence:
                return Clarify("I couldn't understand how often that should repeat. Try 'every Monday at 8 AM'.")
            if recurrence is not None:
                first = next_occurrence(recurrence, now, ctx.parser.zone)
                return Ready({"message": args.message, "scheduled_at": _iso(first), "recurrence": recurrence.to_json()})
            if not args.when:
                return Clarify("When should I remind you?", field="when")
            parsed = ctx.parser.parse(args.when, now)
        except TimeParseError as exc:
            return Clarify(str(exc))
        if parsed is None:
            return Clarify("I couldn't understand that time. Could you say it like 'tomorrow at 9 AM'?", field="when")
        if not parsed.has_time:
            return Clarify(f"What time {format_when(parsed.value, now, ctx.parser.zone, with_time=False)}?", field="when", merge=True)
        if parsed.value <= now:
            return Clarify("That time has already passed. When should I remind you?", field="when")
        return Ready({"message": args.message, "scheduled_at": _iso(parsed.value), "recurrence": None})

    _last_created: dict[str, tuple[str, str, datetime, datetime]] | None = None  # session -> (id, message, when, created_at)

    def plan_correction(self, session_id: str, phrase: str) -> Clarify | Ready | None:
        """The user just said "make that 6 PM" / "no, tomorrow": a corrected version of the reminder created in this session
        moments ago. None if there is nothing recent to correct. Keeps the part the user did not change (the time when only the
        day is given, the day when only the time is given). The old reminder is replaced, not duplicated (`replaces`)."""
        ctx, now = self._ctx, self._ctx.now()
        last = (self._last_created or {}).get(session_id)
        if last is None or ctx.reminders is None or now - last[3] > timedelta(minutes=5):
            return None
        reminder_id, message, old_when, _ = last
        try:
            current = self._reminders().get_reminder(reminder_id)
        except Exception:  # noqa: BLE001 - gone or unreadable: nothing safe to correct
            return None
        if current.status.value != "scheduled" or current.is_recurring:
            return None
        zone = ctx.parser.zone
        old_local = old_when.astimezone(zone)
        time_of_day, rest = extract_time_of_day(phrase)
        try:
            if time_of_day is not None and not re.sub(r"\b(at|around|by|on|the|make|it|that|to|for)\b", " ", rest).strip():
                new_local = old_local.replace(hour=time_of_day[0], minute=time_of_day[1], second=0, microsecond=0)  # same day, new time
            else:
                parsed = ctx.parser.parse(phrase, now)
                if parsed is None:
                    return Clarify("I couldn't understand the new time. Could you say it like 'tomorrow at 9 AM'?")
                if parsed.has_time:
                    new_local = parsed.value.astimezone(zone)
                else:  # only the day changed: keep the time of day
                    day = parsed.value.astimezone(zone).date()
                    new_local = datetime.combine(day, old_local.timetz(), tzinfo=zone)
        except TimeParseError as exc:
            return Clarify(str(exc))
        if new_local <= now:
            return Clarify("That time has already passed. What is the correct time?")
        return Ready({"message": message, "scheduled_at": _iso(new_local), "recurrence": None, "replaces": reminder_id})

    def run(self, *, message: str, scheduled_at: str, recurrence: dict[str, Any] | None,
            origin_session: str | None = None, replaces: str | None = None) -> str:
        reminders, now = self._reminders(), self._ctx.now()
        rec = Recurrence.model_validate(recurrence) if recurrence else None
        if replaces:
            try:
                reminders.cancel_reminder(replaces)  # a correction replaces the reminder, never adds a second one
            except Exception as exc:  # noqa: BLE001 - already fired/cancelled: then a new reminder would be a duplicate of nothing
                logger.warning("Reminder to correct could not be cancelled (%s)", type(exc).__name__)
                raise
        reminder = reminders.create_reminder(
            message, _from_iso(scheduled_at), recurrence=rec, session_id=origin_session, source="conversation"
        )
        if origin_session and rec is None:
            if self._last_created is None:
                self._last_created = {}
            self._last_created[origin_session] = (reminder.reminder_id, reminder.message, reminder.scheduled_at, now)
        if rec is not None:
            return f"Okay, I'll remind you {format_recurrence(rec)}: {reminder.message}."
        lead = "Okay, I've changed it. " if replaces else "Okay, "
        return f"{lead}I'll remind you {format_when(reminder.scheduled_at, now, self._ctx.parser.zone)}: {reminder.message}."


class ListTasksTool(TaskTool):
    action = TaskActionName.LIST_TASKS
    description = "List the user's tasks: due today, overdue, upcoming, or all incomplete."
    input_schema = {"scope": "today | overdue | upcoming | incomplete (default incomplete)"}
    requires_permission = False
    risk = RiskLevel.LOW

    def resolve(self, args: ListTasksArgs) -> Clarify | Ready:
        return Ready({"scope": args.scope})

    def run(self, *, scope: str, origin_session: str | None = None) -> str:
        tasks, now, zone = self._tasks(), self._ctx.now(), self._ctx.parser.zone
        describe = lambda items: _spoken_list([_describe_task(t, now, zone) for t in items])  # noqa: E731
        if scope == "today":
            overdue, today = tasks.overdue_tasks(), tasks.tasks_due_today()
            today = [t for t in today if t.task_id not in {o.task_id for o in overdue}]
            if not overdue and not today:
                return "You have no tasks due today."
            parts = []
            if overdue:
                parts.append(f"{len(overdue)} overdue: {describe(overdue)}")
            if today:
                parts.append(f"{len(today)} due today: {describe(today)}")
            return "You have " + ". And ".join(parts) + "."
        if scope == "overdue":
            found = tasks.overdue_tasks()
            return f"You have {len(found)} overdue: {describe(found)}." if found else "You have no overdue tasks."
        if scope == "upcoming":
            found = tasks.upcoming_tasks()
            return f"You have {len(found)} upcoming: {describe(found)}." if found else "You have no upcoming tasks."
        found = tasks.incomplete_tasks()
        return f"You have {len(found)} incomplete: {describe(found)}." if found else "You have no incomplete tasks."


class ListRemindersTool(TaskTool):
    action = TaskActionName.LIST_REMINDERS
    description = "List the user's scheduled reminders: today, tomorrow, upcoming, or the next one."
    input_schema = {"scope": "today | tomorrow | upcoming | next (default upcoming)"}
    requires_permission = False
    risk = RiskLevel.LOW

    def resolve(self, args: ListRemindersArgs) -> Clarify | Ready:
        return Ready({"scope": args.scope})

    def run(self, *, scope: str, origin_session: str | None = None) -> str:
        reminders, now = self._reminders(), self._ctx.now()
        describe = lambda items: _spoken_list([_describe_reminder(r, now) for r in items])  # noqa: E731
        if scope == "next":
            nxt = reminders.next_reminder()
            return f"Your next reminder is {_describe_reminder(nxt, now)}." if nxt else "You have no upcoming reminders."
        if scope in ("today", "tomorrow"):
            found = reminders.reminders_on_day(0 if scope == "today" else 1)
            return f"You have {len(found)} reminders {scope}: {describe(found)}." if found else f"You have no reminders {scope}."
        found = reminders.upcoming_reminders()
        return f"You have {len(found)} upcoming reminders: {describe(found)}." if found else "You have no upcoming reminders."


class CompleteTaskTool(TaskTool):
    action = TaskActionName.COMPLETE_TASK
    description = "Mark an open task as completed. Describe the task in words; ids are never used."
    input_schema = {"query": f"{_FIELD}, required: words identifying the task (e.g. 'JARVIS documentation')"}
    requires_permission = False
    risk = RiskLevel.LOW

    def resolve(self, args: CompleteTaskArgs) -> Clarify | Ready:
        tasks = self._tasks()
        found = tasks.find_matching_tasks(args.query)
        if not found:
            return Clarify("I couldn't find an open task matching that.")
        if len(found) > 1:
            return Clarify(_candidates("tasks", [t.title for t in found]))
        return Ready({"task_id": found[0].task_id})

    def run(self, *, task_id: str, origin_session: str | None = None) -> str:
        return f"Done. I've marked the task completed: {self._tasks().complete_task(task_id).title}."


class CancelTaskTool(TaskTool):
    action = TaskActionName.CANCEL_TASK
    description = "Cancel an open task (asks the user to confirm first). Describe the task in words; ids are never used."
    input_schema = {"query": f"{_FIELD}, required: words identifying the task"}
    requires_permission = True
    risk = RiskLevel.MEDIUM

    def resolve(self, args: CancelTaskArgs) -> Clarify | Ready:
        found = self._tasks().find_matching_tasks(args.query)
        if not found:
            return Clarify("I couldn't find an open task matching that.")
        if len(found) > 1:
            return Clarify(_candidates("tasks", [t.title for t in found]))
        return Ready({"task_id": found[0].task_id}, f"Do you want me to cancel the task: {found[0].title}? Say yes to confirm.")

    def run(self, *, task_id: str, origin_session: str | None = None) -> str:
        return f"Okay, I've cancelled the task: {self._tasks().cancel_task(task_id).title}."


class CancelReminderTool(TaskTool):
    action = TaskActionName.CANCEL_REMINDER
    description = (
        "Cancel a scheduled reminder (asks the user to confirm first). Cancelling a repeating reminder stops "
        "every future occurrence. Describe the reminder in words; ids are never used."
    )
    input_schema = {
        "query": f"{_FIELD}, required: words identifying the reminder (e.g. 'assignment')",
        "when": f"{_FIELD}, optional: its time of day if the user gave one (e.g. '9 AM')",
    }
    requires_permission = True
    risk = RiskLevel.MEDIUM

    def resolve(self, args: CancelReminderArgs) -> Clarify | Ready:
        reminders, zone = self._reminders(), self._ctx.parser.zone
        clock_time, remaining = extract_time_of_day(args.when or "")
        if clock_time is None:
            clock_time, remaining = extract_time_of_day(args.query)
            remaining = remaining if clock_time is not None else args.query
        else:
            remaining = args.query
        if query_tokens(remaining):
            found = reminders.find_matching_reminders(remaining)
        elif clock_time is not None:
            found = reminders.upcoming_reminders()  # only a time was given: identify it by its time
        else:
            found = []
        if clock_time is not None:
            found = [r for r in found if (lambda t: (t.hour, t.minute))(r.scheduled_at.astimezone(zone)) == clock_time]
        if not found:
            return Clarify("I couldn't find a scheduled reminder matching that.")
        if len(found) > 1:
            return Clarify(_candidates("reminders", [_describe_reminder(r, self._ctx.now()) for r in found]))
        target = found[0]
        prompt = f"Do you want me to cancel the reminder: {_describe_reminder(target, self._ctx.now())}?"
        if target.is_recurring:
            prompt += " This will stop all future occurrences."
        return Ready({"reminder_id": target.reminder_id}, prompt + " Say yes to confirm.")

    def run(self, *, reminder_id: str, origin_session: str | None = None) -> str:
        cancelled = self._reminders().cancel_reminder(reminder_id)
        return "Okay, I've cancelled that repeating reminder." if cancelled.is_recurring else "Okay, I've cancelled that reminder."


def build_task_tools(context: TaskToolContext) -> list[TaskTool]:
    """The tools that are enabled: task tools need TaskService, reminder tools need ReminderService."""
    tools: list[TaskTool] = []
    if context.tasks is not None:
        tools += [CreateTaskTool(context), ListTasksTool(context), CompleteTaskTool(context), CancelTaskTool(context)]
    if context.reminders is not None:
        tools += [CreateReminderTool(context), ListRemindersTool(context), CancelReminderTool(context)]
    return tools
