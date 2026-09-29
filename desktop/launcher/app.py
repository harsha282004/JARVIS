"""JarvisApplication: startup/shutdown sequencing for the Windows runtime.

Wires the tray, the sleep/resume watcher, the RuntimeManager and the background services together and keeps the process alive until
an exit is requested (tray Exit, `--stop`, Ctrl+C, a console-close signal or a Windows logoff/shutdown). It contains no voice or reasoning logic.

Startup order:  tray -> power watcher -> reminder scheduler -> voice runtime -> background services (health, supervisor, intelligence)
Shutdown order: background services (reverse) -> scheduler -> power watcher -> voice runtime -> tray -> cleanup hooks (database pool)

Lifetime rules (each has a regression test):
  * The main thread blocks on the exit event; no background thread is what keeps the process alive, and nothing falls through to the end of `main()`.
  * Only an explicit exit request ends the process, and every request carries a REASON (user_exit, ctrl_c, windows_shutdown, ...) that is logged
    (`JARVIS_SHUTDOWN_REQUESTED SHUTDOWN_REASON=...`), so "why did it stop?" is always answerable from the log.
  * Every optional part is isolated: a tray that cannot be created yet (Explorer's taskbar is not ready right after logon), a background service, the power
    watcher, the scheduler or the voice runtime failing to start is logged, marked degraded and retried where sensible; it never ends the process.
  * An unexpected exception in the application itself is logged with its stack trace (redacted) and the crash log, then shutdown still releases everything.
Shutdown is idempotent and always attempts every step, so no thread, stream or connection is left behind.
"""

import threading
import time
import traceback
from collections.abc import Callable, Sequence
from typing import Protocol

from agent.tasks.scheduler import ReminderScheduler
from backend.core.events import EventBus, SystemEvent
from backend.core.logging import get_logger, log_event
from backend.core.metrics import metrics
from desktop.runtime.manager import RuntimeManager
from desktop.runtime.power import SleepResumeWatcher
from desktop.tray.tray import TrayController, TrayError

logger = get_logger(__name__)

_EXIT_POLL_SECONDS = 0.5
SHUTDOWN_REASONS = ("user_exit", "ctrl_c", "windows_shutdown", "fatal_error", "startup_failure", "unexpected_exception", "restart")


class Lifecycle(Protocol):
    def start(self) -> None: ...

    def stop(self) -> None: ...


class ExitCoordinator:
    """Turns "something asked JARVIS to stop" into an event plus a recorded reason. The first request wins; later ones are ignored (and not logged as new reasons)."""

    def __init__(self, event: threading.Event | None = None):
        self.event = event or threading.Event()
        self._lock = threading.Lock()
        self._reason: str | None = None
        self._detail = ""

    def request(self, reason: str, detail: str = "") -> bool:
        """True if this call recorded the reason."""
        with self._lock:
            first = self._reason is None
            if first:
                self._reason, self._detail = reason, detail
        if first:
            logger.info("JARVIS_SHUTDOWN_REQUESTED SHUTDOWN_REASON=%s%s", reason, f" DETAIL={detail}" if detail else "")
        self.event.set()
        return first

    @property
    def reason(self) -> str | None:
        return self._reason

    @property
    def detail(self) -> str:
        return self._detail

    def is_set(self) -> bool:
        return self.event.is_set()


class TrayKeeper:
    """Starts the tray icon without ever being fatal. Right after logon Explorer's taskbar may not exist yet, so `tray.start()` can fail for a few seconds;
    that used to end the whole process. Now a failure is logged, the status becomes `retrying`, and a background thread retries with capped backoff until the
    icon comes up (or JARVIS exits). The voice runtime, API and scheduler do not wait for it."""

    DELAYS = (2.0, 4.0, 8.0, 15.0, 30.0)

    def __init__(self, tray: TrayController | None, stop: threading.Event, sleep: Callable[[float], bool] | None = None):
        self._tray = tray
        self._stop = stop
        self._wait = sleep or (lambda seconds: stop.wait(seconds))
        self._thread: threading.Thread | None = None
        self.status = "disabled" if tray is None else "starting"
        self.attempts = 0
        self.last_error = ""

    def start(self) -> str:
        if self._tray is None:
            return self.status
        if self._try():
            return self.status
        self.status = "retrying"
        self._thread = threading.Thread(target=self._retry_loop, name="jarvis-tray-keeper", daemon=True)
        self._thread.start()
        return "retrying"      # what the FIRST attempt found (the retry thread may already have succeeded by the time the caller looks)

    def _try(self) -> bool:
        self.attempts += 1
        try:
            self._tray.start()  # type: ignore[union-attr]
        except TrayError as exc:
            self.last_error = str(exc)[:200]
            logger.warning("TRAY_START_FAILED attempt=%d (%s); JARVIS keeps running and will retry", self.attempts, self.last_error)
            return False
        except Exception as exc:  # noqa: BLE001 - any tray backend error is non-fatal
            self.last_error = f"{type(exc).__name__}"
            logger.warning("TRAY_START_FAILED attempt=%d (%s); JARVIS keeps running and will retry", self.attempts, self.last_error)
            return False
        self.status = "running"
        logger.info("TRAY_STATUS=running attempts=%d", self.attempts)
        return True

    def _retry_loop(self) -> None:
        index = 0
        while not self._stop.is_set():
            delay = self.DELAYS[min(index, len(self.DELAYS) - 1)]
            index += 1
            if self._wait(delay) or self._stop.is_set():
                return
            if self._try():
                return

    def stop(self) -> None:
        thread, self._thread = self._thread, None
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=2.0)


class JarvisApplication:
    def __init__(
        self,
        manager: RuntimeManager,
        exit_event: threading.Event,
        tray: TrayController | None = None,
        power: SleepResumeWatcher | None = None,
        scheduler: ReminderScheduler | None = None,
        *,
        background: Sequence[Lifecycle] = (),
        bus: EventBus | None = None,
        cleanup: Sequence[Callable[[], None]] = (),
        started_at: float | None = None,
        coordinator: ExitCoordinator | None = None,
        startup_source: str = "manual",
        report: Callable[[dict], None] | None = None,
    ):
        self._manager = manager
        self._exit_event = exit_event
        self._coordinator = coordinator
        self._tray = tray
        self._tray_keeper = TrayKeeper(tray, exit_event)
        self._power = power
        self._scheduler = scheduler
        self._background = list(background)
        self._bus = bus
        self._cleanup = list(cleanup)
        self._started_at = started_at if started_at is not None else time.perf_counter()
        self._started: list[Lifecycle] = []
        self._shutdown_done = False
        self._startup_source = startup_source
        self._report = report
        self.running = False
        self.optional_failures: list[str] = []

    @property
    def tray_status(self) -> str:
        return self._tray_keeper.status

    @property
    def tray_health(self) -> dict:
        """The real tray state (TrayController.health()): distinguishes "TrayKeeper thinks the tray started" from
        "Windows has actually registered the icon" -- see desktop/tray/tray.py for why that distinction is real."""
        if self._tray is None:
            return {"controller": "disabled", "icon_created": False, "thread_alive": False, "icon_registered": None, "registered_at": None, "last_error": ""}
        health = self._tray.health()
        health["keeper_status"] = self._tray_keeper.status
        health["keeper_attempts"] = self._tray_keeper.attempts
        return health

    def request_exit(self, reason: str = "user_exit", detail: str = "") -> None:
        if self._coordinator is not None:
            self._coordinator.request(reason, detail)
        else:
            self._exit_event.set()

    def run(self) -> int:
        """Start everything, block until exit is requested, then shut down. Returns an exit code (0 = clean exit requested; 1 = failed)."""
        code, reason = 0, "user_exit"
        try:
            self._tray_keeper.start()
            self._optional("power watcher", self._start_power)
            self._start_scheduler()
            self._optional("voice runtime", self._manager.start)
            self._start_background()
            elapsed_ms = (time.perf_counter() - self._started_at) * 1000
            metrics.observe("startup_ms", elapsed_ms)
            log_event(logger, "startup_complete", startup_ms=round(elapsed_ms), background_services=len(self._started), tray=self.tray_status,
                      degraded=",".join(self.optional_failures) or "none")
            if self._bus is not None:
                self._bus.publish(SystemEvent.SYSTEM_START)
            self.running = True
            logger.info("JARVIS_RUNTIME_RUNNING TRAY_STATUS=%s STARTUP_SOURCE=%s", self.tray_status, self._startup_source)
            logger.info("JARVIS runtime running")
            self._start_report_thread()
            while not self._exit_event.wait(_EXIT_POLL_SECONDS):
                pass
            reason = (self._coordinator.reason if self._coordinator is not None else None) or "user_exit"
            return 0
        except Exception as exc:  # noqa: BLE001 - an unexpected failure of the application itself: diagnosable, then a clean shutdown in `finally`
            code = 1
            reason = "unexpected_exception" if self.running else "startup_failure"
            self._log_failure(reason, exc)
            return code
        finally:
            if self._coordinator is None or self._coordinator.reason is None:
                logger.info("JARVIS_SHUTDOWN_REQUESTED SHUTDOWN_REASON=%s", reason)
            self._shutdown()

    def _log_failure(self, reason: str, exc: BaseException) -> None:
        logger.error("JARVIS_UNEXPECTED_EXIT SHUTDOWN_REASON=%s STARTUP_SOURCE=%s runtime_state=%s exception=%s: %s\n%s", reason, self._startup_source,
                     getattr(self._manager, "state", "?"), type(exc).__name__, str(exc)[:300], "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))[-2500:])
        try:
            from desktop.launcher.bootstrap import write_crash

            write_crash(reason, exc, startup_source=self._startup_source, state=str(getattr(self._manager, "state", "")))
        except Exception:  # noqa: BLE001
            pass

    def _optional(self, what: str, fn: Callable[[], object]) -> None:
        try:
            fn()
        except Exception as exc:  # noqa: BLE001 - an optional part failing marks JARVIS degraded; it never ends the process
            self.optional_failures.append(what)
            logger.error("Optional part %s could not start (%s); JARVIS keeps running degraded", what, type(exc).__name__, exc_info=True)

    def _start_power(self) -> None:
        if self._power is not None:
            self._power.start()

    def _start_scheduler(self) -> None:
        """Reminders and proactive checks are independent of the voice engine: a scheduler problem never stops JARVIS."""
        if self._scheduler is None:
            return
        try:
            self._scheduler.start()
        except Exception:  # noqa: BLE001
            self.optional_failures.append("scheduler")
            logger.exception("Reminder scheduler could not start; reminders will not fire")

    def _start_background(self) -> None:
        for service in self._background:
            try:
                service.start()
                self._started.append(service)
            except Exception as exc:  # noqa: BLE001 - an optional service failing must not stop JARVIS
                self.optional_failures.append(type(service).__name__)
                logger.error("Background service %s could not start (%s)", type(service).__name__, type(exc).__name__)

    def _start_report_thread(self) -> None:
        """One line per subsystem once the voice runtime has left STARTING (model loading takes a while): the startup record a support question needs."""
        if self._report is None:
            return

        def report() -> None:
            deadline = time.monotonic() + 180.0
            while not self._exit_event.is_set() and time.monotonic() < deadline:
                state = getattr(self._manager, "state", None)
                if str(getattr(state, "value", state)) != "starting":
                    break
                self._exit_event.wait(1.0)
            try:
                self._report({"tray": self.tray_status, "degraded": list(self.optional_failures)})
            except Exception:  # noqa: BLE001 - diagnostics must never disturb the runtime
                pass

        threading.Thread(target=report, name="jarvis-startup-report", daemon=True).start()

    def _shutdown(self) -> None:
        if self._shutdown_done:
            return
        self._shutdown_done = True
        self.running = False
        if self._bus is not None:
            try:
                self._bus.publish(SystemEvent.SYSTEM_SHUTDOWN)
            except Exception:  # noqa: BLE001
                pass
        self._tray_keeper.stop()
        for service in reversed(self._started):
            self._safely(service.stop, type(service).__name__)
        self._started.clear()
        if self._scheduler is not None:
            self._safely(self._scheduler.stop, "scheduler")
        if self._power is not None:
            self._safely(self._power.stop, "power watcher")
        self._safely(self._manager.shutdown, "voice runtime")
        if self._tray is not None:
            self._safely(self._tray.stop, "tray")
        for hook in self._cleanup:
            self._safely(hook, "cleanup")
        logger.info("JARVIS runtime stopped")

    @staticmethod
    def _safely(fn: Callable[[], None], what: str) -> None:
        try:
            fn()
        except Exception as exc:  # noqa: BLE001 - keep shutting down whatever else fails
            logger.error("Shutdown step %s failed (%s)", what, type(exc).__name__)
