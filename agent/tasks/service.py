"""TaskService and ReminderService: all task/reminder rules. Persistence is the repository's job.

The LLM never reaches these classes directly: a validated action goes through
the PermissionManager and a Tool, which calls them (agent/tasks/tools.py).
Logging carries ids and statuses only, never titles, notes or messages.
"""

from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from agent.tasks.formatting import local_day_bounds
from agent.tasks.matching import find_matches
from agent.tasks.models import (
    MAX_HISTORY_ENTRIES,
    OPEN_STATUSES,
    REOPEN_FROM,
    TASK_TRANSITIONS,
    InvalidTransition,
    Recurrence,
    Reminder,
    ReminderStatus,
    Task,
    TaskNotFound,
    TaskPriority,
    TaskStatus,
    TaskValidationError,
    to_utc,
    utcnow,
)
from agent.tasks.recurrence import next_occurrence
from agent.tasks.repository import TaskRepository
from backend.core.logging import get_logger

logger = get_logger(__name__)

Clock = Callable[[], datetime]

DEFAULT_LIMIT = 50
MAX_LIMIT = 200
MATCH_SCAN_LIMIT = 500  # open items scanned when identifying "my X task"
DEFAULT_LEASE_SECONDS = 120.0  # how long a delivering scheduler holds a reminder before another may retry it
MAX_DELIVERY_ATTEMPTS = 20  # polls (about five minutes at the default interval) before giving up
PAST_GRACE = timedelta(seconds=5)  # a reminder time this close to "now" is still accepted

_UNSET: Any = object()


def _history(metadata: dict[str, Any], old: str, new: str, now: datetime) -> dict[str, Any]:
    """Metadata with one more status-change record (bounded), so completion/cancellation history is kept."""
    entries = [*metadata.get("history", []), {"from": old, "to": new, "at": now.isoformat()}]
    return {**metadata, "history": entries[-MAX_HISTORY_ENTRIES:]}


def _require_aware(value: datetime | None, name: str) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None or value.utcoffset() is None:
        raise TaskValidationError(f"{name} must be timezone-aware")
    return to_utc(value)


def _new_reminder(
    *, message: str, scheduled_at: datetime | None, recurrence: Recurrence | None, zone: ZoneInfo, now: datetime,
    task_id: str | None, session_id: str | None, source: str, metadata: dict[str, Any] | None,
) -> Reminder:
    if scheduled_at is None:
        if recurrence is None:
            raise TaskValidationError("A reminder needs a time")
        scheduled_at = next_occurrence(recurrence, now, zone)  # first occurrence of a recurrence
    scheduled_at = _require_aware(scheduled_at, "scheduled_at")
    if scheduled_at < now - PAST_GRACE:
        raise TaskValidationError("A reminder cannot be scheduled in the past")
    try:
        return Reminder(
            task_id=task_id, message=message, scheduled_at=scheduled_at, timezone=zone.key, recurrence=recurrence,
            created_at=now, updated_at=now, session_id=session_id, source=source, metadata=dict(metadata or {}),
        )
    except ValueError as exc:
        raise TaskValidationError(_first_error(exc)) from None


def _first_error(exc: ValueError) -> str:
    """A content-free validation message (pydantic errors echo the offending input)."""
    errors = getattr(exc, "errors", None)
    if callable(errors):
        first = errors()[0]
        return f"Invalid {'.'.join(str(p) for p in first['loc'])}: {first['msg']}"
    return "Invalid value"


class TaskService:
    def __init__(
        self,
        repository: TaskRepository,
        *,
        zone: ZoneInfo,
        clock: Clock = utcnow,
        default_priority: TaskPriority = TaskPriority.MEDIUM,
    ):
        self._repo = repository
        self._zone = zone
        self._clock = clock
        self._default_priority = default_priority

    # ---- create / read -------------------------------------------------------

    def create_task(
        self,
        title: str,
        *,
        notes: str | None = None,
        priority: TaskPriority | None = None,
        due_at: datetime | None = None,
        session_id: str | None = None,
        source: str = "api",
        metadata: dict[str, Any] | None = None,
    ) -> Task:
        return self._create(title, notes, priority, due_at, session_id, source, metadata, reminder=None)[0]

    def create_task_with_reminder(
        self,
        title: str,
        remind_at: datetime,
        *,
        reminder_message: str | None = None,
        recurrence: Recurrence | None = None,
        notes: str | None = None,
        priority: TaskPriority | None = None,
        due_at: datetime | None = None,
        session_id: str | None = None,
        source: str = "api",
        metadata: dict[str, Any] | None = None,
    ) -> tuple[Task, Reminder]:
        """Create a task and a reminder for it in ONE transaction: both exist afterwards, or neither."""
        reminder = {"at": remind_at, "message": reminder_message, "recurrence": recurrence}
        task, created = self._create(title, notes, priority, due_at, session_id, source, metadata, reminder=reminder)
        assert created is not None
        return task, created

    def _create(
        self, title, notes, priority, due_at, session_id, source, metadata, reminder: dict[str, Any] | None
    ) -> tuple[Task, Reminder | None]:
        now = self._clock()
        try:
            task = Task(
                title=title, notes=notes, priority=priority or self._default_priority, created_at=now, updated_at=now,
                due_at=_require_aware(due_at, "due_at"), session_id=session_id, source=source,
                metadata=dict(metadata or {}),
            )
        except ValueError as exc:
            raise TaskValidationError(_first_error(exc)) from None
        new_reminder = None
        if reminder is not None:
            new_reminder = _new_reminder(
                message=reminder["message"] or task.title, scheduled_at=reminder["at"],
                recurrence=reminder["recurrence"], zone=self._zone, now=now, task_id=task.task_id,
                session_id=session_id, source=source, metadata=None,
            )
        with self._repo.unit_of_work():
            self._repo.add_task(task)
            if new_reminder is not None:
                self._repo.add_reminder(new_reminder)
        logger.info("Task created (task=%s, priority=%s, reminder=%s)", task.task_id, task.priority.name,
                    new_reminder.reminder_id if new_reminder else None)
        if new_reminder is not None:
            logger.info("Reminder scheduled (reminder=%s, task=%s)", new_reminder.reminder_id, task.task_id)
        return task, new_reminder

    def get_task(self, task_id: str) -> Task:
        task = self._repo.get_task(task_id)
        if task is None:
            raise TaskNotFound("No such task")
        return task

    def list_tasks(
        self,
        *,
        statuses: set[TaskStatus] | None = None,
        due_from: datetime | None = None,
        due_before: datetime | None = None,
        overdue_only: bool = False,
        limit: int = DEFAULT_LIMIT,
        now: datetime | None = None,
    ) -> list[Task]:
        """Default order: overdue first, then due date, then priority (highest first), then creation time."""
        return self._repo.list_tasks(
            now=now or self._clock(), statuses=statuses, due_from=due_from, due_before=due_before,
            overdue_only=overdue_only, limit=max(1, min(limit, MAX_LIMIT)),
        )

    def incomplete_tasks(self, limit: int = DEFAULT_LIMIT) -> list[Task]:
        return self.list_tasks(statuses=set(OPEN_STATUSES), limit=limit)

    def overdue_tasks(self, limit: int = DEFAULT_LIMIT) -> list[Task]:
        return self.list_tasks(statuses=set(OPEN_STATUSES), overdue_only=True, limit=limit)

    def tasks_due_today(self, limit: int = DEFAULT_LIMIT) -> list[Task]:
        """Open tasks due during the user's local today (not the overdue ones from earlier days)."""
        start, end = local_day_bounds(0, self._clock(), self._zone)
        return self.list_tasks(statuses=set(OPEN_STATUSES), due_from=start, due_before=end, limit=limit)

    def upcoming_tasks(self, days: int = 7, limit: int = DEFAULT_LIMIT) -> list[Task]:
        """Open tasks due from now until the end of the local day `days` days ahead."""
        now = self._clock()
        _, end = local_day_bounds(max(days, 1) - 1, now, self._zone)
        return self.list_tasks(statuses=set(OPEN_STATUSES), due_from=now, due_before=end, limit=limit)

    def find_due_tasks(self, now: datetime | None = None, limit: int = DEFAULT_LIMIT) -> list[Task]:
        """Open tasks whose due time has arrived (due now or overdue)."""
        moment = now or self._clock()
        return self.list_tasks(
            statuses=set(OPEN_STATUSES), due_before=moment + timedelta(microseconds=1), limit=limit, now=moment
        )

    def find_matching_tasks(self, query: str, *, include_closed: bool = False, limit: int = 10) -> list[Task]:
        """Candidate tasks for a spoken description. More than one candidate means: ask the user."""
        statuses = None if include_closed else set(OPEN_STATUSES)
        tasks = self.list_tasks(statuses=statuses, limit=MATCH_SCAN_LIMIT)
        ids = set(find_matches(query, [(t.task_id, t.title) for t in tasks]))
        return [t for t in tasks if t.task_id in ids][:limit]

    # ---- changes ---------------------------------------------------------------

    def update_task(
        self,
        task_id: str,
        *,
        title: str | None = None,
        notes: str | None = _UNSET,
        priority: TaskPriority | None = None,
        due_at: datetime | None = _UNSET,
    ) -> Task:
        """Edit an open task. Status is never changed here except OVERDUE -> PENDING when the new due date is in the future."""
        task = self.get_task(task_id)
        if not task.is_open:
            raise InvalidTransition(f"Cannot edit a {task.status.value} task; reopen it first")
        now = self._clock()
        try:
            changes: dict[str, Any] = Task(
                title=title if title is not None else task.title,
                notes=task.notes if notes is _UNSET else notes,
                priority=priority or task.priority,
                due_at=task.due_at if due_at is _UNSET else _require_aware(due_at, "due_at"),
                created_at=task.created_at, updated_at=now,
            ).model_dump(include={"title", "notes", "priority", "due_at"})
        except ValueError as exc:
            raise TaskValidationError(_first_error(exc)) from None
        values: dict[str, Any] = {**changes, "priority": int(changes["priority"]), "updated_at": now}
        extra = task.metadata
        if task.status is TaskStatus.OVERDUE and changes["due_at"] is not None and changes["due_at"] >= now:
            values["status"] = TaskStatus.PENDING.value
            extra = _history(extra, task.status.value, TaskStatus.PENDING.value, now)
            values["extra"] = extra
        if not self._repo.update_task(task_id, values, expected_status=task.status):
            raise InvalidTransition("The task changed while updating it")
        return self.get_task(task_id)

    def start_task(self, task_id: str) -> Task:
        return self._transition(task_id, TaskStatus.IN_PROGRESS)

    def complete_task(self, task_id: str) -> Task:
        """Mark done: sets completed_at and cancels the task's scheduled reminders, atomically."""
        return self._transition(task_id, TaskStatus.COMPLETED)

    def cancel_task(self, task_id: str) -> Task:
        """Cancel: sets cancelled_at and cancels the task's scheduled reminders, atomically."""
        return self._transition(task_id, TaskStatus.CANCELLED)

    def reopen_task(self, task_id: str) -> Task:
        """Explicit reopen of a completed or cancelled task (back to PENDING). The earlier history is kept."""
        return self._transition(task_id, TaskStatus.PENDING, reopen=True)

    def mark_overdue(self, now: datetime | None = None) -> int:
        """PENDING tasks past their due time become OVERDUE. Returns how many changed."""
        moment = now or self._clock()
        changed = 0
        for task in self._repo.list_tasks(now=moment, statuses={TaskStatus.PENDING}, overdue_only=True, limit=MAX_LIMIT):
            try:
                self._transition(task.task_id, TaskStatus.OVERDUE)
                changed += 1
            except InvalidTransition:
                continue  # someone completed/cancelled it meanwhile
        return changed

    def delete_task(self, task_id: str) -> None:
        """Physically delete a task and its reminders. Code-level only: no voice or LLM path reaches it."""
        if not self._repo.delete_task(task_id):
            raise TaskNotFound("No such task")
        logger.info("Task deleted (task=%s)", task_id)

    def _transition(self, task_id: str, new: TaskStatus, reopen: bool = False) -> Task:
        for _ in range(3):
            task = self.get_task(task_id)
            allowed = task.status in REOPEN_FROM if reopen else new in TASK_TRANSITIONS[task.status]
            if not allowed:
                raise InvalidTransition(f"Cannot change a {task.status.value} task to {new.value}")
            now = self._clock()
            values: dict[str, Any] = {
                "status": new.value, "updated_at": now, "extra": _history(task.metadata, task.status.value, new.value, now),
            }
            if new is TaskStatus.COMPLETED:
                values["completed_at"] = now
            elif new is TaskStatus.CANCELLED:
                values["cancelled_at"] = now
            elif reopen:  # the history record keeps when it was completed/cancelled
                values.update(completed_at=None, cancelled_at=None)
            with self._repo.unit_of_work():
                matched = self._repo.update_task(task_id, values, expected_status=task.status)
                if matched and new in (TaskStatus.COMPLETED, TaskStatus.CANCELLED):
                    self._repo.cancel_reminders_for_task(task_id, now)
            if matched:
                logger.info("Task %s (task=%s, %s -> %s)", "reopened" if reopen else "status changed", task_id,
                            task.status.value, new.value)
                return self.get_task(task_id)
            # The status changed under us (a concurrent caller); re-read and re-validate.
        raise InvalidTransition("The task keeps changing; try again")


class ReminderService:
    def __init__(
        self,
        repository: TaskRepository,
        *,
        zone: ZoneInfo,
        clock: Clock = utcnow,
        lease_seconds: float = DEFAULT_LEASE_SECONDS,
        max_delivery_attempts: int = MAX_DELIVERY_ATTEMPTS,
    ):
        self._repo = repository
        self._zone = zone
        self._clock = clock
        self._lease = timedelta(seconds=lease_seconds)
        self._max_attempts = max_delivery_attempts

    # ---- create / read -------------------------------------------------------

    def create_reminder(
        self,
        message: str,
        scheduled_at: datetime | None = None,
        *,
        recurrence: Recurrence | None = None,
        task_id: str | None = None,
        session_id: str | None = None,
        source: str = "api",
        metadata: dict[str, Any] | None = None,
    ) -> Reminder:
        """Schedule a reminder. With a recurrence and no `scheduled_at`, the first occurrence is computed.
        A recurring reminder is ONE row that moves to its next occurrence after each trigger."""
        now = self._clock()
        reminder = _new_reminder(
            message=message, scheduled_at=scheduled_at, recurrence=recurrence, zone=self._zone, now=now,
            task_id=task_id, session_id=session_id, source=source, metadata=metadata,
        )
        with self._repo.unit_of_work():
            if task_id is not None:
                task = self._repo.get_task(task_id)
                if task is None:
                    raise TaskNotFound("No such task")
                if not task.is_open:
                    raise InvalidTransition(f"Cannot add a reminder to a {task.status.value} task")
            self._repo.add_reminder(reminder)
        logger.info("Reminder scheduled (reminder=%s, recurring=%s, task=%s)", reminder.reminder_id,
                    reminder.is_recurring, task_id)
        return reminder

    def get_reminder(self, reminder_id: str) -> Reminder:
        reminder = self._repo.get_reminder(reminder_id)
        if reminder is None:
            raise TaskNotFound("No such reminder")
        return reminder

    def list_reminders(
        self,
        *,
        statuses: set[ReminderStatus] | None = None,
        scheduled_from: datetime | None = None,
        scheduled_before: datetime | None = None,
        task_id: str | None = None,
        limit: int = DEFAULT_LIMIT,
    ) -> list[Reminder]:
        return self._repo.list_reminders(
            statuses=statuses, scheduled_from=scheduled_from, scheduled_before=scheduled_before, task_id=task_id,
            limit=max(1, min(limit, MAX_LIMIT)),
        )

    def upcoming_reminders(self, limit: int = DEFAULT_LIMIT) -> list[Reminder]:
        return self.list_reminders(statuses={ReminderStatus.SCHEDULED}, limit=limit)

    def reminders_on_day(self, day_offset: int, limit: int = DEFAULT_LIMIT) -> list[Reminder]:
        """Scheduled reminders during the user's local day `day_offset` days from today (0 = today)."""
        start, end = local_day_bounds(day_offset, self._clock(), self._zone)
        return self.list_reminders(
            statuses={ReminderStatus.SCHEDULED}, scheduled_from=start, scheduled_before=end, limit=limit
        )

    def next_reminder(self) -> Reminder | None:
        found = self.list_reminders(statuses={ReminderStatus.SCHEDULED}, limit=1)
        return found[0] if found else None

    def find_matching_reminders(self, query: str, limit: int = 10) -> list[Reminder]:
        reminders = self.list_reminders(statuses={ReminderStatus.SCHEDULED}, limit=MATCH_SCAN_LIMIT)
        ids = set(find_matches(query, [(r.reminder_id, r.message) for r in reminders]))
        return [r for r in reminders if r.reminder_id in ids][:limit]

    # ---- cancel ------------------------------------------------------------------

    def cancel_reminder(self, reminder_id: str) -> Reminder:
        """Cancel a scheduled reminder. For a recurring one this ends all future occurrences."""
        now = self._clock()
        reminder = self.get_reminder(reminder_id)
        if reminder.status is not ReminderStatus.SCHEDULED:
            raise InvalidTransition(f"Cannot cancel a {reminder.status.value} reminder")
        values = {"status": ReminderStatus.CANCELLED.value, "cancelled_at": now, "updated_at": now, "claimed_at": None}
        if not self._repo.update_reminder(reminder_id, values, expected_status=ReminderStatus.SCHEDULED):
            raise InvalidTransition("The reminder changed while cancelling it")
        logger.info("Reminder cancelled (reminder=%s, recurring=%s)", reminder_id, reminder.is_recurring)
        return self.get_reminder(reminder_id)

    # ---- triggering ------------------------------------------------------------------
    # claim_delivery -> (deliver) -> complete_delivery / fail_delivery. The claim is an atomic lease, so a
    # reminder checked by the scheduler twice (or by two schedulers) is delivered once.

    def find_due_reminders(self, now: datetime | None = None, limit: int = DEFAULT_LIMIT) -> list[Reminder]:
        """Scheduled reminders whose time has come and that nobody is currently delivering."""
        moment = now or self._clock()
        return self._repo.find_due_reminders(moment, moment - self._lease, limit)

    def claim_delivery(self, reminder_id: str, now: datetime | None = None) -> Reminder | None:
        """Take the delivery lease. None if the reminder is not due, was cancelled, or another caller holds it."""
        moment = now or self._clock()
        if not self._repo.claim_reminder(reminder_id, moment, moment - self._lease):
            return None
        claimed = self._repo.get_reminder(reminder_id)
        if claimed is None or claimed.status is not ReminderStatus.SCHEDULED or claimed.claimed_at is None:
            return None
        return claimed

    def complete_delivery(self, claimed: Reminder, now: datetime | None = None) -> Reminder | None:
        """Record a REAL delivery. One-shot: TRIGGERED. Recurring: moves to its next occurrence.
        None if the claim was lost (cancelled or re-claimed): nothing is recorded twice."""
        return self._finish(claimed, now or self._clock(), delivered=True)

    def expire_missed(self, claimed: Reminder, now: datetime | None = None) -> Reminder | None:
        """Missed-reminder policy 'expire': no delivery. One-shot: EXPIRED. Recurring: skip to the next occurrence."""
        return self._finish(claimed, now or self._clock(), delivered=False)

    def fail_delivery(self, claimed: Reminder, now: datetime | None = None) -> Reminder | None:
        """Delivery failed: release the lease so it is retried, and give up after the maximum attempts
        (one-shot: EXPIRED, recurring: skip to the next occurrence). Never records a delivery."""
        moment = now or self._clock()
        attempts = claimed.delivery_attempts + 1
        if attempts >= self._max_attempts:
            logger.error("Reminder delivery failed permanently (reminder=%s, attempts=%d)", claimed.reminder_id, attempts)
            return self._finish(claimed, moment, delivered=False)
        matched = self._repo.update_reminder(
            claimed.reminder_id, {"claimed_at": None, "delivery_attempts": attempts, "updated_at": moment},
            expected_status=ReminderStatus.SCHEDULED, expected_claim=claimed.claimed_at,
        )
        return self.get_reminder(claimed.reminder_id) if matched else None

    def mark_triggered(self, reminder_id: str, now: datetime | None = None) -> Reminder | None:
        """Claim and record a trigger in one step (for callers that deliver by their own means).
        Triggering the same reminder again returns None: it never triggers twice."""
        moment = now or self._clock()
        claimed = self.claim_delivery(reminder_id, moment)
        return self.complete_delivery(claimed, moment) if claimed is not None else None

    def _finish(self, claimed: Reminder, now: datetime, delivered: bool) -> Reminder | None:
        token = claimed.claimed_at
        if token is None:
            return None
        values: dict[str, Any] = {"claimed_at": None, "delivery_attempts": 0, "updated_at": now}
        if delivered:
            values.update(triggered_at=now, occurrences=claimed.occurrences + 1)
        if claimed.recurrence is not None:
            # One row, one next occurrence: strictly after now and after the slot just handled.
            values["scheduled_at"] = next_occurrence(
                claimed.recurrence, max(now, claimed.scheduled_at), claimed.zone
            )
        else:
            values["status"] = (ReminderStatus.TRIGGERED if delivered else ReminderStatus.EXPIRED).value
        matched = self._repo.update_reminder(
            claimed.reminder_id, values, expected_status=ReminderStatus.SCHEDULED, expected_claim=token,
            expected_scheduled_at=claimed.scheduled_at,
        )
        if not matched:
            return None
        logger.info("Reminder %s (reminder=%s, recurring=%s)", "triggered" if delivered else "expired",
                    claimed.reminder_id, claimed.is_recurring)
        return self.get_reminder(claimed.reminder_id)
