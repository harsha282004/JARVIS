"""Tray icon *Windows registration* verification (desktop/tray/tray.py).

Root cause this exists for (confirmed by reading pystray's source and reproducing on a real machine): pystray's
Windows backend calls `Shell_NotifyIcon(NIM_ADD, ...)` and never checks its BOOL return value, so if that call fails
-- which really happens for a few seconds right after logon or a Task-Scheduler-triggered interactive launch, this
project's real startup path -- pystray proceeds exactly as if the icon were showing. `TrayController.start()`'s own
`ready` event used to only ever mean "pystray's call did not raise", which is a strictly weaker claim than "Windows
registered this icon somewhere (including the hidden-icons overflow)". These tests exercise the independent
verification (`Shell_NotifyIconGetRect`, faked here) added on top of that, and its retry/self-heal behaviour.

Uses a fake pystray.Icon; no real Windows shell integration and no real tray icon is shown."""

import threading
from datetime import datetime, timezone

import pytest

import desktop.tray.tray as tray_module
from desktop.runtime.state import RuntimeState, RuntimeStatus
from desktop.tray.tray import TrayController, TrayError


class FakeManager:
    def __init__(self, state=RuntimeState.RUNNING):
        self.state = state

    def status(self):
        return RuntimeStatus(self.state, None, datetime.now(timezone.utc), None, False)

    def add_listener(self, listener):
        pass


class FakePystrayIcon:
    """Mimics the surface of pystray.Icon (win32 backend) that TrayController actually touches."""

    def __init__(self, name, icon, title, menu=None):
        self.name, self.icon, self.title, self.menu = name, icon, title, menu
        self._hwnd = 424242
        self._thread = threading.current_thread()
        self.visible = False
        self.stopped = False

    def run_detached(self, setup):
        setup(self)  # synchronous here: real pystray runs this on its own thread, but the sequencing tested is the same

    def update_menu(self):
        pass

    def stop(self):
        self.stopped = True


def _controller(monkeypatch, registered_sequence):
    """`registered_sequence`: values `_icon_registered_with_windows` returns on successive calls, in order."""
    calls = iter(registered_sequence)
    monkeypatch.setattr(tray_module, "_icon_registered_with_windows", lambda hwnd, uid=0: next(calls))
    monkeypatch.setattr(tray_module.pystray, "Icon", FakePystrayIcon)
    monkeypatch.setattr(tray_module.time, "sleep", lambda seconds: None)  # no real delay in the in-process retry loop
    return TrayController(FakeManager(), lambda: None)


def test_start_succeeds_when_windows_confirms_registration(monkeypatch):
    tray = _controller(monkeypatch, [True])
    tray.start()
    health = tray.health()
    assert health["controller"] == "running" and health["icon_created"] and health["icon_registered"] is True


def test_start_raises_when_windows_never_confirms_registration(monkeypatch):
    """The exact bug this fix targets: a NIM_ADD that silently failed must now surface as a real failure (so
    TrayKeeper's existing backoff retry loop actually runs), never as a false "running"."""
    tray = _controller(monkeypatch, [False, False, False, False])  # every in-process retry attempt fails too
    with pytest.raises(TrayError, match="did not register"):
        tray.start()
    assert tray.health()["controller"] == "stopped"


def test_start_succeeds_after_a_transient_registration_failure(monkeypatch):
    """Explorer's tray can be briefly not ready right after logon/Task-Scheduler startup: one failed check that
    then succeeds on an in-process retry must not be treated as a real failure."""
    tray = _controller(monkeypatch, [False, True])
    tray.start()
    assert tray.health()["icon_registered"] is True


def test_refresh_self_heals_a_lost_icon(monkeypatch):
    """An Explorer restart can silently drop the icon between health ticks: refresh() must notice on its next
    periodic re-verification and re-add it, not keep reporting stale "registered" state."""
    tray = _controller(monkeypatch, [True, False, True])
    tray.start()
    assert tray.health()["icon_registered"] is True
    tray.refresh()  # sees it's gone (False) -> toggles visible off/on to re-add -> re-verifies (True)
    assert tray.health()["icon_registered"] is True


def test_refresh_does_not_flap_an_icon_that_is_still_registered(monkeypatch):
    tray = _controller(monkeypatch, [True, True])
    tray.start()
    icon = tray._icon
    tray.refresh()
    assert icon.stopped is False and tray.health()["icon_registered"] is True


def test_health_before_start_reports_no_icon():
    tray = TrayController(FakeManager(), lambda: None)
    assert tray.health() == {"controller": "stopped", "icon_created": False, "thread_alive": False,
                              "icon_registered": None, "registered_at": None, "last_error": ""}


def test_stop_clears_registration_state(monkeypatch):
    tray = _controller(monkeypatch, [True])
    tray.start()
    tray.stop()
    health = tray.health()
    assert health["controller"] == "stopped" and health["icon_registered"] is None and tray._icon is None


def test_verification_off_windows_is_unknown_not_a_failure(monkeypatch):
    """On a non-Windows platform (or any environment where the check cannot run), verification must report `None`
    (unknown) and never block startup -- this is a real diagnostic, not a portability requirement."""
    monkeypatch.setattr(tray_module.sys, "platform", "linux")
    assert tray_module._icon_registered_with_windows(123) is None


def test_jarvis_application_exposes_tray_health_including_when_tray_disabled():
    from desktop.launcher.app import JarvisApplication

    app = JarvisApplication(FakeManager(), threading.Event(), tray=None)
    health = app.tray_health
    assert health["controller"] == "disabled" and health["icon_registered"] is None
