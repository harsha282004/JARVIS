"""Task/reminder models and TaskService, against an isolated SQLite database (never your PostgreSQL).

The same tests run on a disposable PostgreSQL when JARVIS_TEST_DATABASE_URL is set (see conftest).
"""

import threading
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import sessionmaker

from agent.tasks.models import (
    OPEN_STATUSES,
    TASK_TRANSITIONS,
    Frequency,
    InvalidTransition,
    Recurrence,
    Reminder,
    ReminderStatus,
    Task,
    TaskNotFound,
    TaskPriority,
    TaskStatus,
    TaskStorageError,
    TaskValidationError,
)
from backend.models.base import Base
from backend.models.tasks import ReminderRow, TaskRow
from tests.task_helpers import IST, Clock, ist, make_services

NOW = datetime(2030, 3, 4, 9, 0, tzinfo=timezone.utc)  # Monday 14:30 IST


# ---- models --------------------------------------------------------------------------------------


def test_status_and_priority_are_typed_enums():
    assert {s.name for s in TaskStatus} == {"PENDING", "IN_PROGRESS", "COMPLETED", "CANCELLED", "OVERDUE"}
    assert {s.name for s in ReminderStatus} == {"SCHEDULED", "TRIGGERED", "CANCELLED", "EXPIRED"}
    assert TaskPriority.LOW < TaskPriority.MEDIUM < TaskPriority.HIGH < TaskPriority.CRITICAL
    assert OPEN_STATUSES == {TaskStatus.PENDING, TaskStatus.IN_PROGRESS, TaskStatus.OVERDUE}


def test_task_model_validation():
    with pytest.raises(ValueError):
        Task(title="   ", created_at=NOW, updated_at=NOW)
    with pytest.raises(ValueError):
        Task(title="x" * 201, created_at=NOW, updated_at=NOW)
    with pytest.raises(ValueError):  # naive timestamps are a bug, never guessed
        Task(title="a", created_at=datetime(2030, 1, 1), updated_at=NOW)
    with pytest.raises(ValueError):
        Task(title="a", created_at=NOW, updated_at=NOW, status="finished")
    with pytest.raises(ValueError):  # a completed task must say when
        Task(title="a", created_at=NOW, updated_at=NOW, status=TaskStatus.COMPLETED)
    task = Task(title="  Write\x00 the   report ", created_at=NOW, updated_at=NOW, due_at=ist(2030, 3, 5, 9))
    assert task.title == "Write the report"
    assert task.due_at == datetime(2030, 3, 5, 3, 30, tzinfo=timezone.utc)  # normalized to UTC
    assert task.status is TaskStatus.PENDING and task.priority is TaskPriority.MEDIUM


def test_reminder_model_validation():
    with pytest.raises(ValueError):
        Reminder(message="", scheduled_at=NOW, timezone="UTC", created_at=NOW, updated_at=NOW)
    with pytest.raises(ValueError):
        Reminder(message="x", scheduled_at=NOW, timezone="Mars/Olympus", created_at=NOW, updated_at=NOW)
    with pytest.raises(ValueError):
        Reminder(message="x", scheduled_at=datetime(2030, 1, 1), timezone="UTC", created_at=NOW, updated_at=NOW)
    with pytest.raises(ValueError):
        Reminder(message="x", scheduled_at=NOW, timezone="UTC", created_at=NOW, updated_at=NOW,
                 status=ReminderStatus.TRIGGERED)
    reminder = Reminder(message="x", scheduled_at=NOW, timezone="Asia/Kolkata", created_at=NOW, updated_at=NOW)
    assert reminder.status is ReminderStatus.SCHEDULED and not reminder.is_recurring


@pytest.mark.parametrize(
    "kwargs",
    [
        {"frequency": Frequency.WEEKLY, "hour": 8},  # weekly needs weekdays
        {"frequency": Frequency.WEEKLY, "hour": 8, "weekdays": (7,)},
        {"frequency": Frequency.WEEKLY, "hour": 8, "weekdays": (1, 1)},
        {"frequency": Frequency.MONTHLY, "hour": 8},  # monthly needs a day
        {"frequency": Frequency.MONTHLY, "hour": 8, "day_of_month": 32},
        {"frequency": Frequency.DAILY, "hour": 8, "weekdays": (1,)},
        {"frequency": Frequency.DAILY, "hour": 24},
    ],
)
def test_recurrence_is_structured_and_validated(kwargs):
    with pytest.raises(ValueError):
        Recurrence(**kwargs)


def test_transition_table_forbids_leaving_terminal_states():
    assert TASK_TRANSITIONS[TaskStatus.COMPLETED] == frozenset()
    assert TASK_TRANSITIONS[TaskStatus.CANCELLED] == frozenset()
    assert TaskStatus.OVERDUE in TASK_TRANSITIONS[TaskStatus.PENDING]


# ---- creation, retrieval, update ---------------------------------------------------------------


def test_create_and_get_task(session_factory):
    tasks, _, _, clock = make_services(session_factory)
    created = tasks.create_task(
        "Finish the JARVIS documentation", notes="chapter 3", priority=TaskPriority.HIGH,
        due_at=ist(2030, 3, 6, 17), session_id="s1", source="conversation", metadata={"k": "v"},
    )
    fetched = tasks.get_task(created.task_id)
    assert fetched == created
    assert fetched.status is TaskStatus.PENDING and fetched.priority is TaskPriority.HIGH
    assert fetched.created_at == clock() and fetched.session_id == "s1" and fetched.metadata == {"k": "v"}
    assert fetched.due_at == datetime(2030, 3, 6, 11, 30, tzinfo=timezone.utc)
    with pytest.raises(TaskNotFound):
        tasks.get_task("does-not-exist")


def test_default_priority_comes_from_configuration(session_factory):
    tasks, *_ = make_services(session_factory, default_priority=TaskPriority.LOW)
    assert tasks.create_task("a").priority is TaskPriority.LOW


def test_invalid_task_input_is_rejected_without_echoing_content(session_factory):
    tasks, *_ = make_services(session_factory)
    with pytest.raises(TaskValidationError) as exc:
        tasks.create_task("s3cret-title " * 40)
    assert "s3cret" not in str(exc.value)
    with pytest.raises(TaskValidationError):
        tasks.create_task("ok", due_at=datetime(2030, 3, 5, 9))  # naive due date
    assert tasks.list_tasks() == []


def test_update_task_edits_open_tasks_only(session_factory):
    tasks, _, _, clock = make_services(session_factory)
    task = tasks.create_task("old title", due_at=ist(2030, 3, 6, 9))
    clock.advance(minutes=5)
    updated = tasks.update_task(task.task_id, title="new title", priority=TaskPriority.CRITICAL, notes="n")
    assert (updated.title, updated.priority, updated.notes) == ("new title", TaskPriority.CRITICAL, "n")
    assert updated.due_at == task.due_at and updated.updated_at == clock()
    assert tasks.update_task(task.task_id, due_at=None).due_at is None  # explicit clear
    assert tasks.update_task(task.task_id, notes=None).notes is None
    tasks.complete_task(task.task_id)
    with pytest.raises(InvalidTransition):
        tasks.update_task(task.task_id, title="changed after completion")
    with pytest.raises(TaskValidationError):
        tasks.update_task(tasks.create_task("b").task_id, title="  ")


# ---- transitions -------------------------------------------------------------------------------


def test_complete_sets_completed_at_and_keeps_history(session_factory):
    tasks, _, _, clock = make_services(session_factory)
    task = tasks.create_task("report")
    clock.advance(hours=1)
    done = tasks.complete_task(task.task_id)
    assert done.status is TaskStatus.COMPLETED and done.completed_at == clock() and done.cancelled_at is None
    assert done.metadata["history"][-1]["from"] == "pending" and done.metadata["history"][-1]["to"] == "completed"


def test_cancel_sets_cancelled_at(session_factory):
    tasks, _, _, clock = make_services(session_factory)
    task = tasks.create_task("report")
    cancelled = tasks.cancel_task(task.task_id)
    assert cancelled.status is TaskStatus.CANCELLED and cancelled.cancelled_at == clock()


def test_start_moves_pending_to_in_progress(session_factory):
    tasks, *_ = make_services(session_factory)
    task = tasks.create_task("report")
    assert tasks.start_task(task.task_id).status is TaskStatus.IN_PROGRESS
    assert tasks.complete_task(task.task_id).status is TaskStatus.COMPLETED


@pytest.mark.parametrize("terminal", ["complete_task", "cancel_task"])
def test_invalid_transitions_are_refused(session_factory, terminal):
    tasks, *_ = make_services(session_factory)
    task = tasks.create_task("report")
    getattr(tasks, terminal)(task.task_id)
    for attempt in (tasks.start_task, tasks.complete_task, tasks.cancel_task):
        with pytest.raises(InvalidTransition):
            attempt(task.task_id)  # COMPLETED -> IN_PROGRESS must never happen silently
    assert tasks.get_task(task.task_id).status in (TaskStatus.COMPLETED, TaskStatus.CANCELLED)


def test_reopen_is_an_explicit_supported_operation(session_factory):
    tasks, _, _, clock = make_services(session_factory)
    task = tasks.create_task("report")
    tasks.complete_task(task.task_id)
    clock.advance(hours=1)
    reopened = tasks.reopen_task(task.task_id)
    assert reopened.status is TaskStatus.PENDING and reopened.completed_at is None
    assert [h["to"] for h in reopened.metadata["history"]] == ["completed", "pending"]  # history preserved
    with pytest.raises(InvalidTransition):
        tasks.reopen_task(task.task_id)  # only completed/cancelled tasks can be reopened
    tasks.cancel_task(task.task_id)
    assert tasks.reopen_task(task.task_id).status is TaskStatus.PENDING


def test_double_completion_is_refused_and_the_state_guard_is_atomic(session_factory):
    tasks, _, repo, clock = make_services(session_factory)
    task = tasks.create_task("report")
    assert tasks.complete_task(task.task_id).status is TaskStatus.COMPLETED
    with pytest.raises(InvalidTransition):
        tasks.complete_task(task.task_id)
    # A racing writer that still believes the task is PENDING loses: the conditional UPDATE matches nothing.
    assert repo.update_task(task.task_id, {"status": "cancelled"}, expected_status=TaskStatus.PENDING) is False
    assert tasks.get_task(task.task_id).status is TaskStatus.COMPLETED


def test_overdue_sweep_moves_only_pending_tasks_past_due(session_factory):
    tasks, _, _, clock = make_services(session_factory)
    late = tasks.create_task("late", due_at=NOW - timedelta(hours=1))
    working = tasks.create_task("working", due_at=NOW - timedelta(hours=1))
    tasks.start_task(working.task_id)
    future = tasks.create_task("future", due_at=NOW + timedelta(hours=1))
    undated = tasks.create_task("undated")
    assert tasks.mark_overdue() == 1
    assert tasks.mark_overdue() == 0  # idempotent
    assert tasks.get_task(late.task_id).status is TaskStatus.OVERDUE
    assert tasks.get_task(working.task_id).status is TaskStatus.IN_PROGRESS
    assert tasks.get_task(future.task_id).status is TaskStatus.PENDING
    assert tasks.get_task(undated.task_id).status is TaskStatus.PENDING
    # Overdue tasks can still be completed, and rescheduling to the future makes them pending again.
    assert tasks.update_task(late.task_id, due_at=NOW + timedelta(days=1)).status is TaskStatus.PENDING


# ---- queries and ordering ----------------------------------------------------------------------


def test_default_order_is_overdue_then_due_date_then_priority_then_creation(session_factory):
    tasks, _, _, clock = make_services(session_factory)
    made = {}
    for name, due, prio in [
        ("undated-high", None, TaskPriority.HIGH),
        ("future-late", NOW + timedelta(days=3), TaskPriority.LOW),
        ("future-soon-low", NOW + timedelta(days=1), TaskPriority.LOW),
        ("future-soon-high", NOW + timedelta(days=1), TaskPriority.HIGH),
        ("overdue-recent", NOW - timedelta(hours=1), TaskPriority.LOW),
        ("overdue-old", NOW - timedelta(days=2), TaskPriority.LOW),
    ]:
        made[name] = tasks.create_task(name, due_at=due, priority=prio)
        clock.advance(seconds=1)
    clock.now = NOW
    assert [t.title for t in tasks.list_tasks()] == [
        "overdue-old", "overdue-recent",          # overdue first, oldest due date first
        "future-soon-high", "future-soon-low",    # then by due date, ties by priority (highest first)
        "future-late",
        "undated-high",                           # tasks without a due date last
    ]
    assert [t.title for t in tasks.list_tasks()] == [t.title for t in tasks.list_tasks()]  # deterministic


def test_todays_overdue_upcoming_and_incomplete_queries(session_factory):
    tasks, _, _, clock = make_services(session_factory)
    tasks.create_task("yesterday", due_at=ist(2030, 3, 3, 10))
    tasks.create_task("this morning", due_at=ist(2030, 3, 4, 8))          # earlier today: overdue AND due today
    tasks.create_task("this evening", due_at=ist(2030, 3, 4, 20))
    tasks.create_task("tomorrow", due_at=ist(2030, 3, 5, 9))
    tasks.create_task("next month", due_at=ist(2030, 4, 5, 9))
    tasks.create_task("no date")
    done = tasks.create_task("finished", due_at=ist(2030, 3, 4, 21))
    tasks.complete_task(done.task_id)

    assert {t.title for t in tasks.tasks_due_today()} == {"this morning", "this evening"}
    assert {t.title for t in tasks.overdue_tasks()} == {"yesterday", "this morning"}
    assert {t.title for t in tasks.upcoming_tasks(days=2)} == {"this evening", "tomorrow"}  # future tasks only
    assert {t.title for t in tasks.incomplete_tasks()} == {
        "yesterday", "this morning", "this evening", "tomorrow", "next month", "no date"
    }
    assert {t.title for t in tasks.find_due_tasks()} == {"yesterday", "this morning"}
    assert [t.title for t in tasks.list_tasks(statuses={TaskStatus.COMPLETED})] == ["finished"]


def test_today_uses_the_users_timezone_not_utc(session_factory):
    tasks, _, _, clock = make_services(session_factory)
    # 22:00 UTC on Mar 4 is 03:30 on Mar 5 in Kolkata: "today" (Mar 4 in IST) must not include it.
    tasks.create_task("just after midnight in IST", due_at=datetime(2030, 3, 4, 22, 0, tzinfo=timezone.utc))
    tasks.create_task("late evening in IST", due_at=datetime(2030, 3, 4, 17, 0, tzinfo=timezone.utc))
    assert [t.title for t in tasks.tasks_due_today()] == ["late evening in IST"]


def test_list_limits_are_bounded(session_factory):
    tasks, *_ = make_services(session_factory)
    for i in range(5):
        tasks.create_task(f"t{i}")
    assert len(tasks.list_tasks(limit=2)) == 2
    assert len(tasks.list_tasks(limit=10_000)) == 5


# ---- identifying a task from words -------------------------------------------------------------


def test_matching_finds_a_unique_task_and_never_an_id(session_factory):
    tasks, *_ = make_services(session_factory)
    doc = tasks.create_task("Finish the JARVIS documentation")
    tasks.create_task("Buy groceries")
    assert [t.task_id for t in tasks.find_matching_tasks("my JARVIS documentation task")] == [doc.task_id]
    assert [t.task_id for t in tasks.find_matching_tasks("documentation")] == [doc.task_id]
    assert tasks.find_matching_tasks("the task") == []  # only filler words: matches nothing
    assert tasks.find_matching_tasks("quantum physics") == []
    assert tasks.find_matching_tasks(doc.task_id) == []  # an id is not a description


def test_ambiguous_descriptions_return_every_candidate(session_factory):
    tasks, *_ = make_services(session_factory)
    tasks.create_task("Write report for physics")
    tasks.create_task("Write report for chemistry")
    assert len(tasks.find_matching_tasks("report")) == 2  # the caller must ask, not guess
    assert len(tasks.find_matching_tasks("report for physics")) == 1


def test_an_exact_title_wins_over_longer_partial_matches(session_factory):
    tasks, *_ = make_services(session_factory)
    exact = tasks.create_task("Write report")
    tasks.create_task("Write report for physics")
    assert [t.task_id for t in tasks.find_matching_tasks("write report")] == [exact.task_id]


def test_completed_tasks_are_not_matched_unless_asked(session_factory):
    tasks, *_ = make_services(session_factory)
    task = tasks.create_task("Write report")
    tasks.complete_task(task.task_id)
    assert tasks.find_matching_tasks("report") == []
    assert len(tasks.find_matching_tasks("report", include_closed=True)) == 1


# ---- transactions and persistence --------------------------------------------------------------


def test_task_and_reminder_are_created_together(session_factory):
    tasks, reminders, *_ = make_services(session_factory)
    task, reminder = tasks.create_task_with_reminder("Submit report", ist(2030, 3, 5, 9), due_at=ist(2030, 3, 5, 9))
    assert reminder.task_id == task.task_id and reminder.message == "Submit report"
    assert [r.reminder_id for r in reminders.list_reminders(task_id=task.task_id)] == [reminder.reminder_id]


def test_failed_reminder_leaves_no_orphan_task(session_factory):
    tasks, reminders, *_ = make_services(session_factory)
    with pytest.raises(TaskValidationError):  # a reminder in the past is invalid
        tasks.create_task_with_reminder("Submit report", NOW - timedelta(days=1))
    assert tasks.list_tasks() == [] and reminders.list_reminders() == []


def test_database_failure_mid_transaction_rolls_everything_back(session_factory, monkeypatch):
    tasks, reminders, repo, _ = make_services(session_factory)

    def boom(_reminder):
        raise TaskStorageError("Task database error (OperationalError)")

    monkeypatch.setattr(repo, "add_reminder", boom)
    with pytest.raises(TaskStorageError):
        tasks.create_task_with_reminder("Submit report", ist(2030, 3, 5, 9))
    monkeypatch.undo()
    assert tasks.list_tasks() == []  # the task inserted before the failure was rolled back


def test_completing_a_task_cancels_its_reminders_atomically(session_factory):
    tasks, reminders, *_ = make_services(session_factory)
    task, reminder = tasks.create_task_with_reminder("Submit report", ist(2030, 3, 5, 9))
    other = reminders.create_reminder("unrelated", ist(2030, 3, 5, 10))
    tasks.complete_task(task.task_id)
    assert reminders.get_reminder(reminder.reminder_id).status is ReminderStatus.CANCELLED
    assert reminders.get_reminder(other.reminder_id).status is ReminderStatus.SCHEDULED


def test_deleting_a_task_removes_its_reminders(session_factory):
    tasks, reminders, *_ = make_services(session_factory)
    task, reminder = tasks.create_task_with_reminder("Submit report", ist(2030, 3, 5, 9))
    tasks.delete_task(task.task_id)
    with pytest.raises(TaskNotFound):
        tasks.get_task(task.task_id)
    with pytest.raises(TaskNotFound):
        reminders.get_reminder(reminder.reminder_id)
    with pytest.raises(TaskNotFound):
        tasks.delete_task(task.task_id)


def test_foreign_key_rejects_a_reminder_for_a_missing_task(session_factory):
    _, reminders, *_ = make_services(session_factory)
    with pytest.raises(TaskNotFound):
        reminders.create_reminder("x", ist(2030, 3, 5, 9), task_id="missing")


def test_tasks_and_reminders_survive_a_restart(tmp_path):
    """A file database and brand-new engines/services stand in for restarting JARVIS."""
    url = f"sqlite:///{tmp_path / 'restart.db'}"
    engine = create_engine(url)
    Base.metadata.create_all(engine)
    tasks, reminders, *_ = make_services(sessionmaker(bind=engine, expire_on_commit=False))
    task = tasks.create_task("Finish docs", due_at=ist(2030, 3, 6, 9))
    reminder = reminders.create_reminder("Call Mom", ist(2030, 3, 5, 9), recurrence=None)
    recurring = reminders.create_reminder(
        "Weekly goals", recurrence=Recurrence(frequency=Frequency.WEEKLY, hour=8, weekdays=(0,))
    )
    engine.dispose()

    engine2 = create_engine(url)  # "restart"
    tasks2, reminders2, *_ = make_services(sessionmaker(bind=engine2, expire_on_commit=False))
    assert tasks2.get_task(task.task_id) == task
    assert reminders2.get_reminder(reminder.reminder_id) == reminder
    assert reminders2.get_reminder(recurring.reminder_id).recurrence == recurring.recurrence
    tasks2.complete_task(task.task_id)
    reminders2.cancel_reminder(reminder.reminder_id)
    engine2.dispose()

    engine3 = create_engine(url)
    tasks3, reminders3, *_ = make_services(sessionmaker(bind=engine3, expire_on_commit=False))
    assert tasks3.get_task(task.task_id).status is TaskStatus.COMPLETED
    assert reminders3.get_reminder(reminder.reminder_id).status is ReminderStatus.CANCELLED
    assert reminders3.get_reminder(recurring.reminder_id).status is ReminderStatus.SCHEDULED
    engine3.dispose()


def test_timestamps_are_stored_as_utc_in_the_database(session_factory):
    tasks, reminders, *_ = make_services(session_factory)
    task = tasks.create_task("x", due_at=ist(2030, 3, 5, 9))
    reminders.create_reminder("y", ist(2030, 3, 5, 9))
    with session_factory() as session:
        stored = session.scalars(select(TaskRow)).one().due_at
        stored_reminder = session.scalars(select(ReminderRow)).one()
    assert stored.replace(tzinfo=timezone.utc) == datetime(2030, 3, 5, 3, 30, tzinfo=timezone.utc)
    assert stored_reminder.timezone == "Asia/Kolkata"  # the user's zone is recorded; the instant is UTC


def test_repository_users_on_two_threads_do_not_share_a_transaction(tmp_path):
    url = f"sqlite:///{tmp_path / 'threads.db'}"
    engine = create_engine(url, connect_args={"timeout": 30})
    Base.metadata.create_all(engine)
    tasks, reminders, repo, _ = make_services(sessionmaker(bind=engine, expire_on_commit=False))
    reminder = reminders.create_reminder("Call Mom", NOW + timedelta(seconds=1))
    results, errors = [], []

    def claim():
        try:
            results.append(reminders.claim_delivery(reminder.reminder_id, NOW + timedelta(seconds=2)))
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=claim) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    engine.dispose()
    assert not errors
    assert sum(r is not None for r in results) == 1  # exactly one caller wins the delivery lease
