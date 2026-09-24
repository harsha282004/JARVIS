"""REAL-infrastructure proactive tests (no mocked scheduler, engine, queue, notifier or database layer).

test_real_scheduler_thread_*   the real ReminderScheduler thread runs the real ProactiveEngine over a real SQLite file,
                               delivering to the real tray-style DesktopNotifier and the real AnnouncementQueue (the voice
                               hand-off), with real start, stop and restart. Only the tray's `send` callable is a recorder.
test_real_windows_tray_*       shows a REAL Windows tray notification through pystray. It pops a balloon, so it runs only
                               when JARVIS_REAL_TRAY_TEST=1 on Windows. "Accepted" means the tray icon took the notification
                               without error; nobody has to look at the screen.

Not covered here: real speech. The VoiceEngine needs audio hardware and downloaded models; what is verified is that the
announcement is queued for it (its own tests cover speaking between conversations).
"""

import os
import sys
import threading
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from agent.proactive.engine import ProactiveEngine
from agent.proactive.models import Channel
from agent.proactive.policy import NotificationPolicy, PolicyConfig
from agent.proactive.repository import NotificationRepository
from agent.proactive.sources import TaskSignalSource
from agent.tasks.models import utcnow
from agent.tasks.notifications import AnnouncementQueue, DesktopNotifier, VoiceNotifier
from agent.tasks.repository import TaskRepository
from agent.tasks.scheduler import ReminderScheduler
from agent.tasks.service import TaskService
from backend.models.base import Base
from tests.task_helpers import IST

pytestmark = pytest.mark.integration

ALWAYS = PolicyConfig(enabled=True, quiet_hours_enabled=False, cooldown_minutes=60, lookahead_minutes=1440, max_per_hour=50)


def build(tmp_path, send):
    engine_db = create_engine(f"sqlite:///{tmp_path / 'real.db'}", connect_args={"check_same_thread": False, "timeout": 30})
    Base.metadata.create_all(engine_db)
    factory = sessionmaker(bind=engine_db, expire_on_commit=False)
    tasks = TaskService(TaskRepository(factory), zone=IST)
    queue = AnnouncementQueue()
    queue.set_accepting(True)  # what a running VoiceEngine does
    notifiers = {Channel.DESKTOP: DesktopNotifier(send, title="JARVIS"), Channel.VOICE: VoiceNotifier(queue)}
    proactive = ProactiveEngine([TaskSignalSource(tasks, 1440)], NotificationPolicy(ALWAYS, IST), NotificationRepository(factory), notifiers, IST, interval_seconds=0)
    return engine_db, tasks, queue, proactive


def test_real_scheduler_thread_runs_the_engine_delivers_once_and_shuts_down(tmp_path):
    sent = []
    db, tasks, queue, proactive = build(tmp_path, lambda title, text: sent.append((title, text)))
    tasks.create_task("Submit internship application", due_at=utcnow() + timedelta(minutes=40))
    polls = threading.Semaphore(0)
    scheduler = ReminderScheduler(None, None, poll_seconds=0.05, extra_passes=[lambda: (proactive.run_once(), polls.release())])
    threads_before = {t.name for t in threading.enumerate()}
    assert scheduler.start() is True
    for _ in range(6):  # several real polls
        assert polls.acquire(timeout=10)
    assert scheduler.is_running and scheduler.stop(10) is True
    assert len(sent) == 1 and sent[0][0] == "JARVIS" and sent[0][1].startswith("Your task 'Submit internship application' is due in about")
    assert len(queue) == 1  # the voice notification is queued for the VoiceEngine (which speaks between conversations)
    assert {t.name for t in threading.enumerate()} - threads_before == set()  # a clean shutdown: no thread left behind
    db.dispose()


def test_real_duplicate_suppression_survives_a_restart(tmp_path):
    sent = []
    db, tasks, queue, first = build(tmp_path, lambda title, text: sent.append(text))
    tasks.create_task("Pay the fee", due_at=utcnow() + timedelta(minutes=30))
    assert first.run_once().delivered == 1
    factory = sessionmaker(bind=db, expire_on_commit=False)
    notifiers = {Channel.DESKTOP: DesktopNotifier(lambda title, text: sent.append(text), title="JARVIS")}
    restarted = ProactiveEngine([TaskSignalSource(TaskService(TaskRepository(factory), zone=IST), 1440)], NotificationPolicy(ALWAYS, IST),
                                NotificationRepository(factory), notifiers, IST, interval_seconds=0)  # JARVIS restarted: a brand new engine
    report = restarted.run_once()
    assert report.delivered == 0 and report.suppressed["already notified about this"] == 1 and len(sent) == 1
    db.dispose()


@pytest.mark.skipif(sys.platform != "win32" or os.environ.get("JARVIS_REAL_TRAY_TEST") != "1", reason="set JARVIS_REAL_TRAY_TEST=1 on Windows to show a real tray notification")
def test_real_windows_tray_accepts_a_proactive_notification(tmp_path):
    from desktop.runtime.manager import RuntimeManager
    from desktop.tray.tray import TrayController

    manager = RuntimeManager(lambda: (_ for _ in ()).throw(RuntimeError("the voice engine is not started by this test")))
    tray = TrayController(manager, on_exit=lambda: None)
    tray.start()  # a real pystray icon
    try:
        db, tasks, queue, proactive = build(tmp_path, tray.notify)  # the engine notifies through the real tray
        tasks.create_task("JARVIS proactive tray self-test", due_at=utcnow() + timedelta(minutes=40))
        first = proactive.run_once()
        assert (first.delivered, first.failed) == (1, 0)  # the tray accepted it (a failure would raise inside notify and count as failed)
        assert proactive.run_once().delivered == 0  # and the same signal is not shown again
        db.dispose()
    finally:
        tray.stop()
