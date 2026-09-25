"""ReminderService, recurrence, missed reminders, notifications and the scheduler."""

import threading
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from agent.tasks.models import (
    Frequency,
    InvalidTransition,
    MissedPolicy,
    Recurrence,
    ReminderStatus,
    TaskNotFound,
    TaskStatus,
    TaskStorageError,
    TaskValidationError,
)
from agent.tasks.notifications import (
    AnnouncementQueue,
    CompositeNotifier,
    DesktopNotifier,
    NotificationError,
    NotificationService,
    VoiceNotifier,
    clean_notification_text,
)
from agent.tasks.recurrence import next_occurrence
from agent.tasks.repository import TaskRepository
from agent.tasks.scheduler import ReminderScheduler
from agent.tasks.service import ReminderService
from tests.task_helpers import IST, Clock, ist, make_services

NOW = datetime(2030, 3, 4, 9, 0, tzinfo=timezone.utc)  # Monday 14:30 IST


class RecordingNotifier(NotificationService):
    def __init__(self, fail_times=0):
        self.sent = []
        self.fail_times = fail_times

    def notify(self, message, metadata=None):
        if self.fail_times > 0:
            self.fail_times -= 1
            raise NotificationError("channel down")
        self.sent.append((message, dict(metadata or {})))


# ---- reminder service -------------------------------------------------------------------------------


def test_create_and_get_reminder(session_factory):
    _, reminders, _, clock = make_services(session_factory)
    made = reminders.create_reminder("Call Mom", clock() + timedelta(minutes=30), session_id="s1", source="conversation")
    fetched = reminders.get_reminder(made.reminder_id)
    assert fetched == made
    assert fetched.status is ReminderStatus.SCHEDULED and fetched.timezone == "Asia/Kolkata"
    assert fetched.scheduled_at == NOW + timedelta(minutes=30) and fetched.task_id is None
    with pytest.raises(TaskNotFound):
        reminders.get_reminder("nope")


def test_reminder_input_validation(session_factory):
    _, reminders, *_ = make_services(session_factory)
    with pytest.raises(TaskValidationError):
        reminders.create_reminder("x", NOW - timedelta(hours=1))  # in the past
    with pytest.raises(TaskValidationError):
        reminders.create_reminder("x", datetime(2030, 3, 5, 9))  # naive
    with pytest.raises(TaskValidationError):
        reminders.create_reminder("x")  # no time and no recurrence
    with pytest.raises(TaskValidationError):
        reminders.create_reminder("   ", NOW + timedelta(hours=1))
    assert reminders.list_reminders() == []


def test_reminder_for_a_completed_task_is_refused(session_factory):
    tasks, reminders, *_ = make_services(session_factory)
    task = tasks.create_task("report")
    tasks.complete_task(task.task_id)
    with pytest.raises(InvalidTransition):
        reminders.create_reminder("x", NOW + timedelta(hours=1), task_id=task.task_id)


def test_cancel_reminder(session_factory):
    _, reminders, _, clock = make_services(session_factory)
    r = reminders.create_reminder("Call Mom", NOW + timedelta(hours=1))
    cancelled = reminders.cancel_reminder(r.reminder_id)
    assert cancelled.status is ReminderStatus.CANCELLED and cancelled.cancelled_at == clock()
    with pytest.raises(InvalidTransition):
        reminders.cancel_reminder(r.reminder_id)
    clock.advance(hours=2)
    assert reminders.find_due_reminders() == []  # a cancelled reminder never becomes due


def test_listing_and_next_reminder(session_factory):
    _, reminders, *_ = make_services(session_factory)
    reminders.create_reminder("later today", ist(2030, 3, 4, 20))
    reminders.create_reminder("tomorrow", ist(2030, 3, 5, 9))
    reminders.create_reminder("soon", ist(2030, 3, 4, 15))
    reminders.create_reminder("next week", ist(2030, 3, 12, 9))
    assert [r.message for r in reminders.upcoming_reminders()] == ["soon", "later today", "tomorrow", "next week"]
    assert reminders.next_reminder().message == "soon"
    assert [r.message for r in reminders.reminders_on_day(0)] == ["soon", "later today"]
    assert [r.message for r in reminders.reminders_on_day(1)] == ["tomorrow"]
    assert [r.message for r in reminders.find_matching_reminders("tomorrow")] == ["tomorrow"]


# ---- triggering -------------------------------------------------------------------------------------


def test_find_due_reminders_only_returns_due_ones(session_factory):
    _, reminders, _, clock = make_services(session_factory)
    due = reminders.create_reminder("due", NOW + timedelta(minutes=10))
    reminders.create_reminder("later", NOW + timedelta(hours=5))
    assert reminders.find_due_reminders() == []
    clock.advance(minutes=11)
    assert [r.reminder_id for r in reminders.find_due_reminders()] == [due.reminder_id]


def test_mark_triggered_triggers_exactly_once(session_factory):
    _, reminders, _, clock = make_services(session_factory)
    r = reminders.create_reminder("Call Mom", NOW + timedelta(minutes=10))
    assert reminders.mark_triggered(r.reminder_id) is None  # not due yet
    clock.advance(minutes=11)
    first = reminders.mark_triggered(r.reminder_id)
    assert first is not None and first.status is ReminderStatus.TRIGGERED
    assert first.triggered_at == clock() and first.occurrences == 1 and first.claimed_at is None
    assert reminders.mark_triggered(r.reminder_id) is None  # checking again never triggers it again
    assert reminders.find_due_reminders() == []


def test_a_delivery_lease_blocks_a_second_claim_until_it_expires(session_factory):
    _, reminders, _, clock = make_services(session_factory, lease_seconds=60)
    r = reminders.create_reminder("Call Mom", NOW + timedelta(minutes=1))
    clock.advance(minutes=2)
    claimed = reminders.claim_delivery(r.reminder_id)
    assert claimed is not None
    assert reminders.claim_delivery(r.reminder_id) is None
    assert reminders.find_due_reminders() == []  # leased: invisible to other schedulers
    clock.advance(seconds=61)
    assert [x.reminder_id for x in reminders.find_due_reminders()] == [r.reminder_id]  # crashed deliverer: retry


def test_a_stale_claim_cannot_complete_a_reminder_someone_else_owns(session_factory):
    _, reminders, _, clock = make_services(session_factory, lease_seconds=60)
    r = reminders.create_reminder("Call Mom", NOW + timedelta(minutes=1))
    clock.advance(minutes=2)
    stale = reminders.claim_delivery(r.reminder_id)
    clock.advance(minutes=5)
    fresh = reminders.claim_delivery(r.reminder_id)  # the lease expired, a second deliverer took over
    assert fresh is not None and fresh.claimed_at != stale.claimed_at
    assert reminders.complete_delivery(stale) is None  # the old claim is refused
    assert reminders.complete_delivery(fresh).status is ReminderStatus.TRIGGERED


def test_cancelling_during_delivery_wins(session_factory):
    _, reminders, _, clock = make_services(session_factory)
    r = reminders.create_reminder("Call Mom", NOW + timedelta(minutes=1))
    clock.advance(minutes=2)
    claimed = reminders.claim_delivery(r.reminder_id)
    reminders.cancel_reminder(r.reminder_id)
    assert reminders.complete_delivery(claimed) is None
    assert reminders.get_reminder(r.reminder_id).status is ReminderStatus.CANCELLED


# ---- recurrence -------------------------------------------------------------------------------------


def test_daily_recurrence_next_occurrence():
    rec = Recurrence(frequency=Frequency.DAILY, hour=8)
    after = ist(2030, 3, 4, 7, 59)
    assert next_occurrence(rec, after, IST) == ist(2030, 3, 4, 8).astimezone(timezone.utc)
    assert next_occurrence(rec, ist(2030, 3, 4, 8), IST) == ist(2030, 3, 5, 8).astimezone(timezone.utc)  # strictly after


def test_weekly_recurrence_next_occurrence():
    monday_9 = Recurrence(frequency=Frequency.WEEKLY, hour=9, weekdays=(0,))
    assert next_occurrence(monday_9, ist(2030, 3, 4, 14, 30), IST) == ist(2030, 3, 11, 9).astimezone(timezone.utc)
    assert next_occurrence(monday_9, ist(2030, 3, 4, 8), IST) == ist(2030, 3, 4, 9).astimezone(timezone.utc)
    mon_fri = Recurrence(frequency=Frequency.WEEKLY, hour=18, weekdays=(0, 4))
    assert next_occurrence(mon_fri, ist(2030, 3, 4, 19), IST) == ist(2030, 3, 8, 18).astimezone(timezone.utc)


def test_monthly_recurrence_clamps_to_short_months():
    on_31 = Recurrence(frequency=Frequency.MONTHLY, hour=10, day_of_month=31)
    assert next_occurrence(on_31, ist(2030, 1, 31, 11), IST) == ist(2030, 2, 28, 10).astimezone(timezone.utc)
    assert next_occurrence(on_31, ist(2030, 2, 28, 11), IST) == ist(2030, 3, 31, 10).astimezone(timezone.utc)
    first = Recurrence(frequency=Frequency.MONTHLY, hour=10, day_of_month=1)
    assert next_occurrence(first, ist(2030, 12, 15), IST) == ist(2031, 1, 1, 10).astimezone(timezone.utc)


def test_recurrence_keeps_wall_clock_time_across_daylight_saving():
    new_york = ZoneInfo("America/New_York")  # clocks go forward on 2030-03-10
    rec = Recurrence(frequency=Frequency.DAILY, hour=8)
    before = next_occurrence(rec, datetime(2030, 3, 9, 7, 0, tzinfo=new_york), new_york)  # 8 AM on Mar 9 (EST)
    after = next_occurrence(rec, before, new_york)
    assert before.astimezone(new_york).hour == 8 and after.astimezone(new_york).hour == 8
    assert after - before == timedelta(hours=23)  # 8 AM both days, but the day was 23 hours long


def test_recurring_reminder_is_one_row_that_moves_to_its_next_occurrence(session_factory):
    _, reminders, repo, clock = make_services(session_factory)
    rec = Recurrence(frequency=Frequency.WEEKLY, hour=8, weekdays=(0,))  # every Monday 8 AM IST
    r = reminders.create_reminder("Review weekly goals", recurrence=rec)
    assert r.scheduled_at == ist(2030, 3, 11, 8).astimezone(timezone.utc)  # today's 8 AM has passed

    clock.now = r.scheduled_at + timedelta(seconds=5)
    triggered = reminders.mark_triggered(r.reminder_id)
    assert triggered.status is ReminderStatus.SCHEDULED  # still active
    assert triggered.scheduled_at == ist(2030, 3, 18, 8).astimezone(timezone.utc)
    assert triggered.occurrences == 1 and triggered.triggered_at == clock()
    assert reminders.mark_triggered(r.reminder_id) is None  # not due again until next Monday
    assert len(reminders.list_reminders()) == 1  # no queue of future rows


def test_cancelling_a_recurring_reminder_stops_future_occurrences(session_factory):
    _, reminders, _, clock = make_services(session_factory)
    r = reminders.create_reminder("Standup", recurrence=Recurrence(frequency=Frequency.DAILY, hour=8))
    reminders.cancel_reminder(r.reminder_id)
    clock.advance(days=30)
    assert reminders.find_due_reminders() == []
    assert reminders.mark_triggered(r.reminder_id) is None
    assert reminders.get_reminder(r.reminder_id).status is ReminderStatus.CANCELLED


def test_recurring_reminder_skips_occurrences_missed_while_off(session_factory):
    _, reminders, _, clock = make_services(session_factory)
    r = reminders.create_reminder("Daily", recurrence=Recurrence(frequency=Frequency.DAILY, hour=8))
    clock.now = r.scheduled_at + timedelta(days=10, hours=1)  # JARVIS was off for ten days
    after = reminders.mark_triggered(r.reminder_id)
    assert after.scheduled_at > clock() and after.occurrences == 1  # one delivery, then the next FUTURE slot
    assert after.scheduled_at - clock() <= timedelta(days=1)


# ---- notifications ----------------------------------------------------------------------------------


def test_clean_notification_text():
    assert clean_notification_text("a\x00b\n\tc") == "a b c"
    assert len(clean_notification_text("x" * 1000)) == 250


def test_desktop_notifier_sends_title_and_cleaned_text():
    sent = []
    DesktopNotifier(lambda title, text: sent.append((title, text))).notify("Reminder:\ncall Mom", {"reminder_id": "1"})
    assert sent == [("JARVIS reminder", "Reminder: call Mom")]


def test_desktop_notifier_failure_is_reported_without_content():
    def broken(title, text):
        raise OSError("balloon failed: secret-text")

    with pytest.raises(NotificationError) as exc:
        DesktopNotifier(broken).notify("secret-text")
    assert "secret-text" not in str(exc.value)
    with pytest.raises(NotificationError):
        DesktopNotifier(lambda *_: None).notify("   ")


def test_voice_notifier_needs_a_running_voice_engine_and_is_bounded():
    queue = AnnouncementQueue(max_size=2)
    notifier = VoiceNotifier(queue)
    with pytest.raises(NotificationError):
        notifier.notify("Reminder: x")  # engine not running: nothing was delivered
    queue.set_accepting(True)
    notifier.notify("Reminder: one")
    notifier.notify("Reminder: two")
    with pytest.raises(NotificationError):
        notifier.notify("Reminder: three")  # queue full
    assert [queue.get_nowait(), queue.get_nowait(), queue.get_nowait()] == ["Reminder: one", "Reminder: two", None]


def test_composite_notifier_succeeds_if_any_channel_delivers():
    good = RecordingNotifier()
    CompositeNotifier([("bad", RecordingNotifier(fail_times=1)), ("good", good)]).notify("m")
    assert len(good.sent) == 1
    with pytest.raises(NotificationError):
        CompositeNotifier([("a", RecordingNotifier(fail_times=1)), ("b", RecordingNotifier(fail_times=1))]).notify("m")


# ---- scheduler ----------------------------------------------------------------------------------------


def scheduler_for(session_factory, notifier=None, policy=MissedPolicy.NOTIFY, **kw):
    tasks, reminders, repo, clock = make_services(session_factory)
    notifier = notifier or RecordingNotifier()
    sched = ReminderScheduler(reminders, notifier, tasks=tasks, poll_seconds=0.05, missed_policy=policy, clock=clock, **kw)
    return sched, tasks, reminders, notifier, clock


def test_scheduler_delivers_a_due_reminder_once(session_factory):
    sched, _, reminders, notifier, clock = scheduler_for(session_factory)
    r = reminders.create_reminder("submit my assignment", NOW + timedelta(minutes=1))
    assert sched.run_once() == 0  # not due yet
    clock.advance(minutes=1, seconds=5)
    assert sched.run_once() == 1
    assert sched.run_once() == 0 and sched.run_once() == 0  # checking repeatedly never re-triggers it
    assert notifier.sent == [("Reminder: submit my assignment", {"reminder_id": r.reminder_id, "task_id": None, "missed": False, "priority": "high"})]
    assert reminders.get_reminder(r.reminder_id).status is ReminderStatus.TRIGGERED


def test_two_schedulers_deliver_a_reminder_only_once(session_factory):
    a, _, reminders, notifier, clock = scheduler_for(session_factory)
    b = ReminderScheduler(reminders, notifier, poll_seconds=0.05, clock=clock)
    reminders.create_reminder("x", NOW + timedelta(minutes=1))
    clock.advance(minutes=1, seconds=5)
    assert a.run_once() + b.run_once() == 1
    assert len(notifier.sent) == 1


def test_failed_notification_is_not_recorded_as_delivered_and_is_retried(session_factory):
    sched, _, reminders, notifier, clock = scheduler_for(session_factory, notifier=RecordingNotifier(fail_times=2))
    r = reminders.create_reminder("x", NOW + timedelta(minutes=1))
    clock.advance(minutes=1, seconds=5)
    assert sched.run_once() == 0
    failed = reminders.get_reminder(r.reminder_id)
    assert failed.status is ReminderStatus.SCHEDULED and failed.triggered_at is None and failed.delivery_attempts == 1
    assert sched.run_once() == 0  # still failing
    assert sched.run_once() == 1  # the channel recovered
    delivered = reminders.get_reminder(r.reminder_id)
    assert delivered.status is ReminderStatus.TRIGGERED and delivered.occurrences == 1 and delivered.delivery_attempts == 0


def test_permanently_failing_delivery_expires_instead_of_pretending(session_factory):
    sched, _, reminders, notifier, clock = scheduler_for(session_factory, notifier=RecordingNotifier(fail_times=10_000))
    r = reminders.create_reminder("x", NOW + timedelta(minutes=1))
    clock.advance(minutes=1, seconds=5)
    for _ in range(25):
        sched.run_once()
    final = reminders.get_reminder(r.reminder_id)
    assert final.status is ReminderStatus.EXPIRED and final.triggered_at is None and final.occurrences == 0
    assert notifier.sent == []


def test_missed_reminder_is_delivered_late_and_marked_when_policy_is_notify(session_factory):
    sched, _, reminders, notifier, clock = scheduler_for(session_factory, policy=MissedPolicy.NOTIFY)
    reminders.create_reminder("submit my assignment", NOW + timedelta(minutes=10))
    clock.advance(hours=5)  # JARVIS was not running when it came due
    assert sched.run_once() == 1
    message, metadata = notifier.sent[0]
    assert message.startswith("Missed reminder from today at 2:40 PM: submit my assignment")
    assert metadata["missed"] is True  # never presented as an on-time delivery


def test_missed_reminder_expires_without_notifying_when_policy_is_expire(session_factory):
    sched, _, reminders, notifier, clock = scheduler_for(session_factory, policy=MissedPolicy.EXPIRE)
    r = reminders.create_reminder("submit my assignment", NOW + timedelta(minutes=10))
    on_time = reminders.create_reminder("still on time", NOW + timedelta(hours=5))
    clock.advance(hours=5, seconds=30)
    assert sched.run_once() == 1  # only the reminder that is not stale is delivered
    assert reminders.get_reminder(r.reminder_id).status is ReminderStatus.EXPIRED
    assert [m for m, _ in notifier.sent] == ["Reminder: still on time"]
    assert reminders.get_reminder(on_time.reminder_id).status is ReminderStatus.TRIGGERED


def test_missed_recurring_reminder_is_delivered_once_then_moves_on(session_factory):
    sched, _, reminders, notifier, clock = scheduler_for(session_factory)
    r = reminders.create_reminder("Daily standup", recurrence=Recurrence(frequency=Frequency.DAILY, hour=8))
    clock.now = r.scheduled_at + timedelta(days=3, hours=2)
    assert sched.run_once() == 1 and sched.run_once() == 0
    assert len(notifier.sent) == 1  # not one message per missed day
    assert reminders.get_reminder(r.reminder_id).scheduled_at > clock()


def test_scheduler_marks_overdue_tasks(session_factory):
    sched, tasks, _, _, clock = scheduler_for(session_factory)
    task = tasks.create_task("report", due_at=NOW + timedelta(hours=1))
    clock.advance(hours=2)
    sched.run_once()
    assert tasks.get_task(task.task_id).status is TaskStatus.OVERDUE


def test_scheduler_lifecycle_start_stop_and_no_second_thread(session_factory):
    sched, _, reminders, notifier, _ = scheduler_for(session_factory)
    before = threading.active_count()
    assert sched.start() is True and sched.is_running
    assert sched.start() is False  # never a second worker thread
    assert threading.active_count() == before + 1
    assert sched.stop(timeout=5) is True and not sched.is_running
    assert threading.active_count() == before
    assert sched.stop() is True  # stopping twice is harmless
    assert sched.start() is True  # and it can be started again
    sched.stop()


def test_scheduler_thread_delivers_in_the_background_and_stops_promptly(session_factory):
    # A real clock and a real thread; the reminder is already due, so the immediate first tick delivers it.
    notifier = RecordingNotifier()
    real_now = lambda: datetime.now(timezone.utc)  # noqa: E731
    reminders = ReminderService(TaskRepository(session_factory), zone=IST, clock=real_now)
    reminders.create_reminder("background", real_now() + timedelta(milliseconds=50))
    sched = ReminderScheduler(reminders, notifier, poll_seconds=0.05, clock=real_now)
    sched.start()
    deadline = time.monotonic() + 5
    while not notifier.sent and time.monotonic() < deadline:
        time.sleep(0.02)
    started = time.monotonic()
    assert sched.stop(timeout=5) is True
    assert time.monotonic() - started < 2  # shutdown does not wait for a full poll interval
    assert [m for m, _ in notifier.sent] == ["Reminder: background"]


def test_scheduler_survives_database_failures_and_recovers(session_factory):
    sched, _, reminders, notifier, clock = scheduler_for(session_factory)
    r = reminders.create_reminder("x", NOW + timedelta(minutes=1))
    clock.advance(minutes=2)
    real = reminders.find_due_reminders
    calls = {"n": 0}

    def flaky(now, limit):
        calls["n"] += 1
        if calls["n"] <= 2:
            raise TaskStorageError("Task database error (OperationalError)")
        return real(now, limit)

    reminders.find_due_reminders = flaky
    with pytest.raises(TaskStorageError):
        sched.run_once()  # run_once reports the failure to its caller ...
    sched.start()  # ... and the thread loop absorbs it, backs off and keeps going
    deadline = time.monotonic() + 10
    while not notifier.sent and time.monotonic() < deadline:
        time.sleep(0.02)
    assert sched.is_running
    assert sched.stop(timeout=5) is True
    assert len(notifier.sent) == 1 and calls["n"] >= 3
    assert reminders.get_reminder(r.reminder_id).status is ReminderStatus.TRIGGERED


def test_scheduler_rejects_a_nonpositive_poll_interval(session_factory):
    _, reminders, *_ = make_services(session_factory)
    with pytest.raises(ValueError):
        ReminderScheduler(reminders, RecordingNotifier(), poll_seconds=0)


def test_scheduler_logs_ids_not_reminder_content(session_factory, caplog):
    import logging

    sched, _, reminders, notifier, clock = scheduler_for(session_factory, notifier=RecordingNotifier(fail_times=1))
    reminders.create_reminder("my-very-private-reminder-text", NOW + timedelta(minutes=1))
    clock.advance(minutes=2)
    with caplog.at_level(logging.DEBUG):
        sched.run_once()
        sched.run_once()
    assert "my-very-private-reminder-text" not in caplog.text
    assert "reminder=" in caplog.text
