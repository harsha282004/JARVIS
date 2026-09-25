"""Windows runtime hardening: tray status, power state, application lifecycle, privacy wiring, automatic recovery, health checks."""

import threading
import time
from datetime import datetime, timezone

import pytest

from backend.core.events import EventBus, SystemEvent
from backend.core.health import Health, HealthMonitor, OverallStatus, ServiceState
from backend.core.privacy import PrivacyController, PrivacyMode
from backend.core.recovery import BackoffPolicy, SupervisedService, Supervisor
from desktop.launcher.app import JarvisApplication
from desktop.runtime.health_checks import _Cached, integration_check, microphone_check, model_file_check, runtime_check
from desktop.runtime.manager import RuntimeManager
from desktop.runtime.power import PowerState, PowerStateTracker
from desktop.runtime.state import RuntimeState, RuntimeStatus
from desktop.tray.status import TrayState, compute_tray_view
from desktop.tray.tray import render_icon
from tests.test_runtime_manager import FakeEngine, wait_for


def status(state, voice=None, mic=False, error=None):
    return RuntimeStatus(state, voice, datetime.now(timezone.utc), error, mic)


# ---- tray status is derived from facts -------------------------------------------------------------------------------------------

@pytest.mark.parametrize("rt,overall,mode,expected", [
    (RuntimeState.RUNNING, OverallStatus.ONLINE, PrivacyMode.ACTIVE, TrayState.ONLINE),
    (RuntimeState.RUNNING, OverallStatus.DEGRADED, PrivacyMode.ACTIVE, TrayState.DEGRADED),
    (RuntimeState.RUNNING, OverallStatus.OFFLINE, PrivacyMode.ACTIVE, TrayState.OFFLINE),
    (RuntimeState.STARTING, None, PrivacyMode.ACTIVE, TrayState.STARTING),
    (RuntimeState.ERROR, OverallStatus.ONLINE, PrivacyMode.ACTIVE, TrayState.OFFLINE),  # a health monitor cannot make a dead runtime "online"
    (RuntimeState.STOPPED, None, PrivacyMode.ACTIVE, TrayState.OFFLINE),
    (RuntimeState.PAUSED, OverallStatus.ONLINE, PrivacyMode.ACTIVE, TrayState.PAUSED),
    (RuntimeState.RUNNING, OverallStatus.ONLINE, PrivacyMode.PRIVATE, TrayState.PAUSED),
])
def test_tray_view_matrix(rt, overall, mode, expected):
    assert compute_tray_view(status(rt), overall, mode).state is expected


def test_private_mode_says_microphone_disabled():
    view = compute_tray_view(status(RuntimeState.RUNNING, "waiting", mic=True), OverallStatus.ONLINE, PrivacyMode.PRIVATE)
    assert "Microphone disabled" in view.voice_label


@pytest.mark.parametrize("state", list(TrayState))
def test_icons_render_for_each_tray_state(state):
    assert render_icon(state).size == (64, 64)


# ---- power state -------------------------------------------------------------------------------------------------------------------

def test_power_state_transitions_and_resume():
    seen, locked = [], {"v": False}
    t = PowerStateTracker(lambda a, b: seen.append((a.value, b.value)), probe=lambda: locked["v"])
    locked["v"] = True
    assert t.poll() is PowerState.BACKGROUND
    locked["v"] = False
    t.note_resume()  # the machine slept: SUSPENDED -> RESUME -> ACTIVE
    assert [b for _, b in seen] == ["background", "suspended", "resume", "active"]
    t.shutting_down()
    locked["v"] = True
    assert t.poll() is PowerState.OFF  # OFF is final


def test_unknown_lock_state_does_not_change_state():
    t = PowerStateTracker(probe=lambda: None)
    assert t.poll() is PowerState.ACTIVE


# ---- application lifecycle ---------------------------------------------------------------------------------------------------------

class FakeManager:
    def __init__(self):
        self.calls = []

    def start(self):
        self.calls.append("start")

    def shutdown(self):
        self.calls.append("shutdown")


class Svc:
    def __init__(self, name, log, fail_start=False, fail_stop=False):
        self.name, self.log, self.fail_start, self.fail_stop = name, log, fail_start, fail_stop

    def start(self):
        self.log.append(f"start {self.name}")
        if self.fail_start:
            raise RuntimeError("x")

    def stop(self):
        self.log.append(f"stop {self.name}")
        if self.fail_stop:
            raise RuntimeError("x")


def test_background_services_start_after_voice_and_stop_in_reverse_isolated():
    log, exit_event, bus = [], threading.Event(), EventBus()
    events = []
    bus.subscribe(None, lambda e: events.append(e.type))
    mgr = FakeManager()
    app = JarvisApplication(mgr, exit_event, background=[Svc("a", log), Svc("bad", log, fail_start=True), Svc("c", log, fail_stop=True), Svc("d", log)],
                            bus=bus, cleanup=[lambda: log.append("cleanup")])
    exit_event.set()
    assert app.run() == 0
    assert log == ["start a", "start bad", "start c", "start d", "stop d", "stop c", "stop a", "cleanup"]  # one failure never stops the rest
    assert mgr.calls == ["start", "shutdown"] and events == [SystemEvent.SYSTEM_START, SystemEvent.SYSTEM_SHUTDOWN]


def test_shutdown_is_idempotent_and_cleanup_runs_even_if_manager_fails():
    class BadManager(FakeManager):
        def shutdown(self):
            raise RuntimeError("stuck")

    log, exit_event = [], threading.Event()
    app = JarvisApplication(BadManager(), exit_event, cleanup=[lambda: log.append("cleanup")])
    exit_event.set()
    app.run()
    app._shutdown()
    assert log == ["cleanup"]


# ---- privacy <-> runtime -----------------------------------------------------------------------------------------------------------

def make_manager(engine=None, start_paused=None):
    engine = engine or FakeEngine()
    return RuntimeManager(lambda: engine, stop_timeout=3, start_paused=start_paused), engine


def test_saved_private_mode_starts_paused_and_never_opens_the_microphone(tmp_path):
    PrivacyController(tmp_path / "p.json").set_mode(PrivacyMode.PRIVATE)
    privacy = PrivacyController(tmp_path / "p.json")
    manager, engine = make_manager(start_paused=lambda: not privacy.capabilities.microphone)
    manager.start()
    assert wait_for(lambda: manager.state is RuntimeState.PAUSED)
    time.sleep(0.05)
    assert engine.calls == 0 and not manager.status().microphone_active  # the microphone was never opened
    assert manager.request_activation() is False  # and "Talk to JARVIS" cannot open it either
    manager.shutdown()


def test_privacy_listener_pauses_and_resumes_only_what_it_paused():
    from desktop.runtime.composition import RuntimeServices

    manager, engine = make_manager()
    manager.start()
    assert wait_for(lambda: manager.state is RuntimeState.RUNNING)
    privacy = PrivacyController()
    svc = RuntimeServices.__new__(RuntimeServices)
    svc.manager, svc.privacy, svc.paused_by_privacy = manager, privacy, False
    privacy.add_listener(svc._on_privacy_change)
    privacy.set_mode(PrivacyMode.PRIVATE)
    assert manager.state is RuntimeState.PAUSED and not manager.status().microphone_active
    privacy.set_mode(PrivacyMode.ACTIVE)
    assert wait_for(lambda: manager.state is RuntimeState.RUNNING)
    manager.pause()  # the user's own pause is not undone by a privacy change
    privacy.set_mode(PrivacyMode.BACKGROUND)
    assert manager.state is RuntimeState.PAUSED
    manager.shutdown()


# ---- automatic recovery ------------------------------------------------------------------------------------------------------------

def test_crashed_voice_runtime_is_restarted_by_the_supervisor():
    attempts = {"n": 0}
    engine = FakeEngine()

    def factory():
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise RuntimeError("audio device vanished")
        return engine

    manager = RuntimeManager(factory, stop_timeout=3)
    manager.start()
    assert wait_for(lambda: manager.state is RuntimeState.ERROR)
    clock = {"t": 0.0}
    sup = Supervisor(clock=lambda: clock["t"])
    sup.add(SupervisedService("voice_runtime", lambda: manager.state is not RuntimeState.ERROR, manager.restart, BackoffPolicy(1.0, 2.0, 30.0, 3, 60.0)))
    sup.tick()
    assert wait_for(lambda: manager.state is RuntimeState.RUNNING) and attempts["n"] == 2  # recovered without user action
    assert manager.status().last_error is None
    manager.shutdown()


def test_paused_or_stopped_runtime_is_not_treated_as_a_failure():
    manager, _ = make_manager()
    manager.start()
    assert wait_for(lambda: manager.state is RuntimeState.RUNNING)
    manager.pause()
    restarts = []
    sup = Supervisor()
    sup.add(SupervisedService("v", lambda: manager.state is not RuntimeState.ERROR, lambda: restarts.append(1)))
    sup.tick()
    assert restarts == []
    manager.shutdown()


def test_manual_activation_reaches_the_engine_only_while_running():
    manager, engine = make_manager()
    engine.request_activation = lambda: engine.__dict__.setdefault("activated", True)
    assert manager.request_activation() is False
    manager.start()
    assert wait_for(lambda: manager.state is RuntimeState.RUNNING)
    assert manager.request_activation() is True and engine.activated
    manager.shutdown()


# ---- health checks -----------------------------------------------------------------------------------------------------------------

def test_runtime_and_microphone_checks_are_truthful():
    manager, _ = make_manager()
    assert runtime_check(manager)().state is ServiceState.DISCONNECTED
    manager.start()
    assert wait_for(lambda: manager.state is RuntimeState.RUNNING)
    assert wait_for(lambda: manager.status().microphone_active)
    privacy = PrivacyController()
    assert microphone_check(manager, privacy)().state is ServiceState.HEALTHY
    privacy.set_mode(PrivacyMode.PRIVATE)
    assert microphone_check(manager, privacy)().state is ServiceState.DISABLED
    manager.shutdown()


def test_model_file_check(tmp_path):
    f = tmp_path / "m.onnx"
    assert model_file_check(str(f), "wake word")().state is ServiceState.FAILED
    f.write_bytes(b"x")
    assert model_file_check(str(f), "wake word")().state is ServiceState.HEALTHY
    assert model_file_check("", "wake word", enabled=False)().state is ServiceState.DISABLED


def test_integration_check_disabled_offline_and_backoff_cache():
    from backend.core.config import Settings
    from integrations.gmail.models import GmailUnavailable

    settings = Settings(_env_file=None, DATABASE_URL="sqlite://")
    offline = Settings(_env_file=None, DATABASE_URL="sqlite://", JARVIS_OFFLINE_MODE=True)
    assert integration_check("Gmail", None, lambda: None, settings)().state is ServiceState.DISABLED

    class Svc:
        def is_configured(self):
            return True

    calls = []
    assert integration_check("Gmail", Svc(), lambda: calls.append(1), offline)().detail == "offline mode" and calls == []  # no network in offline mode

    def down():
        calls.append(1)
        raise GmailUnavailable()

    check = integration_check("Gmail", Svc(), down, settings)
    assert check().state is ServiceState.DISCONNECTED
    check()
    assert len(calls) == 1  # cached between checks: monitoring does not hammer the service


def test_cached_check_rechecks_quickly_after_failure_then_backs_off():
    t, calls, results = {"now": 0.0}, [], [Health(ServiceState.FAILED), Health(ServiceState.FAILED), Health(ServiceState.HEALTHY)]

    def check():
        calls.append(t["now"])
        return results[len(calls) - 1]

    c = _Cached(check, ttl=300, clock=lambda: t["now"], retry_first=15)
    c()
    t["now"] = 10
    c()
    t["now"] = 16
    c()
    assert c().state is ServiceState.FAILED
    assert calls == [0, 16]  # first retry after 15 s, not after 5 minutes


def test_monitor_reports_degraded_when_gmail_fails_but_voice_runs():
    mon = HealthMonitor()
    mon.register("voice_runtime", lambda: Health(ServiceState.HEALTHY), critical=True)
    mon.register("gmail", lambda: Health(ServiceState.DISCONNECTED, "cannot be reached"))
    mon.check_all()
    assert mon.overall() is OverallStatus.DEGRADED
