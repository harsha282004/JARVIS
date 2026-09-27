"""Process bootstrap and crash logging that do not depend on the current directory, the console, VS Code or an activated virtual environment.

Windows starts a background application with whatever working directory and stdio it likes (a Run entry starts in System32 or the user profile; a
Task Scheduler action starts in System32 unless told otherwise; `pythonw.exe` has NO stdout/stderr at all). A traceback printed by a windowless process goes
nowhere, so before this module a failure that happened before the normal logging was configured made JARVIS "just disappear". Everything here therefore:

  * finds the project root from THIS FILE's location, puts it on `sys.path` and makes it the working directory (relative model/state paths in .env resolve);
  * gives libraries a harmless stdout/stderr when there is none;
  * writes every unexpected top-level exception (main thread and background threads) with its stack trace to `logs/jarvis-crash.log` and, once logging
    exists, to `logs/jarvis.log` - redacted, never containing configuration values.
"""

import os
import platform
import sys
import threading
import time
import traceback
from pathlib import Path
from types import TracebackType

PROJECT_ROOT = Path(__file__).resolve().parents[2]
CRASH_LOG = PROJECT_ROOT / "logs" / "jarvis-crash.log"

_installed = False


def prepare_process() -> Path:
    """Idempotent. Returns the project root."""
    root = str(PROJECT_ROOT)
    if root not in sys.path:
        sys.path.insert(0, root)
    try:
        os.chdir(PROJECT_ROOT)
    except OSError:
        pass
    if sys.stdout is None:  # pythonw.exe
        sys.stdout = open(os.devnull, "w")  # noqa: SIM115 - lives for the whole process
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w")  # noqa: SIM115
    os.environ.setdefault("JARVIS_PROJECT_ROOT", root)
    return PROJECT_ROOT


def _redact(text: str) -> str:
    try:
        from backend.core.redaction import redact

        return redact(text)
    except Exception:  # noqa: BLE001 - the crash log must be writable even when the project cannot be imported
        return text


def write_crash(kind: str, exc: BaseException | None = None, *, startup_source: str = "", state: str = "") -> None:
    """Append one crash record. Never raises. Contains: time, kind, pid/parent pid, executable, cwd, exception type/message and stack trace (redacted)."""
    try:
        CRASH_LOG.parent.mkdir(parents=True, exist_ok=True)
        lines = [f"=== {time.strftime('%Y-%m-%d %H:%M:%S')} {kind} pid={os.getpid()} ppid={os.getppid()} startup_source={startup_source or '-'} runtime_state={state or '-'}",
                 f"executable={sys.executable} cwd={os.getcwd()} python={platform.python_version()}"]
        if exc is not None:
            lines.append(f"exception={type(exc).__name__}: {_redact(str(exc))[:500]}")
            lines.append(_redact("".join(traceback.format_exception(type(exc), exc, exc.__traceback__))))
        with CRASH_LOG.open("a", encoding="utf-8") as handle:
            handle.write("\n".join(lines) + "\n")
    except Exception:  # noqa: BLE001
        pass


def install_excepthooks(startup_source: str = "") -> None:
    """Uncaught exceptions on any thread are logged with a stack trace instead of vanishing (a windowless process has no console to print them on)."""
    global _installed
    if _installed:
        return
    _installed = True

    def log_it(kind: str, exc: BaseException) -> None:
        write_crash(kind, exc, startup_source=startup_source)
        try:
            from backend.core.logging import get_logger

            get_logger("desktop.launcher").error("%s: %s: %s", kind, type(exc).__name__, _redact(str(exc))[:300], exc_info=(type(exc), exc, exc.__traceback__))
        except Exception:  # noqa: BLE001
            pass

    def main_hook(exc_type: type[BaseException], exc: BaseException, tb: TracebackType | None) -> None:
        if issubclass(exc_type, KeyboardInterrupt):
            return
        exc.__traceback__ = tb
        log_it("unexpected_exception", exc)

    def thread_hook(args: threading.ExceptHookArgs) -> None:
        if args.exc_value is not None and not issubclass(args.exc_type, SystemExit):
            log_it(f"unexpected_exception_in_thread:{getattr(args.thread, 'name', '?')}", args.exc_value)

    sys.excepthook = main_hook
    threading.excepthook = thread_hook
