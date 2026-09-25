"""PeriodicTask: a small daemon thread that runs one function every `interval` seconds, with error isolation and a clean stop.

Used for the health check loop and the power/lock poll. `start()` and `stop()` make it a lifecycle component for JarvisApplication.
A failing run is logged (exception type only) and the loop continues on the next interval.
"""

import threading
from collections.abc import Callable

from backend.core.logging import get_logger

logger = get_logger(__name__)


class PeriodicTask:
    def __init__(self, name: str, fn: Callable[[], None], interval_seconds: float, *, run_immediately: bool = True):
        self.name = name
        self._fn = fn
        self._interval = interval_seconds
        self._immediate = run_immediately
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name=f"jarvis-{self.name}", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=self._interval + 5.0)

    def is_alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def run_now(self) -> None:
        self._run()

    def _run(self) -> None:
        try:
            self._fn()
        except Exception as exc:  # noqa: BLE001 - one bad run must not end the loop
            logger.error("Periodic task %s failed (%s)", self.name, type(exc).__name__)

    def _loop(self) -> None:
        if self._immediate:
            self._run()
        while not self._stop.wait(self._interval):
            self._run()
