"""JarvisApplication: startup/shutdown sequencing for the Windows runtime.

Wires the tray, the sleep/resume watcher, the RuntimeManager and the background services together and keeps the process alive until
an exit is requested (tray Exit, Ctrl+C, or a console-close signal). It contains no voice or reasoning logic.

Startup order:  tray -> power watcher -> reminder scheduler -> voice runtime -> background services (health, supervisor, intelligence)
Shutdown order: background services (reverse) -> scheduler -> power watcher -> voice runtime -> tray -> cleanup hooks (database pool)

Every optional part is isolated: a background service that fails to start or stop is logged and skipped, and the rest carry on.
Shutdown is idempotent and always attempts every step, so no thread, stream or connection is left behind.
"""

import threading
import time
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


class Lifecycle(Protocol):
    def start(self) -> None: ...

    def stop(self) -> None: ...


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
    ):
        self._manager = manager
        self._exit_event = exit_event
        self._tray = tray
        self._power = power
        self._scheduler = scheduler
        self._background = list(background)
        self._bus = bus
        self._cleanup = list(cleanup)
        self._started_at = started_at if started_at is not None else time.perf_counter()
        self._started: list[Lifecycle] = []
        self._shutdown_done = False

    def request_exit(self) -> None:
        self._exit_event.set()

    def run(self) -> int:
        """Start everything, block until exit is requested, then shut down. Returns an exit code."""
        try:
            if self._tray is not None:
                try:
                    self._tray.start()
                except TrayError as exc:
                    logger.error("Tray initialization failed: %s", exc)
                    return 1
            if self._power is not None:
                self._power.start()
            self._start_scheduler()
            self._manager.start()
            self._start_background()
            elapsed_ms = (time.perf_counter() - self._started_at) * 1000
            metrics.observe("startup_ms", elapsed_ms)
            log_event(logger, "startup_complete", startup_ms=round(elapsed_ms), background_services=len(self._started))
            if self._bus is not None:
                self._bus.publish(SystemEvent.SYSTEM_START)
            logger.info("JARVIS runtime running")
            while not self._exit_event.wait(_EXIT_POLL_SECONDS):
                pass
            return 0
        finally:
            self._shutdown()

    def _start_scheduler(self) -> None:
        """Reminders and proactive checks are independent of the voice engine: a scheduler problem never stops JARVIS."""
        if self._scheduler is None:
            return
        try:
            self._scheduler.start()
        except Exception:  # noqa: BLE001
            logger.exception("Reminder scheduler could not start; reminders will not fire")

    def _start_background(self) -> None:
        for service in self._background:
            try:
                service.start()
                self._started.append(service)
            except Exception as exc:  # noqa: BLE001 - an optional service failing must not stop JARVIS
                logger.error("Background service %s could not start (%s)", type(service).__name__, type(exc).__name__)

    def _shutdown(self) -> None:
        if self._shutdown_done:
            return
        self._shutdown_done = True
        if self._bus is not None:
            try:
                self._bus.publish(SystemEvent.SYSTEM_SHUTDOWN)
            except Exception:  # noqa: BLE001
                pass
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
