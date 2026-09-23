"""Unit tests for the launcher application sequencing, CLI parsing and tray
controller (with a fake manager). No real tray icon is shown and no Windows
shell integration is exercised here — see docs/windows-runtime.md for the
manual Windows checks."""

import threading
import time

import pytest

from desktop.launcher.app import JarvisApplication
from desktop.launcher.cli import parse_args
from desktop.runtime.state import RuntimeState
from desktop.tray.tray import TrayController, TrayError, render_icon


class FakeManager:
    def __init__(self, state=RuntimeState.STOPPED):
        self.state = state
        self.calls = []

    def start(self):
        self.calls.append("start")
        self.state = RuntimeState.RUNNING

    def pause(self):
        self.calls.append("pause")

    def resume(self):
        self.calls.append("resume")

    def restart(self):
        self.calls.append("restart")

    def shutdown(self):
        self.calls.append("shutdown")
        self.state = RuntimeState.STOPPED

    def handle_system_resume(self):
        self.calls.append("system_resume")


class FakeTray:
    def __init__(self, fail=False):
        self.fail = fail
        self.calls = []

    def start(self):
        if self.fail:
            raise TrayError("no display")
        self.calls.append("start")

    def stop(self):
        self.calls.append("stop")


class FakePower:
    def __init__(self):
        self.calls = []

    def start(self):
        self.calls.append("start")

    def stop(self):
        self.calls.append("stop")


def test_application_startup_and_shutdown_sequence():
    manager, tray, power, exit_event = FakeManager(), FakeTray(), FakePower(), threading.Event()
    app = JarvisApplication(manager, exit_event, tray=tray, power=power)
    result = {}
    thread = threading.Thread(target=lambda: result.setdefault("code", app.run()))
    thread.start()

    deadline = time.monotonic() + 3
    while "start" not in manager.calls and time.monotonic() < deadline:
        time.sleep(0.005)
    assert tray.calls == ["start"]  # tray is up before the voice engine starts
    app.request_exit()
    thread.join(5)

    assert result["code"] == 0
    assert manager.calls == ["start", "shutdown"]
    assert tray.calls == ["start", "stop"]
    assert power.calls == ["start", "stop"]


def test_application_runs_headless_without_tray():
    manager, exit_event = FakeManager(), threading.Event()
    app = JarvisApplication(manager, exit_event)
    exit_event.set()
    assert app.run() == 0
    assert manager.calls == ["start", "shutdown"]


def test_tray_failure_aborts_startup_without_starting_engine():
    manager, tray, exit_event = FakeManager(), FakeTray(fail=True), threading.Event()
    app = JarvisApplication(manager, exit_event, tray=tray)
    assert app.run() == 1
    assert "start" not in manager.calls
    assert "shutdown" in manager.calls


def test_parse_args_defaults_to_running_runtime():
    args = parse_args([])
    assert not (args.enable_startup or args.disable_startup or args.startup_status)


def test_parse_args_startup_flags_are_mutually_exclusive():
    assert parse_args(["--enable-startup"]).enable_startup
    with pytest.raises(SystemExit):
        parse_args(["--enable-startup", "--disable-startup"])


@pytest.mark.parametrize("state", list(RuntimeState))
def test_render_icon_for_every_state(state):
    assert render_icon(state).size == (64, 64)


def test_tray_actions_delegate_to_manager():
    manager, exits = FakeManager(RuntimeState.PAUSED), []
    tray = TrayController(manager, lambda: exits.append(1))

    tray._start_or_resume()
    manager.state = RuntimeState.STOPPED
    tray._start_or_resume()
    tray._pause()
    tray._restart()
    tray._exit()

    assert manager.calls == ["resume", "start", "pause", "restart"]
    assert exits == [1]


def test_tray_menu_enablement_follows_state():
    manager = FakeManager(RuntimeState.RUNNING)
    menu = TrayController(manager, lambda: None)._build_menu()
    items = {item.text: item for item in menu.items if item and isinstance(item.text, str)}

    assert items["Pause"].enabled is True
    assert items["Start / Resume"].enabled is False
    manager.state = RuntimeState.PAUSED
    assert items["Pause"].enabled is False
    assert items["Start / Resume"].enabled is True
