"""Wiring of the task/reminder engine into the runtime: bootstrap, VoiceEngine, launcher, tray.

Fakes only: no audio hardware, no tray icon is shown, no Ollama, and no PostgreSQL is touched.
"""

import threading
import time

import numpy as np
import pytest

from agent.tasks.notifications import AnnouncementQueue, NotificationError
from agent.tasks.scheduler import ReminderScheduler
from backend.core.config import Settings
from backend.core.security import PermissionStatus
from desktop.launcher.app import JarvisApplication
from desktop.runtime.state import RuntimeState
from desktop.tray.tray import TrayController, TrayError
from tests.test_launcher_tray import FakeManager, FakePower, FakeTray
from tests.test_voice_engine import (
    FakeAudioInput,
    FakeAudioOutput,
    FakeLLM,
    FakeSTT,
    FakeTTS,
    FakeWakeWord,
    _conversation,
)
from voice.bootstrap import _build_conversation, build_reminder_scheduler, build_task_system
from voice.engine import VoiceEngine


def settings(**overrides) -> Settings:
    base = {
        "DATABASE_URL": "postgresql+psycopg2://jarvis:jarvis@localhost:5432/jarvis_test",
        "JARVIS_MEMORY_ENABLED": False, "JARVIS_RAG_ENABLED": False, "JARVIS_KG_ENABLED": False, "JARVIS_EVENTS_ENABLED": False, "JARVIS_BRIEFING_ENABLED": False,
        "JARVIS_TIMEZONE": "Asia/Kolkata",
    }
    base.update(overrides)
    return Settings(_env_file=None, **base)


# ---- bootstrap -----------------------------------------------------------------------------------------------


def test_task_system_is_built_from_settings():
    system = build_task_system(settings())
    assert system.tasks is not None and system.reminders is not None
    assert system.parser.zone.key == "Asia/Kolkata"
    assert {t.name for t in system.tools} == {
        "create_task", "list_tasks", "complete_task", "cancel_task",
        "create_reminder", "list_reminders", "cancel_reminder",
    }


def test_each_subsystem_can_be_disabled_independently():
    only_reminders = build_task_system(settings(JARVIS_TASKS_ENABLED=False))
    assert only_reminders.tasks is None and {t.name for t in only_reminders.tools} == {
        "create_reminder", "list_reminders", "cancel_reminder"}
    only_tasks = build_task_system(settings(JARVIS_REMINDERS_ENABLED=False))
    assert only_tasks.reminders is None and "create_reminder" not in {t.name for t in only_tasks.tools}
    assert build_task_system(settings(JARVIS_TASKS_ENABLED=False, JARVIS_REMINDERS_ENABLED=False)) is None


def test_the_conversation_registers_only_the_task_tools_with_the_permission_manager():
    engine = _build_conversation(settings(), build_task_system(settings()))
    assert engine._actions is not None
    perms = engine._permissions
    assert perms.request_permission("create_reminder", "execute").status is PermissionStatus.APPROVED  # LOW risk
    assert perms.request_permission("cancel_reminder", "execute").status is PermissionStatus.PENDING  # needs the user
    for unknown in ("email", "delete_task", "shell"):
        assert perms.request_permission(unknown, "execute").status is PermissionStatus.DENIED


def test_without_a_task_system_nothing_changes_from_phase_8():
    engine = _build_conversation(settings(), None)
    assert engine._actions is None
    assert engine._permissions.request_permission("create_task", "execute").status is PermissionStatus.DENIED


def test_the_scheduler_needs_a_delivery_channel():
    system = build_task_system(settings())
    assert isinstance(build_reminder_scheduler(settings(), system, lambda t, m: None), ReminderScheduler)
    assert isinstance(build_reminder_scheduler(settings(), system, None), ReminderScheduler)  # voice only
    no_channels = settings(JARVIS_REMINDER_VOICE_NOTIFICATIONS=False)
    assert build_reminder_scheduler(no_channels, system, None) is None  # reminders could never be delivered
    assert build_reminder_scheduler(settings(JARVIS_REMINDERS_ENABLED=False), build_task_system(settings(JARVIS_REMINDERS_ENABLED=False)), lambda t, m: None) is None
    assert build_reminder_scheduler(settings(), None, lambda t, m: None) is None


def test_scheduler_uses_the_configured_policy_and_interval():
    system = build_task_system(settings())
    sched = build_reminder_scheduler(settings(JARVIS_MISSED_REMINDER_POLICY="expire", JARVIS_REMINDER_POLL_SECONDS=7), system, None)
    assert sched._poll == 7 and sched._policy.value == "expire"


# ---- VoiceEngine announcements ---------------------------------------------------------------------------------


def make_engine(queue, tts=None):
    return VoiceEngine(
        wakeword=FakeWakeWord(trigger_on_call=1), stt=FakeSTT("what is today's date"), conversation=_conversation(FakeLLM()),
        tts=tts or FakeTTS(), audio_input=FakeAudioInput(), audio_output=FakeAudioOutput(), sample_rate=16000,
        listen_seconds=1.0, announcements=queue,
    )


def test_engine_speaks_a_queued_reminder_while_waiting_for_the_wake_word():
    queue, tts = AnnouncementQueue(), FakeTTS()
    engine = make_engine(queue, tts)
    seen = []

    original = queue.set_accepting
    queue.set_accepting = lambda value: (seen.append(value), original(value))[1]
    queue.set_accepting(True)
    assert queue.put("Reminder: submit my assignment") is True
    engine.run_once()
    assert tts.spoken[0] == "Reminder: submit my assignment"  # spoken on the voice thread, before the conversation
    assert tts.spoken[1] == "Yes?"
    assert len(queue) == 0
    assert seen[-1] is False and queue.accepting is False  # not accepting once the engine stopped


def test_engine_marks_the_queue_accepting_only_while_running():
    queue = AnnouncementQueue()
    accepting_during = []
    engine = make_engine(queue)
    original = engine._audio_input.read_frame
    engine._audio_input.read_frame = lambda: (accepting_during.append(queue.accepting), original())[1]
    assert queue.accepting is False
    engine.run_once()
    assert accepting_during and all(accepting_during)
    assert queue.accepting is False


def test_a_failing_announcement_does_not_stop_the_engine():
    class BrokenTTS(FakeTTS):
        def synthesize(self, text):
            if text.startswith("Reminder"):
                raise RuntimeError("audio device lost")
            return super().synthesize(text)

    queue = AnnouncementQueue()
    engine = make_engine(queue, BrokenTTS())
    queue.set_accepting(True)
    queue.put("Reminder: x")
    assert engine.run_once() == "I don't have access to a calendar yet."  # the normal cycle still completed


def test_engine_without_announcements_is_unchanged():
    tts = FakeTTS()
    engine = VoiceEngine(FakeWakeWord(), FakeSTT("hi"), _conversation(), tts, FakeAudioInput(), FakeAudioOutput(), 16000, 1.0)
    engine.run_once()
    assert tts.spoken[0] == "Yes?"


# ---- launcher lifecycle ---------------------------------------------------------------------------------------


class FakeScheduler:
    def __init__(self, fail=False):
        self.calls, self.fail = [], fail

    def start(self):
        self.calls.append("start")
        if self.fail:
            raise RuntimeError("cannot start")

    def stop(self):
        self.calls.append("stop")


def run_app(scheduler):
    manager, exit_event = FakeManager(), threading.Event()
    app = JarvisApplication(manager, exit_event, tray=FakeTray(), power=FakePower(), scheduler=scheduler)
    result = {}
    thread = threading.Thread(target=lambda: result.setdefault("code", app.run()))
    thread.start()
    deadline = time.monotonic() + 3
    while "start" not in manager.calls and time.monotonic() < deadline:
        time.sleep(0.005)
    app.request_exit()
    thread.join(5)
    return result["code"], manager, scheduler


def test_scheduler_starts_with_the_runtime_and_stops_on_shutdown():
    code, manager, scheduler = run_app(FakeScheduler())
    assert code == 0 and scheduler.calls == ["start", "stop"] and manager.calls == ["start", "shutdown"]


def test_a_scheduler_that_cannot_start_does_not_stop_jarvis():
    code, manager, scheduler = run_app(FakeScheduler(fail=True))
    assert code == 0 and "start" in manager.calls and scheduler.calls == ["start", "stop"]


def test_application_without_a_scheduler_is_unchanged():
    code, manager, _ = run_app(None)
    assert code == 0 and manager.calls == ["start", "shutdown"]


# ---- tray notification ------------------------------------------------------------------------------------------


class FakeIcon:
    def __init__(self, fail=False):
        self.shown, self.fail = [], fail

    def notify(self, message, title):
        if self.fail:
            raise OSError("no shell")
        self.shown.append((title, message))


def test_tray_notify_shows_a_balloon_notification():
    tray = TrayController(FakeManager(RuntimeState.RUNNING), lambda: None)
    tray._icon = FakeIcon()
    tray.notify("JARVIS reminder", "Reminder: call Mom")
    assert tray._icon.shown == [("JARVIS reminder", "Reminder: call Mom")]


def test_tray_notify_fails_loudly_without_a_tray_or_on_error():
    tray = TrayController(FakeManager(), lambda: None)
    with pytest.raises(TrayError):
        tray.notify("t", "m")
    tray._icon = FakeIcon(fail=True)
    with pytest.raises(TrayError):
        tray.notify("t", "m")


def test_scheduler_does_not_record_delivery_when_the_tray_is_missing(session_factory):
    from datetime import timedelta

    from agent.tasks.models import ReminderStatus
    from agent.tasks.notifications import CompositeNotifier, DesktopNotifier
    from tests.task_helpers import make_services

    tasks, reminders, _, clock = make_services(session_factory)
    tray = TrayController(FakeManager(), lambda: None)  # no icon: notify raises
    sched = ReminderScheduler(reminders, CompositeNotifier([("desktop", DesktopNotifier(tray.notify))]), clock=clock)
    r = reminders.create_reminder("x", clock() + timedelta(minutes=1))
    clock.advance(minutes=2)
    assert sched.run_once() == 0
    assert reminders.get_reminder(r.reminder_id).status is ReminderStatus.SCHEDULED  # not marked delivered
    tray._icon = FakeIcon()
    assert sched.run_once() == 1 and tray._icon.shown[0] == ("JARVIS reminder", "Reminder: x")
