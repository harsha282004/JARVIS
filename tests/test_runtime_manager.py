"""Unit tests for RuntimeManager lifecycle, using fake engines.

These verify the lifecycle logic (states, threading, idempotency, failure
handling). They do not use real audio, models, or Windows APIs.
"""

import threading
import time

import pytest

from backend.core.llm.base import LLMProviderError
from desktop.runtime.manager import RuntimeManager
from desktop.runtime.state import RuntimeState
from voice.exceptions import AudioDeviceError


class FakeEngine:
    """Blocks in run_once until should_stop, like waiting for a wake word."""

    def __init__(self, script=None):
        self.state = "waiting"
        self.calls = 0
        self.mic_open = False
        self._script = list(script or [])

    @property
    def microphone_active(self) -> bool:
        return self.mic_open

    def run_once(self, should_stop=None):
        self.calls += 1
        if self._script:
            action = self._script.pop(0)
            if isinstance(action, Exception):
                raise action
        self.mic_open = True
        try:
            while not should_stop():
                time.sleep(0.002)
        finally:
            self.mic_open = False
        return None


def wait_for(predicate, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return False


@pytest.fixture
def make_manager():
    managers = []

    def _make(factory=None, engine=None):
        engine = engine or FakeEngine()
        built = []

        def default_factory():
            built.append(engine)
            return engine

        manager = RuntimeManager(factory or default_factory, stop_timeout=2.0)
        managers.append(manager)
        return manager, engine, built

    yield _make
    for manager in managers:
        manager.shutdown()


def test_initial_state_is_stopped(make_manager):
    manager, _, built = make_manager()
    assert manager.state is RuntimeState.STOPPED
    assert built == []
    status = manager.status()
    assert status.voice_state is None
    assert status.microphone_active is False
    assert status.last_error is None


def test_start_reaches_running_and_engine_is_driven(make_manager):
    manager, engine, built = make_manager()
    assert manager.start() is True
    assert wait_for(lambda: manager.state is RuntimeState.RUNNING)
    assert wait_for(lambda: manager.status().microphone_active)
    assert built == [engine]
    assert manager.status().voice_state == "waiting"


def test_starting_state_is_visible_while_engine_loads(make_manager):
    release = threading.Event()
    engine = FakeEngine()

    def slow_factory():
        release.wait(2)
        return engine

    manager, _, _ = make_manager(factory=slow_factory)
    manager.start()
    assert manager.state is RuntimeState.STARTING
    release.set()
    assert wait_for(lambda: manager.state is RuntimeState.RUNNING)


def test_start_is_ignored_when_already_running(make_manager):
    manager, _, built = make_manager()
    manager.start()
    assert wait_for(lambda: manager.state is RuntimeState.RUNNING)
    assert manager.start() is False
    assert len(built) == 1


def test_pause_releases_microphone_and_resume_reuses_engine(make_manager):
    manager, engine, built = make_manager()
    manager.start()
    assert wait_for(lambda: manager.status().microphone_active)

    assert manager.pause() is True
    assert manager.state is RuntimeState.PAUSED
    assert engine.mic_open is False
    assert manager.status().microphone_active is False

    assert manager.resume() is True
    assert manager.state is RuntimeState.RUNNING
    assert wait_for(lambda: engine.mic_open)
    assert built == [engine]  # not rebuilt


def test_pause_and_resume_are_ignored_in_wrong_state(make_manager):
    manager, _, _ = make_manager()
    assert manager.pause() is False
    assert manager.resume() is False
    assert manager.state is RuntimeState.STOPPED


def test_shutdown_stops_worker_and_releases_resources(make_manager):
    manager, engine, _ = make_manager()
    manager.start()
    assert wait_for(lambda: engine.mic_open)
    manager.shutdown()
    assert manager.state is RuntimeState.STOPPED
    assert engine.mic_open is False
    assert manager.status().voice_state is None


def test_shutdown_is_idempotent(make_manager):
    manager, _, _ = make_manager()
    manager.start()
    assert wait_for(lambda: manager.state is RuntimeState.RUNNING)
    manager.shutdown()
    manager.shutdown()
    assert manager.state is RuntimeState.STOPPED


def test_shutdown_without_start_is_safe(make_manager):
    manager, _, _ = make_manager()
    manager.shutdown()
    assert manager.state is RuntimeState.STOPPED


def test_concurrent_shutdown_does_not_crash(make_manager):
    manager, _, _ = make_manager()
    manager.start()
    assert wait_for(lambda: manager.state is RuntimeState.RUNNING)
    threads = [threading.Thread(target=manager.shutdown) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(5)
    assert manager.state is RuntimeState.STOPPED


def test_shutdown_while_starting(make_manager):
    def slow_factory():
        time.sleep(0.3)
        return FakeEngine()

    manager, _, _ = make_manager(factory=slow_factory)
    manager.start()
    manager.shutdown()
    assert manager.state is RuntimeState.STOPPED
    time.sleep(0.5)  # the late-finishing worker must not resurrect the runtime
    assert manager.state is RuntimeState.STOPPED


def test_engine_build_failure_sets_error_and_recovers_on_restart(make_manager):
    attempts = []

    def flaky_factory():
        attempts.append(1)
        if len(attempts) == 1:
            raise AudioDeviceError("no microphone")
        return FakeEngine()

    manager, _, _ = make_manager(factory=flaky_factory)
    manager.start()
    assert wait_for(lambda: manager.state is RuntimeState.ERROR)
    assert "no microphone" in manager.status().last_error

    assert manager.restart() is True
    assert wait_for(lambda: manager.state is RuntimeState.RUNNING)
    assert manager.status().last_error is None
    assert len(attempts) == 2


def test_engine_crash_while_running_sets_error(make_manager):
    engine = FakeEngine(script=[AudioDeviceError("device unplugged")])
    manager, _, _ = make_manager(engine=engine)
    manager.start()
    assert wait_for(lambda: manager.state is RuntimeState.ERROR)
    assert "device unplugged" in manager.status().last_error
    assert manager.status().microphone_active is False


def test_llm_error_is_recorded_but_runtime_keeps_running(make_manager):
    engine = FakeEngine(script=[LLMProviderError("ollama down")])
    manager, _, _ = make_manager(engine=engine)
    manager.start()
    assert wait_for(lambda: engine.calls >= 2)  # looped again after the error
    assert manager.state is RuntimeState.RUNNING
    assert "ollama down" in manager.status().last_error


def test_start_from_error_state(make_manager):
    engine = FakeEngine(script=[AudioDeviceError("boom")])
    manager, _, _ = make_manager(engine=engine)
    manager.start()
    assert wait_for(lambda: manager.state is RuntimeState.ERROR)
    assert manager.start() is True
    assert wait_for(lambda: manager.state is RuntimeState.RUNNING)


def test_restart_from_running_rebuilds_engine(make_manager):
    manager, _, built = make_manager()
    manager.start()
    assert wait_for(lambda: manager.state is RuntimeState.RUNNING)
    assert manager.restart() is True
    assert wait_for(lambda: len(built) == 2 and manager.state is RuntimeState.RUNNING)


def test_system_resume_reacquires_microphone_when_running(make_manager):
    manager, engine, _ = make_manager()
    manager.start()
    assert wait_for(lambda: engine.calls == 1 and engine.mic_open)
    manager.handle_system_resume()
    assert wait_for(lambda: engine.calls == 2 and engine.mic_open)
    assert manager.state is RuntimeState.RUNNING


def test_system_resume_leaves_paused_runtime_paused(make_manager):
    manager, engine, _ = make_manager()
    manager.start()
    assert wait_for(lambda: engine.mic_open)
    manager.pause()
    manager.handle_system_resume()
    assert manager.state is RuntimeState.PAUSED
    assert engine.mic_open is False


def test_listeners_receive_state_changes(make_manager):
    manager, _, _ = make_manager()
    seen = []
    manager.add_listener(lambda status: seen.append(status.state))
    manager.start()
    assert wait_for(lambda: RuntimeState.RUNNING in seen)
    manager.pause()
    assert seen[:3] == [RuntimeState.STARTING, RuntimeState.RUNNING, RuntimeState.PAUSED]


def test_failing_listener_does_not_break_runtime(make_manager):
    manager, _, _ = make_manager()

    def bad_listener(status):
        raise RuntimeError("listener bug")

    manager.add_listener(bad_listener)
    manager.start()
    assert wait_for(lambda: manager.state is RuntimeState.RUNNING)


def test_worker_that_never_stops_puts_pause_in_error():
    release, entered = threading.Event(), threading.Event()

    class StuckEngine(FakeEngine):
        def run_once(self, should_stop=None):
            entered.set()
            release.wait(5)  # ignores should_stop until the test releases it

    manager = RuntimeManager(lambda: StuckEngine(), stop_timeout=0.05)
    manager.start()
    assert entered.wait(3)  # worker is inside run_once, ignoring stop requests
    assert manager.pause() is False
    assert manager.state is RuntimeState.ERROR
    assert "did not stop" in manager.status().last_error
    release.set()
    manager.shutdown()
