"""Windows auto-start and persistent-runtime hardening.

Root causes covered (each reproduced before it was fixed):
  * `python -m desktop.launcher` only works when the working directory is the project, and Windows starts a Run entry / task action in System32: the process died
    with `No module named 'desktop'` and, being windowless, left no trace. A launcher SCRIPT that sets its own path and directory is the entry point now.
  * `.env` was found relative to the working directory at import time.
  * A tray icon that could not be created right after logon (no taskbar yet) ended the whole process.
  * Nothing recorded WHY the process stopped, and uncaught exceptions vanished under pythonw.
  * Windows logoff / shutdown was not recognised as a stop request.
"""

import json
import logging
import os
import signal
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

import pytest

from desktop.launcher import bootstrap, cli
from desktop.launcher.app import ExitCoordinator, JarvisApplication, TrayKeeper
from desktop.launcher.session import (
    ENDSESSION_LOGOFF,
    WM_ENDSESSION,
    WM_QUERYENDSESSION,
    SessionEndWatcher,
    reason_for_console_event,
    reason_for_end_session,
)
from desktop.launcher.single_instance import SingleInstanceGuard
from desktop.launcher.startup import RUN_KEY, StartupIntegrationError, StartupManager, _WinRegistry, project_pythonw
from desktop.tray.tray import TrayError
from tests.test_launcher_tray import FakeManager, FakePower, FakeTray

ROOT = Path(__file__).resolve().parents[2]
LAUNCHER = ROOT / "scripts" / "windows" / "jarvis_launcher.pyw"
needs_windows = pytest.mark.skipif(sys.platform != "win32", reason="Windows only")


def run_app(app, exit_event, seconds):
    """Run app.run() on a thread; returns (thread, result dict). The caller decides when to end it."""
    result = {}
    thread = threading.Thread(target=lambda: result.setdefault("code", app.run()), daemon=True)
    thread.start()
    time.sleep(seconds)
    return thread, result


# ============================================ startup path =====================================================================================

def test_windows_startup_path(tmp_path):
    project = tmp_path / "proj dir"
    (project / ".venv" / "Scripts").mkdir(parents=True)
    (project / ".venv" / "Scripts" / "pythonw.exe").write_text("x")
    (project / "scripts" / "windows").mkdir(parents=True)
    m = StartupManager(startup_dir=tmp_path / "s", project_root=project, registry=_Reg(), query=lambda c, e: "no")
    cmd = m.command_line()
    assert cmd == f'"{project / ".venv" / "Scripts" / "pythonw.exe"}" "{project / "scripts" / "windows" / "jarvis_launcher.pyw"}" --startup-source windows'
    assert "-m desktop.launcher" not in cmd                                     # not directory-dependent
    assert LAUNCHER.is_file() and StartupManager().launcher_script == LAUNCHER                 # the real script Windows will run exists in the repository


def test_startup_uses_project_python(tmp_path):
    assert project_pythonw(tmp_path).name in ("pythonw.exe", "python.exe", "pythonw", "python")      # falls back to the running interpreter without a .venv
    (tmp_path / ".venv" / "Scripts").mkdir(parents=True)
    (tmp_path / ".venv" / "Scripts" / "pythonw.exe").write_text("x")
    assert project_pythonw(tmp_path) == tmp_path / ".venv" / "Scripts" / "pythonw.exe"      # the project's own interpreter, never whatever `python` is on PATH
    task_env = StartupManager(project_root=tmp_path, registry=_Reg(), query=lambda c, e: "no")._task_env()
    assert task_env["JARVIS_TASK_EXE"].endswith(r".venv\Scripts\pythonw.exe") and task_env["JARVIS_TASK_CWD"] == str(tmp_path)


def test_startup_from_non_project_cwd(tmp_path):
    """The launcher script works from any working directory (what Windows gives a Run entry or a task action); `-m desktop.launcher` cannot, which is why it is not used."""
    out = subprocess.run([sys.executable, str(LAUNCHER), "--self-check"], cwd=tmp_path, capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr[-300:]
    assert f"PROJECT_ROOT={ROOT}" in out.stdout and f"CWD={ROOT}" in out.stdout and "CONFIG_STATUS=ok" in out.stdout
    bad = subprocess.run([sys.executable, "-m", "desktop.launcher", "--self-check"], cwd=tmp_path, capture_output=True, text=True, timeout=60, env={**os.environ, "PYTHONPATH": ""})
    assert bad.returncode != 0 and "No module named 'desktop'" in bad.stderr


@pytest.mark.skipif(not (ROOT / ".env").is_file(), reason="no local .env to test discovery against")
def test_startup_env_loading(tmp_path):
    from backend.core.config import PROJECT_ROOT, Settings

    assert Path(Settings.model_config["env_file"]) == PROJECT_ROOT / ".env" and Path(Settings.model_config["env_file"]).is_absolute()
    env = {k: v for k, v in os.environ.items() if k != "DATABASE_URL"}
    code = "from backend.core.config import get_settings; s = get_settings(); print('ok' if s.DATABASE_URL and s.LLM_PROVIDER else 'empty')"
    out = subprocess.run([sys.executable, "-c", code], cwd=tmp_path, capture_output=True, text=True, timeout=60, env={**env, "PYTHONPATH": str(ROOT)})
    assert out.stdout.strip() == "ok", out.stderr[-300:]                          # .env found from a foreign directory


def test_self_check_never_prints_secrets():
    out = subprocess.run([sys.executable, str(LAUNCHER), "--self-check"], capture_output=True, text=True, timeout=60)
    text = out.stdout
    from backend.core.config import get_settings

    s = get_settings()
    for secret in (s.GROQ_API_KEY.get_secret_value(), s.DATABASE_URL.split("@")[0].split(":")[-1]):
        if secret and len(secret) > 3:
            assert secret not in text


def test_startup_runtime_persists():
    """Started, the application keeps running: it does not fall off the end of main() and no background thread is what keeps it up."""
    manager, exit_event = FakeManager(), threading.Event()
    app = JarvisApplication(manager, exit_event, tray=FakeTray(), power=FakePower())
    thread, result = run_app(app, exit_event, 1.6)
    assert thread.is_alive() and "code" not in result and app.running and manager.calls == ["start"]
    app.request_exit()
    thread.join(5)
    assert result["code"] == 0 and manager.calls == ["start", "shutdown"]


# ============================================ process lifetime =================================================================================

def test_main_runtime_does_not_fall_through():
    assert "while not self._exit_event.wait(" in (ROOT / "desktop" / "launcher" / "app.py").read_text(encoding="utf-8")
    manager, exit_event = FakeManager(), threading.Event()
    app = JarvisApplication(manager, exit_event)
    thread, result = run_app(app, exit_event, 1.2)
    assert thread.is_alive()                                                    # blocked on the exit event, not finished
    exit_event.set()
    thread.join(5)
    assert not thread.is_alive() and result["code"] == 0


def test_runtime_stays_alive_when_optional_parts_fail():
    class Bad:
        def start(self):
            raise RuntimeError("boom")

        def stop(self):
            raise RuntimeError("boom")

    class BadPower(FakePower):
        def start(self):
            raise OSError("no power watcher")

    manager, exit_event = FakeManager(), threading.Event()
    app = JarvisApplication(manager, exit_event, tray=FakeTray(fail=True), power=BadPower(), background=[Bad()])
    thread, result = run_app(app, exit_event, 1.2)
    assert thread.is_alive() and app.running                                     # tray, power watcher and a background service all failed; JARVIS did not
    assert app.tray_status == "retrying" and {"power watcher", "Bad"} <= set(app.optional_failures)
    exit_event.set()
    thread.join(5)
    assert result["code"] == 0 and manager.calls == ["start", "shutdown"]


def test_unexpected_exception_in_the_application_is_logged_and_shutdown_still_runs(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(bootstrap, "CRASH_LOG", tmp_path / "crash.log")
    caplog.set_level(logging.INFO)

    class Boom(FakeManager):
        def start(self):
            raise RuntimeError("engine factory exploded")

    manager, exit_event = Boom(), threading.Event()
    app = JarvisApplication(manager, exit_event)
    # a failing voice runtime is optional: JARVIS keeps running degraded ...
    exit_event.set()
    assert app.run() == 0 and "voice runtime" in app.optional_failures
    # ... but a genuine bug in the application itself is logged with a stack trace and a crash record, and everything is still released
    class Bus:
        def publish(self, *a, **k):
            if a and a[0].value == "system_start":
                raise ValueError("bus exploded")

    app2 = JarvisApplication(FakeManager(), threading.Event(), bus=Bus())
    assert app2.run() == 1
    text = caplog.text
    assert "JARVIS_UNEXPECTED_EXIT SHUTDOWN_REASON=startup_failure" in text and "ValueError" in text and "Traceback" in text
    assert "ValueError: bus exploded" in (tmp_path / "crash.log").read_text(encoding="utf-8")


def test_single_instance():
    name = f"Local\\JARVIS_Test_{uuid.uuid4().hex}"
    first, second = SingleInstanceGuard(name), SingleInstanceGuard(name)
    assert not SingleInstanceGuard.is_running(name)
    assert first.acquire() is True and SingleInstanceGuard.is_running(name)
    assert second.acquire() is False                                             # a second runtime cannot take the same login session's instance
    first.release()
    assert not SingleInstanceGuard.is_running(name) and second.acquire() is True  # a legitimate restart works
    second.release()


def test_scoped_helper_only_suffixes_when_something_asks_for_it():
    """`_scoped()` is pure and touches no OS object, so this needs no fake names and can never collide with a real running JARVIS."""
    from desktop.launcher.single_instance import _scoped

    base = "Local\\JARVIS_Runtime"
    assert _scoped(base, None) == base                                          # a real launch: no explicit id, and (in this process) no env var either
    assert _scoped(base, "abc") == f"{base}_abc"                                # an explicit id always wins
    assert _scoped(base, "") == base                                            # an empty id is "no scoping", not a literal "_" suffix


def test_instance_id_env_var_scopes_a_guard_without_an_explicit_id(monkeypatch):
    from desktop.launcher.single_instance import _scoped

    monkeypatch.delenv("JARVIS_INSTANCE_ID", raising=False)
    assert SingleInstanceGuard()._name == "Local\\JARVIS_Runtime"               # no env var: the real, shared name
    monkeypatch.setenv("JARVIS_INSTANCE_ID", "test-abc123")
    assert SingleInstanceGuard()._name == "Local\\JARVIS_Runtime_test-abc123"
    assert SingleInstanceGuard()._name == _scoped("Local\\JARVIS_Runtime", None)  # picked up from the environment, same as an explicit id would


def test_instance_id_isolates_a_diagnostic_run_from_a_same_process_real_instance(monkeypatch):
    """Regression for a real incident: scripts/e2e_launcher_check.py's `--stop` cleanup used the DEFAULT (unscoped) mutex/exit-signal names, so
    running it while a person's real JARVIS was up sent that real instance a global `Local\\JARVIS_Exit` signal and shut it down -- not the
    throwaway test instance it had started. `JARVIS_INSTANCE_ID` must give a caller (test harness, a second checkout) its own private namespace.
    A unique fake base name stands in for the production one here, so this test cannot collide with an actual JARVIS running on this machine
    (which is exactly the bug being fixed: the real code path uses the true default names, verified by the two tests above)."""
    from desktop.launcher.single_instance import ExitSignal, _scoped

    base_mutex, base_exit = f"Local\\JARVIS_Test_{uuid.uuid4().hex}", f"Local\\JARVIS_TestExit_{uuid.uuid4().hex}"
    real = SingleInstanceGuard(name=base_mutex)                                 # stands in for "the real, already-running JARVIS" (no instance id)
    real_exit = ExitSignal(name=base_exit)
    scoped_id = f"test-{uuid.uuid4().hex[:8]}"
    scoped = SingleInstanceGuard(name=_scoped(base_mutex, scoped_id))           # what a diagnostic run with JARVIS_INSTANCE_ID set would construct
    scoped_exit = ExitSignal(name=_scoped(base_exit, scoped_id))
    assert real._name != scoped._name

    caught: list[str] = []
    assert real_exit.wait_in_thread(lambda: caught.append("real"))
    assert scoped_exit.wait_in_thread(lambda: caught.append("scoped"))
    try:
        assert real.acquire() is True                                           # simulates the real, already-running JARVIS
        assert scoped.acquire() is True                                         # the scoped diagnostic run does not collide with it

        assert ExitSignal.send(name=_scoped(base_exit, scoped_id)) is True      # the scoped run's own `--stop`
        deadline = time.monotonic() + 2
        while not caught and time.monotonic() < deadline:
            time.sleep(0.02)
        assert caught == ["scoped"]                                             # only the scoped listener fired -- the real one was never touched
    finally:
        scoped.release()
        real.release()
        scoped_exit.close()
        real_exit.close()


def test_duplicate_start_prevention(monkeypatch, caplog):
    """Windows auto-start + a manual start: the second copy leaves the running one alone and ends with exit code 0 (Task Scheduler must not 'restart on failure')."""
    caplog.set_level(logging.INFO)

    class Taken:
        def acquire(self):
            return False

        def release(self):
            raise AssertionError("never acquired")

    monkeypatch.setattr(cli, "SingleInstanceGuard", Taken)
    monkeypatch.setattr(cli, "_build_application", lambda *a, **k: (_ for _ in ()).throw(AssertionError("a second runtime must not be built")))
    monkeypatch.setattr(cli, "configure_logging", lambda *a, **k: None)
    assert cli.main(["--startup-source", "windows"]) == 0
    assert "JARVIS_ALREADY_RUNNING" in caplog.text and "STARTUP_SOURCE=windows" in caplog.text


# ============================================ shutdown =========================================================================================

def test_graceful_shutdown(caplog):
    caplog.set_level(logging.INFO)
    log, exit_event = [], threading.Event()

    class Svc:
        def __init__(self, n):
            self.n = n

        def start(self):
            log.append(f"start {self.n}")

        def stop(self):
            log.append(f"stop {self.n}")

    coord = ExitCoordinator(exit_event)
    manager = FakeManager()
    app = JarvisApplication(manager, exit_event, tray=FakeTray(), power=FakePower(), background=[Svc("a"), Svc("b")], coordinator=coord, cleanup=[lambda: log.append("cleanup")])
    thread, result = run_app(app, exit_event, 0.6)
    coord.request("user_exit", "tray Exit")
    thread.join(5)
    assert result["code"] == 0 and log == ["start a", "start b", "stop b", "stop a", "cleanup"] and manager.calls == ["start", "shutdown"]
    assert "JARVIS_SHUTDOWN_REQUESTED SHUTDOWN_REASON=user_exit DETAIL=tray Exit" in caplog.text
    assert coord.reason == "user_exit"


def test_first_shutdown_reason_wins(caplog):
    caplog.set_level(logging.INFO)
    coord = ExitCoordinator()
    assert coord.request("windows_shutdown", "system shutdown or restart") is True
    assert coord.request("user_exit") is False and coord.reason == "windows_shutdown" and coord.is_set()
    assert caplog.text.count("JARVIS_SHUTDOWN_REQUESTED") == 1


def test_ctrl_c_shutdown():
    coord = ExitCoordinator()
    previous = cli._install_signal_handlers(coord)
    try:
        signal.raise_signal(signal.SIGINT)
        assert coord.is_set() and coord.reason == "ctrl_c"
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)
    assert reason_for_console_event(0)[0] == "ctrl_c" and reason_for_console_event(1)[0] == "ctrl_c"
    assert reason_for_console_event(2)[0] == "user_exit"


def test_windows_shutdown_reasons():
    assert reason_for_console_event(6) == ("windows_shutdown", "system shutdown")
    assert reason_for_console_event(5) == ("windows_shutdown", "user logoff")
    assert reason_for_end_session(0)[0] == "windows_shutdown" and "logoff" in reason_for_end_session(ENDSESSION_LOGOFF)[1]


@needs_windows
def test_windows_shutdown():
    """A REAL Win32 top-level window receives WM_QUERYENDSESSION / WM_ENDSESSION exactly as Windows sends them at shutdown; JARVIS treats that as a stop request."""
    import ctypes

    user32 = ctypes.windll.user32
    user32.SendMessageW.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_size_t, ctypes.c_ssize_t]
    user32.SendMessageW.restype = ctypes.c_ssize_t
    coord = ExitCoordinator()
    watcher = SessionEndWatcher(coord.request, end_wait_seconds=0.3)
    assert watcher.start() and watcher.hwnd
    try:
        assert user32.SendMessageW(watcher.hwnd, WM_QUERYENDSESSION, 0, 0) == 1        # JARVIS never vetoes a shutdown
        assert not coord.is_set()                                                       # asking is not ending
        user32.SendMessageW(watcher.hwnd, WM_ENDSESSION, 0, 0)                          # the shutdown was cancelled by someone else: nothing happens
        assert not coord.is_set()
        watcher.notify_shutdown_complete()
        user32.SendMessageW(watcher.hwnd, WM_ENDSESSION, 1, 0)                          # the session is really ending
        assert coord.is_set() and coord.reason == "windows_shutdown" and "shutdown" in coord.detail
    finally:
        watcher.stop()


@needs_windows
def test_windows_shutdown_ends_the_application_cleanly_with_all_steps():
    import ctypes

    user32 = ctypes.windll.user32
    user32.SendMessageW.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_size_t, ctypes.c_ssize_t]
    user32.SendMessageW.restype = ctypes.c_ssize_t
    exit_event = threading.Event()
    coord = ExitCoordinator(exit_event)
    manager = FakeManager()
    app = JarvisApplication(manager, exit_event, tray=FakeTray(), power=FakePower(), coordinator=coord)
    watcher = SessionEndWatcher(coord.request, end_wait_seconds=3.0)
    assert watcher.start()
    thread, result = run_app(app, exit_event, 0.5)
    threading.Thread(target=lambda: user32.SendMessageW(watcher.hwnd, WM_ENDSESSION, 1, 0), daemon=True).start()
    thread.join(6)
    watcher.notify_shutdown_complete()
    watcher.stop()
    assert result.get("code") == 0 and manager.calls == ["start", "shutdown"] and coord.reason == "windows_shutdown"     # a shutdown is a clean exit, not a crash


# ============================================ recovery =========================================================================================

def test_microphone_startup_retry_uses_bounded_backoff():
    from tests.voice_helpers import RecordingTTS, ScriptedMic, ScriptedOutput, ScriptedSTT
    from voice.engine import VoiceEngine
    from voice.exceptions import AudioDeviceError
    from tests.voice_helpers import FakeConversation, ScriptedWake

    sleeps = []
    mic = ScriptedMic(open_failures=[AudioDeviceError("audio subsystem not ready")] * 9)
    engine = VoiceEngine(ScriptedWake(), ScriptedSTT(), FakeConversation(), RecordingTTS(), mic, ScriptedOutput(), 16000, 1.0, sleep=sleeps.append, mic_retry_seconds=2.0)
    assert engine.run_once(lambda: mic.opens >= 1 and mic.reads > 3) is None
    assert sleeps[:6] == [2.0, 4.0, 8.0, 16.0, 30.0, 30.0] and max(sleeps) == 30.0          # backs off, never a tight loop, never longer than 30 s
    assert mic.opens == 1                                                                   # ...and the moment the microphone is available it is used
    assert engine.status.last_error is None                                                  # recovered: no lingering error once it opened


def test_optional_service_failure_does_not_kill_runtime():
    class Down:
        def start(self):
            raise ConnectionError("Groq unreachable")

        def stop(self):
            pass

    exit_event = threading.Event()
    app = JarvisApplication(FakeManager(), exit_event, background=[Down(), Down()])
    thread, result = run_app(app, exit_event, 0.8)
    assert thread.is_alive() and app.running
    exit_event.set()
    thread.join(5)
    assert result["code"] == 0


def test_runtime_recovery_tray_keeper_retries_until_the_taskbar_exists():
    class LateTaskbar(FakeTray):
        def __init__(self):
            super().__init__()
            self.attempts = 0

        def start(self):
            self.attempts += 1
            if self.attempts < 4:
                raise TrayError("Shell_TrayWnd does not exist yet")
            self.calls.append("start")

    tray, stop, waits = LateTaskbar(), threading.Event(), []
    keeper = TrayKeeper(tray, stop, sleep=lambda seconds: waits.append(seconds) or False)
    started = keeper.start()
    assert started == "retrying"                                                 # the first attempt failed; JARVIS did not
    keeper._thread.join(3)
    assert keeper.status == "running" and tray.calls == ["start"] and waits == [2.0, 4.0, 8.0]          # bounded, growing delays
    stop.set()


def test_runtime_recovery_voice_runtime_is_restarted_by_the_supervisor():
    from backend.core.recovery import BackoffPolicy, SupervisedService, Supervisor

    state, restarts, now = {"v": "error"}, [], [0.0]
    sup = Supervisor(clock=lambda: now[0])
    sup.add(SupervisedService("voice_runtime", check=lambda: state["v"] != "error", restart=lambda: (restarts.append(1), state.update(v="running")),
                              policy=BackoffPolicy(1.0, 2.0, 60.0, 5, 300.0)))
    sup.tick()
    assert restarts == [1] and (sup.tick() is None) and state["v"] == "running"


def test_tray_keeper_is_disabled_without_a_tray_and_stops_with_the_application():
    keeper = TrayKeeper(None, threading.Event())
    assert keeper.start() == "disabled"
    stop = threading.Event()
    k2 = TrayKeeper(FakeTray(fail=True), stop, sleep=lambda s: stop.wait(s))
    assert k2.start() == "retrying"
    stop.set()
    k2.stop()
    assert k2._thread is None


# ============================================ crash logging ====================================================================================

def test_crash_records_are_redacted_and_carry_the_context(tmp_path, monkeypatch):
    monkeypatch.setattr(bootstrap, "CRASH_LOG", tmp_path / "crash.log")
    try:
        raise RuntimeError("bad key gsk_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789 in the request")
    except RuntimeError as exc:
        bootstrap.write_crash("unexpected_exception", exc, startup_source="windows", state="running")
    text = (tmp_path / "crash.log").read_text(encoding="utf-8")
    assert "unexpected_exception" in text and "startup_source=windows" in text and "runtime_state=running" in text and "RuntimeError" in text and "Traceback" in text
    assert "gsk_ABCDEF" not in text and "[REDACTED]" in text
    assert f"pid={os.getpid()}" in text and "ppid=" in text and "cwd=" in text


def test_uncaught_thread_exceptions_are_logged_not_lost(tmp_path, monkeypatch):
    monkeypatch.setattr(bootstrap, "CRASH_LOG", tmp_path / "crash.log")
    monkeypatch.setattr(bootstrap, "_installed", False)
    old_hooks = (sys.excepthook, threading.excepthook)
    try:
        bootstrap.install_excepthooks("windows")
        t = threading.Thread(target=lambda: (_ for _ in ()).throw(ValueError("worker died")), name="unit-test-worker")
        t.start()
        t.join()
    finally:
        sys.excepthook, threading.excepthook = old_hooks
        monkeypatch.setattr(bootstrap, "_installed", True)
    text = (tmp_path / "crash.log").read_text(encoding="utf-8")
    assert "unexpected_exception_in_thread:unit-test-worker" in text and "ValueError: worker died" in text


def test_entry_turns_an_unexpected_exit_into_a_logged_failure(tmp_path, monkeypatch):
    from desktop.launcher import entry

    monkeypatch.setattr(bootstrap, "CRASH_LOG", tmp_path / "crash.log")
    monkeypatch.setattr(cli, "main", lambda argv=None: (_ for _ in ()).throw(RuntimeError("kaboom")))
    monkeypatch.setattr(bootstrap, "install_excepthooks", lambda *a, **k: None)
    assert entry.run(["--startup-source", "windows"]) == 1
    assert "kaboom" in (tmp_path / "crash.log").read_text(encoding="utf-8")
    monkeypatch.setattr(cli, "main", lambda argv=None: 0)
    assert entry.run([]) == 0
    assert entry._source_from(["--startup-source", "task"]) == "task" and entry._source_from(["--startup-source=tray"]) == "tray" and entry._source_from([]) == "manual"


def test_startup_record_has_every_diagnostic_and_no_secret(caplog):
    from backend.core.config import get_settings

    caplog.set_level(logging.INFO)
    settings = get_settings()
    cli._log_startup(settings, "windows")
    text = caplog.text
    for key in ("JARVIS_STARTING", "STARTUP_SOURCE=windows", "PROJECT_ROOT=", "PYTHON_EXECUTABLE=", f"PID={os.getpid()}", "PARENT_PID=", "ENVIRONMENT=", "CONFIG_STATUS=ok", "LLM_STATUS=",
                "DATABASE_STATUS="):
        assert key in text, key
    key = settings.GROQ_API_KEY.get_secret_value()
    if key:
        assert key not in text


# ============================================ registration =====================================================================================

class _Reg:
    def __init__(self):
        self.values = {}

    def get(self, name):
        return self.values.get(name)

    def set(self, name, value):
        self.values[name] = value

    def delete(self, name):
        return self.values.pop(name, None) is not None


def test_exactly_one_startup_mechanism_is_ever_active(tmp_path):
    reg, tasks, calls = _Reg(), {"on": False}, []

    def runner(command, env):
        script = command[-1]
        calls.append(script[:30])
        if "Register-ScheduledTask" in script:
            tasks["on"] = True
        elif "Unregister-ScheduledTask" in script:
            tasks["on"] = False
        elif "CreateShortcut" in script:
            Path(env["JARVIS_LNK"]).write_text("lnk")

    m = StartupManager(startup_dir=tmp_path / "S", project_root=tmp_path, registry=reg, runner=runner, query=lambda c, e: "yes" if tasks["on"] else "no")
    m.enable("shortcut")
    m.enable("run")                                                             # non-exclusive (legacy) enabling can stack: the status warns about it
    assert m.status()["duplicate"] is True and set(m.status()["methods"]) == {"shortcut", "run"}
    m.enable("task", exclusive=True)                                            # what --enable-startup does: everything else is removed first
    st = m.status()
    assert st["methods"] == ["task"] and st["duplicate"] is False and reg.values == {} and not (tmp_path / "S" / "JARVIS.lnk").exists()
    m.enable("run", exclusive=True)
    assert m.status()["methods"] == ["run"] and tasks["on"] is False and reg.values["JARVIS"] == m.command_line()
    assert m.disable() is True and m.status()["enabled"] is False and m.disable() is False
    with pytest.raises(StartupIntegrationError, match="unknown startup method"):
        m.enable("registry-hack")


def test_the_scheduled_task_definition_is_what_the_docs_promise():
    from desktop.launcher import startup

    script = startup._CREATE_TASK_SCRIPT
    for needle in ("-AtLogOn", "PT20S", "-MultipleInstances IgnoreNew", "-RestartCount 5", "-RestartInterval (New-TimeSpan -Minutes 1)", "-ExecutionTimeLimit (New-TimeSpan -Seconds 0)",
                   "-RunLevel Limited", "-WorkingDirectory $env:JARVIS_TASK_CWD", "-LogonType Interactive"):
        assert needle in script, needle
    assert "Highest" not in script and "RunAs" not in script and "-Verb" not in script       # never elevated


@needs_windows
def test_real_run_key_registration_uses_a_scratch_key():
    import winreg

    key = rf"Software\JarvisTest\Run{uuid.uuid4().hex[:8]}"
    reg = _WinRegistry(key)
    m = StartupManager(registry=reg, query=lambda c, e: "no", run_value="JARVIS_TEST")
    try:
        m.enable("run")
        assert reg.get("JARVIS_TEST") == m.command_line() and m.status()["methods"] == ["run"]
        assert m.disable() is True and reg.get("JARVIS_TEST") is None
    finally:
        try:
            winreg.DeleteKey(winreg.HKEY_CURRENT_USER, key)
            winreg.DeleteKey(winreg.HKEY_CURRENT_USER, r"Software\JarvisTest")
        except OSError:
            pass
    assert RUN_KEY.endswith(r"CurrentVersion\Run")


@needs_windows
def test_real_task_scheduler_registration_without_administrator_rights():
    name = f"JARVIS_TEST_{uuid.uuid4().hex[:8]}"
    m = StartupManager(registry=_Reg(), task_name=name, run_value=name)
    try:
        try:
            m.enable("task")
        except StartupIntegrationError as exc:
            pytest.skip(f"Task Scheduler registration is not available here: {exc}")
        assert m.task_enabled() and m.status()["methods"] == ["task"]
    finally:
        m.disable()
    assert not m.task_enabled()


# ============================================ voice: nothing at boot ============================================================================

def test_boot_does_not_activate_voice():
    from tests.voice.test_wake_session_sleep import ScoredWake, build, silence
    from tests.voice_helpers import ScriptedMic

    mic = ScriptedMic(*silence(400))
    engine = build(mic=mic, wake=ScoredWake())
    assert engine.run_once(lambda: mic.reads >= 300) is None
    assert engine.status.activations == 0 and engine._last_wake is None and engine.status.snapshot()["conversation"]["active"] is False
    assert engine.state == "waiting"                                              # STARTING -> WAITING, never LISTENING without a real wake phrase


def test_boot_does_not_speak_yes():
    from tests.voice.test_wake_session_sleep import ScoredWake, build, silence
    from tests.voice_helpers import RecordingTTS, ScriptedMic

    tts, mic = RecordingTTS(), ScriptedMic(*silence(400))
    engine = build(mic=mic, wake=ScoredWake(), tts=tts)
    engine.run_once(lambda: mic.reads >= 300)
    assert tts.spoken == []
    # a manual request left over from BEFORE boot (a click while paused, an API call nobody consumed) is dropped, never replayed as a surprise "Yes?"
    clock = {"t": 0.0}
    tts2, mic2 = RecordingTTS(), ScriptedMic(*silence(200))
    e2 = build(mic=mic2, tts=tts2, clock=lambda: clock["t"])
    e2.request_activation()
    clock["t"] += 60
    e2.run_once(lambda: mic2.reads >= 100)
    assert tts2.spoken == [] and e2.status.activations == 0


def test_every_source_of_the_word_yes_is_reachable_only_through_a_validated_wake():
    src = (ROOT / "voice" / "engine.py").read_text(encoding="utf-8")
    # Stored once; spoken from exactly two places, both gated on a validated wake: the initial wake in `_run_once`, and a
    # re-said wake phrase inside `_converse` (only reachable once that conversation is already open).
    assert src.count("self._activation_reply") == 3 and "ACTIVATION_WITHOUT_VALIDATED_WAKE" in src
    for path in ("desktop", "agent", "backend", "autonomy", "workflows", "browser"):
        for f in (ROOT / path).rglob("*.py"):
            text = f.read_text(encoding="utf-8", errors="ignore")
            assert '"Yes?"' not in text and "'Yes?'" not in text, f       # no other module can speak the acknowledgement


def test_wake_hey_jarvis_and_jarvis_activate_once_each():
    from tests.voice.test_wake_session_sleep import STRICT, ScoredWake, UTT, build, silence
    from tests.voice_helpers import RecordingTTS, ScriptedMic, ScriptedSTT

    for phrase in ("Hey JARVIS", "JARVIS"):
        tts = RecordingTTS()
        engine = build(mic=ScriptedMic(*silence(3), *UTT), wake=ScoredWake(0.0, 0.95, 0.99, 1.0), stt=ScriptedSTT(phrase, "hello"), tts=tts, cfg=STRICT, timeout=3.0)
        engine.run_once()
        assert tts.spoken.count("Yes?") == 1 and engine.status.activations == 1


def test_unrelated_speech_no_activation():
    from tests.voice.test_wake_session_sleep import STRICT, ScoredWake, build, silence
    from tests.voice_helpers import RecordingTTS, ScriptedMic, ScriptedSTT

    tts, mic = RecordingTTS(), ScriptedMic(*silence(300))
    engine = build(mic=mic, wake=ScoredWake(0.0, 0.95, 0.99, 1.0), stt=ScriptedSTT("what a lovely day it is"), tts=tts, cfg=STRICT)
    engine.run_once(lambda: mic.reads >= 200)
    assert tts.spoken == [] and engine.status.activations == 0


def test_sleep_command_idle_timeout_and_wake_after_sleep():
    from tests.voice.test_wake_session_sleep import ScoredWake, UTT, build, silence
    from tests.voice_helpers import FakeConversation, RecordingTTS, ScriptedMic, ScriptedSTT

    tts, conv = RecordingTTS(), FakeConversation("ok")
    mic = ScriptedMic(*silence(3), *UTT, *silence(20), *UTT, *silence(100))
    wake = ScoredWake(0.0, 0.92, 0.95)
    engine = build(mic=mic, wake=wake, stt=ScriptedSTT("hi", "JARVIS sleep", "hello again"), conv=conv, tts=tts, timeout=30.0)
    engine.run_once()
    assert engine.status.snapshot()["conversation"]["sleep_reason"] == "command" and conv.received == ["hi"]          # test_sleep_command
    mic.push(*UTT, *silence(2000))                                              # (silence(100) is left over from the first script)
    wake.scores += [0.0] * 3 + [0.9, 0.95]
    engine._gate._blocked_until = 0.0
    engine.run_once()                                                                                               # test_wake_after_sleep
    assert tts.spoken.count("Yes?") == 2 and conv.received == ["hi", "hello again"]
    assert engine.status.snapshot()["conversation"]["sleep_reason"] == "timeout"                                     # test_idle_timeout (30 s of audio)
    assert tts.spoken[-1] == "ok"                                                                                    # nothing was spoken by the timeout itself


# ============================================ status script ====================================================================================

def test_status_script_is_safe_to_run_repeatedly_and_reports_stopped():
    """scripts/jarvis_status.py must work with JARVIS stopped (the normal state of the test suite) and never start anything."""
    for _ in range(2):
        out = subprocess.run([sys.executable, str(ROOT / "scripts" / "jarvis_status.py"), "--json"], capture_output=True, text=True, timeout=60)
        assert out.returncode in (0, 1), out.stderr[-300:]
        data = json.loads(out.stdout)
        assert data["process"] in ("RUNNING", "STOPPED") and "startup" in data and "project_root" in data
