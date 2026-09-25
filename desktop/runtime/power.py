"""Detects Windows sleep/resume without extra dependencies.

A heartbeat thread wakes every `interval` seconds and compares wall-clock
time between wake-ups. While the machine sleeps, the thread is suspended, so
after resume the gap is far larger than `interval`; that gap is reported as
a resume. This can only notice a sleep *after* it happened (there is no
"about to sleep" signal), and a large manual clock change can trigger a
harmless false positive (the microphone is simply reopened).
"""

import ctypes
import threading
import time
from collections.abc import Callable
from enum import StrEnum

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


# ---- power / session state ----------------------------------------------------------------------------------------------

class PowerState(StrEnum):
    ACTIVE = "active"  # user is at the machine
    BACKGROUND = "background"  # screen locked
    SUSPENDED = "suspended"  # asleep (only ever observed after the fact, see SleepResumeWatcher)
    RESUME = "resume"  # just woke up; becomes ACTIVE/BACKGROUND on the next probe
    OFF = "off"  # Windows is shutting down / JARVIS is exiting


def windows_session_locked() -> bool | None:
    """True if the interactive desktop is locked, False if not, None if it cannot be determined (non-Windows, no session).

    While the workstation is locked the input desktop is the secure desktop and `OpenInputDesktop` fails for a normal process."""
    try:
        user32 = ctypes.windll.user32  # type: ignore[attr-defined]
    except (AttributeError, OSError):
        return None
    try:
        desktop = user32.OpenInputDesktop(0, False, 0x0100)  # DESKTOP_SWITCHDESKTOP
        if not desktop:
            return True
        user32.CloseDesktop(desktop)
        return False
    except Exception:  # noqa: BLE001
        return None


class PowerStateTracker:
    """Combines the sleep/resume watcher and a lock probe into one PowerState and reports changes."""

    def __init__(self, on_change: Callable[[PowerState, PowerState], None] | None = None, probe: Callable[[], bool | None] = windows_session_locked):
        self._on_change = on_change
        self._probe = probe
        self._state = PowerState.ACTIVE
        self._lock = threading.Lock()

    @property
    def state(self) -> PowerState:
        return self._state

    def _set(self, new: PowerState) -> None:
        with self._lock:
            old = self._state
            if old is new:
                return
            self._state = new
        logger.info("Power state: %s -> %s", old.value, new.value)
        if self._on_change:
            try:
                self._on_change(old, new)
            except Exception as exc:  # noqa: BLE001
                logger.error("Power state listener failed (%s)", type(exc).__name__)

    def note_resume(self) -> None:
        """Called by the SleepResumeWatcher after a sleep gap: the machine WAS suspended and is now resuming."""
        self._set(PowerState.SUSPENDED)
        self._set(PowerState.RESUME)
        self.poll()

    def poll(self) -> PowerState:
        """Re-read the lock state. ACTIVE <-> BACKGROUND; unknown lock state leaves the state alone."""
        if self._state is PowerState.OFF:
            return self._state
        locked = self._probe()
        if locked is True:
            self._set(PowerState.BACKGROUND)
        elif locked is False:
            self._set(PowerState.ACTIVE)
        elif self._state is PowerState.RESUME:
            self._set(PowerState.ACTIVE)
        return self._state

    def shutting_down(self) -> None:
        self._set(PowerState.OFF)
