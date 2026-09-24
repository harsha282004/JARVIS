"""JarvisApplication: startup/shutdown sequencing for the Windows runtime.

Wires the tray, the sleep/resume watcher and the RuntimeManager together and
keeps the process alive until an exit is requested (tray Exit, Ctrl+C, or a
console-close signal). It contains no voice or reasoning logic.
"""

import threading

from backend.core.logging import get_logger
from agent.tasks.scheduler import ReminderScheduler
from desktop.runtime.manager import RuntimeManager
from desktop.runtime.power import SleepResumeWatcher
from desktop.tray.tray import TrayController, TrayError

logger = get_logger(__name__)

_EXIT_POLL_SECONDS = 0.5


class JarvisApplication:
    def __init__(
        self,
        manager: RuntimeManager,
        exit_event: threading.Event,
        tray: TrayController | None = None,
        power: SleepResumeWatcher | None = None,
        scheduler: ReminderScheduler | None = None,
    ):
        self._manager = manager
        self._exit_event = exit_event
        self._tray = tray
        self._power = power
        self._scheduler = scheduler

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
            logger.info("JARVIS runtime running")
            while not self._exit_event.wait(_EXIT_POLL_SECONDS):
                pass
            return 0
        finally:
            self._shutdown()

    def _start_scheduler(self) -> None:
        """Reminders are independent of the voice engine: a scheduler problem never stops JARVIS."""
        if self._scheduler is None:
            return
        try:
            self._scheduler.start()
        except Exception:  # noqa: BLE001
            logger.exception("Reminder scheduler could not start; reminders will not fire")

    def _shutdown(self) -> None:
        if self._scheduler is not None:
            self._scheduler.stop()
        if self._power is not None:
            self._power.stop()
        self._manager.shutdown()
        if self._tray is not None:
            self._tray.stop()
        logger.info("JARVIS runtime stopped")
