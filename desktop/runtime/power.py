"""Detects Windows sleep/resume without extra dependencies.

A heartbeat thread wakes every `interval` seconds and compares wall-clock
time between wake-ups. While the machine sleeps, the thread is suspended, so
after resume the gap is far larger than `interval`; that gap is reported as
a resume. This can only notice a sleep *after* it happened (there is no
"about to sleep" signal), and a large manual clock change can trigger a
harmless false positive (the microphone is simply reopened).
"""

import threading
import time
from collections.abc import Callable

from backend.core.logging import get_logger

logger = get_logger(__name__)


class SleepResumeWatcher:
    def __init__(
        self,
        on_resume: Callable[[], None],
        interval: float = 2.0,
        gap_threshold: float = 10.0,
        clock: Callable[[], float] = time.time,
    ):
        self._on_resume = on_resume
        self._interval = interval
        self._gap_threshold = gap_threshold
        self._clock = clock
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last = clock()

    def tick(self) -> bool:
        """Record one heartbeat; fire `on_resume` and return True if a sleep gap is seen."""
        now = self._clock()
        gap = now - self._last
        self._last = now
        if gap > self._interval + self._gap_threshold:
            logger.info("Detected system resume (heartbeat gap %.0fs)", gap)
            self._on_resume()
            self._last = self._clock()  # the callback may block; don't count that as a gap
            return True
        return False

    def start(self) -> None:
        self._last = self._clock()
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="jarvis-power", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout=self._interval + 1.0)
        self._thread = None

    def _loop(self) -> None:
        while not self._stop.wait(self._interval):
            self.tick()
