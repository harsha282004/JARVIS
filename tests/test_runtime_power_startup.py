"""Unit tests for the sleep/resume watcher and the Windows startup helper.

The startup tests use a temporary Startup folder and a fake PowerShell
runner: they verify our logic, NOT that real Windows honors the shortcut
(see docs/windows-runtime.md for the manual verification).
"""

from pathlib import Path

import pytest

from desktop.launcher.startup import (
    SHORTCUT_NAME,
    StartupIntegrationError,
    StartupManager,
    default_startup_dir,
)
from desktop.runtime.power import SleepResumeWatcher


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def test_watcher_ignores_normal_heartbeats():
    clock, calls = FakeClock(), []
    watcher = SleepResumeWatcher(lambda: calls.append(1), interval=2, gap_threshold=10, clock=clock)
    for _ in range(5):
        clock.now += 2.1
        assert watcher.tick() is False
    assert calls == []


def test_watcher_detects_sleep_gap_once():
    clock, calls = FakeClock(), []
    watcher = SleepResumeWatcher(lambda: calls.append(1), interval=2, gap_threshold=10, clock=clock)
    clock.now += 3600  # machine slept for an hour
    assert watcher.tick() is True
    clock.now += 2
    assert watcher.tick() is False
    assert calls == [1]


def test_watcher_does_not_count_slow_callback_as_gap():
    clock, calls = FakeClock(), []

    def slow_callback():
        calls.append(1)
        clock.now += 20  # callback blocks

    watcher = SleepResumeWatcher(slow_callback, interval=2, gap_threshold=10, clock=clock)
    clock.now += 100
    watcher.tick()
    clock.now += 2
    assert watcher.tick() is False


def test_watcher_thread_starts_and_stops():
    watcher = SleepResumeWatcher(lambda: None, interval=0.01)
    watcher.start()
    watcher.stop()


class FakeRunner:
    def __init__(self, create=True):
        self.calls = []
        self.create = create

    def __call__(self, command, env):
        self.calls.append((command, env))
        if self.create:
            Path(env["JARVIS_LNK"]).write_text("shortcut")


def test_startup_disabled_by_default(tmp_path):
    manager = StartupManager(startup_dir=tmp_path, runner=FakeRunner())
    assert manager.is_enabled() is False
    assert not (tmp_path / SHORTCUT_NAME).exists()


def test_startup_enable_and_disable(tmp_path):
    runner = FakeRunner()
    project = tmp_path / "proj dir"
    project.mkdir()
    manager = StartupManager(
        startup_dir=tmp_path / "Startup", project_root=project, target=Path("py.exe"), runner=runner
    )

    shortcut = manager.enable()

    assert shortcut == tmp_path / "Startup" / SHORTCUT_NAME
    assert manager.is_enabled()
    _, env = runner.calls[0]
    assert env["JARVIS_TARGET"] == "py.exe"
    assert env["JARVIS_ARGS"] == "-m desktop.launcher"
    assert env["JARVIS_CWD"] == str(project)

    assert manager.disable() is True
    assert not manager.is_enabled()
    assert manager.disable() is False  # idempotent


def test_startup_enable_fails_clearly_if_shortcut_not_created(tmp_path):
    manager = StartupManager(startup_dir=tmp_path, runner=FakeRunner(create=False))
    with pytest.raises(StartupIntegrationError, match="not created"):
        manager.enable()


def test_default_startup_dir_uses_appdata(monkeypatch, tmp_path):
    monkeypatch.setenv("APPDATA", str(tmp_path))
    assert default_startup_dir() == (
        tmp_path / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"
    )


def test_default_startup_dir_requires_appdata(monkeypatch):
    monkeypatch.delenv("APPDATA", raising=False)
    with pytest.raises(StartupIntegrationError, match="APPDATA"):
        default_startup_dir()
