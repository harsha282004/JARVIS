"""ReminderScheduler: one background thread that delivers due reminders.

    Windows runtime -> ReminderScheduler -> ReminderService -> database
                                         -> NotificationService

A single daemon thread wakes every `poll_seconds`. Each due reminder is claimed
through an atomic lease (so a second check, or a second scheduler, cannot deliver
it again), delivered, and only then recorded as triggered. If delivery fails it is
not recorded and is retried on later polls. The first tick runs immediately at start,
which is how reminders that came due while JARVIS was not running are handled
(missed-reminder policy). The scheduler never touches the VoiceEngine or audio;
it only calls the notification abstraction.
"""

import threading
from collections.abc import Callable
from datetime import datetime, timedelta

from agent.tasks.formatting import format_when
from agent.tasks.models import MissedPolicy, Reminder, utcnow
from agent.tasks.notifications import NotificationService
from agent.tasks.service import ReminderService, TaskService
from backend.core.logging import get_logger

logger = get_logger(__name__)

# A reminder delivered later than this after its scheduled time counts as "missed".
DEFAULT_MISSED_GRACE_SECONDS = 120.0
MAX_BACKOFF_SECONDS = 300.0
BATCH_SIZE = 50


class ReminderScheduler:
    def __init__(
        self,
        reminders: ReminderService,
        notifier: NotificationService,
        *,
        tasks: TaskService | None = None,
        poll_seconds: float = 15.0,
        missed_policy: MissedPolicy = MissedPolicy.NOTIFY,
        missed_grace_seconds: float = DEFAULT_MISSED_GRACE_SECONDS,
        clock: Callable[[], datetime] = utcnow,
    ):
        if poll_seconds <= 0:
            raise ValueError("poll_seconds must be positive")
        self._reminders = reminders
        self._notifier = notifier
        self._tasks = tasks
        self._poll = poll_seconds
        self._policy = missed_policy
        self._grace = timedelta(seconds=missed_grace_seconds)
        self._clock = clock
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._failures = 0

    @property
    def is_running(self) -> bool:
        thread = self._thread
        return thread is not None and thread.is_alive()

    def start(self) -> bool:
        """Start the scheduler thread. False if it is already running (never a second thread)."""
        with self._lock:
            if self.is_running:
                return False
            self._stop = threading.Event()
            self._thread = threading.Thread(target=self._loop, args=(self._stop,), name="jarvis-reminders", daemon=True)
            self._thread.start()
        logger.info("Reminder scheduler started (poll=%ss, missed_policy=%s)", self._poll, self._policy.value)
        return True

    def stop(self, timeout: float = 10.0) -> bool:
        """Stop the thread and wait for it. True if it is stopped (or was never started)."""
        with self._lock:
            thread, self._thread = self._thread, None
            self._stop.set()
        if thread is None:
            return True
        thread.join(timeout)
        stopped = not thread.is_alive()
        if stopped:
            logger.info("Reminder scheduler stopped")
        else:
            logger.error("Reminder scheduler did not stop within %ss", timeout)
        return stopped

    def _loop(self, stop: threading.Event) -> None:
        delay = 0.0  # the first tick is immediate: it recovers reminders missed while JARVIS was off
        while not stop.wait(delay):
            try:
                self.run_once()
                self._failures = 0
                delay = self._poll
            except Exception as exc:  # noqa: BLE001 - the scheduler must outlive a database or notifier failure
                self._failures += 1
                delay = min(self._poll * 2**self._failures, MAX_BACKOFF_SECONDS)
                logger.error("Scheduler error (%s); retrying in %.0fs", type(exc).__name__, delay)

    def run_once(self) -> int:
        """One scheduling pass (also called directly by tests). Returns how many reminders were delivered.
        Raises on a database failure so the loop can back off."""
        now = self._clock()
        if self._tasks is not None:
            self._tasks.mark_overdue(now)
        delivered = 0
        for due in self._reminders.find_due_reminders(now, BATCH_SIZE):
            if self._stop.is_set():
                break
            claimed = self._reminders.claim_delivery(due.reminder_id, now)
            if claimed is None:
                continue  # another caller took it, or it was cancelled
            delivered += self._deliver(claimed, now)
        return delivered

    def _deliver(self, claimed: Reminder, now: datetime) -> int:
        missed = now - claimed.scheduled_at > self._grace
        if missed and self._policy is MissedPolicy.EXPIRE:
            self._reminders.expire_missed(claimed, now)
            logger.info("Missed reminder expired (reminder=%s)", claimed.reminder_id)
            return 0
        try:
            self._notifier.notify(self._text(claimed, missed, now), {
                "reminder_id": claimed.reminder_id, "task_id": claimed.task_id, "missed": missed,
            })
        except Exception as exc:  # noqa: BLE001 - not delivered: do NOT record a trigger
            logger.warning("Reminder delivery failed (reminder=%s, %s)", claimed.reminder_id, type(exc).__name__)
            self._reminders.fail_delivery(claimed, now)
            return 0
        if self._reminders.complete_delivery(claimed, now) is None:
            # Delivered but the record could not be updated (cancelled meanwhile, or the lease was lost).
            logger.warning("Reminder was delivered but its state changed meanwhile (reminder=%s)", claimed.reminder_id)
        return 1

    @staticmethod
    def _text(reminder: Reminder, missed: bool, now: datetime) -> str:
        if not missed:
            return f"Reminder: {reminder.message}"
        when = format_when(reminder.scheduled_at, now, reminder.zone)
        return f"Missed reminder from {when}: {reminder.message}"
