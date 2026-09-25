"""IntelligenceRunner: the background loop that keeps the intelligence layer current and raises proactive notifications.

Event-driven first, polling as a safety net:
  * events on the bus (EMAIL_RECEIVED, CALENDAR_UPDATED, TASK_CREATED/COMPLETED, AGENT_RESPONSE that changed something, SYSTEM_RESUME,
    INTEGRATION_RECOVERED) wake the loop early (after a short debounce);
  * otherwise it runs every `interval` seconds.
Each run reads the sources once. If nothing changed since the last run, the expensive analysis is skipped; time-based rules (a deadline
getting closer) still need evaluating, so findings are re-evaluated at least every `reevaluate_seconds` even when nothing changed.

Privacy: with external monitoring switched off (PRIVATE mode) the loop does nothing. Offline: cloud sources are simply unavailable and
say so. A failing run is logged and retried with backoff; it never affects the rest of JARVIS. The loop makes no language-model call.
"""

import threading
import time
from collections.abc import Callable

from agent.intelligence.proactive import IntelligenceNotifier
from agent.intelligence.service import IntelligenceService
from backend.core.events import EventBus, SystemEvent
from backend.core.logging import get_logger
from backend.core.metrics import metrics
from backend.core.notifications import NotificationCenter
from backend.core.privacy import PrivacyController

logger = get_logger(__name__)

WAKE_EVENTS = (SystemEvent.EMAIL_RECEIVED, SystemEvent.CALENDAR_UPDATED, SystemEvent.TASK_CREATED, SystemEvent.TASK_COMPLETED,
               SystemEvent.SYSTEM_RESUME, SystemEvent.INTEGRATION_RECOVERED, SystemEvent.AGENT_RESPONSE)
DEBOUNCE_SECONDS = 2.0
MAX_BACKOFF_SECONDS = 600.0


class IntelligenceRunner:
    def __init__(
        self,
        service: IntelligenceService,
        notifier: IntelligenceNotifier | None,
        *,
        center: NotificationCenter | None = None,
        bus: EventBus | None = None,
        privacy: PrivacyController | None = None,
        interval_seconds: float = 300.0,
        reevaluate_seconds: float = 600.0,
        clock: Callable[[], float] = time.monotonic,
    ):
        self._svc, self._notifier, self._center = service, notifier, center
        self._bus, self._privacy = bus, privacy
        self._interval = interval_seconds
        self._reevaluate = reevaluate_seconds
        self._clock = clock
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._running = False
        self._last_fingerprint: str | None = None
        self._last_evaluated = -1e18
        self._failures = 0
        self._unsubscribe: list[Callable[[], None]] = []
        self.runs = 0
        self.skipped_unchanged = 0

    # ---- lifecycle ---------------------------------------------------------------------------------------------------

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        if self._bus is not None:
            for event in WAKE_EVENTS:
                self._unsubscribe.append(self._bus.subscribe(event, self._on_event))
        self._thread = threading.Thread(target=self._loop, name="jarvis-intelligence", daemon=True)
        self._thread.start()
        logger.info("Intelligence runner started (interval=%ss)", self._interval)

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        for unsubscribe in self._unsubscribe:
            unsubscribe()
        self._unsubscribe.clear()
        thread, self._thread = self._thread, None
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=10)
        logger.info("Intelligence runner stopped")

    def is_alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def mark_dirty(self) -> None:
        self._wake.set()

    def _on_event(self, event) -> None:
        if event.type is SystemEvent.AGENT_RESPONSE and not event.payload.get("executed"):
            return  # a plain answer changed nothing
        if not self._running:  # events caused by this runner's own reads are not news
            self._wake.set()

    # ---- the loop --------------------------------------------------------------------------------------------------------

    def _loop(self) -> None:
        delay = 5.0  # first run shortly after startup, once services had a moment to come up
        while not self._stop.is_set():
            woke = self._wake.wait(delay)
            if self._stop.is_set():
                break
            if woke:
                self._wake.clear()
                self._stop.wait(DEBOUNCE_SECONDS)  # let a burst of events settle into one run
                self._wake.clear()
            try:
                self.run_once()
                self._failures = 0
                delay = self._interval
            except Exception as exc:  # noqa: BLE001 - a failing analysis must never affect the rest of JARVIS
                self._failures += 1
                delay = min(MAX_BACKOFF_SECONDS, self._interval * (2 ** min(self._failures, 5)))
                logger.error("Intelligence run failed (%s); next attempt in %.0fs", type(exc).__name__, delay)

    def run_once(self) -> dict:
        """One evaluation. Returns what happened (for tests and the dashboard)."""
        if self._privacy is not None and not self._privacy.capabilities.external_monitoring:
            return {"skipped": "private"}
        self._running = True
        try:
            with metrics.timer("intelligence.run_ms"):
                bundle = self._svc.background_pass()
                changed = bundle.fingerprint != self._last_fingerprint
                due_for_time_rules = self._clock() - self._last_evaluated >= self._reevaluate
                self.runs += 1
                metrics.incr("intelligence.runs")
                notified = []
                created = []
                if changed or due_for_time_rules:
                    created = self._svc.auto_create_tasks(bundle)
                    if self._notifier is not None:
                        notified = self._notifier.publish(bundle.findings)
                    if self._center is not None:
                        self._center.release_deferred()
                    self._last_fingerprint, self._last_evaluated = bundle.fingerprint, self._clock()
                else:
                    self.skipped_unchanged += 1
                    metrics.incr("intelligence.skipped_unchanged")
            return {"changed": changed, "notified": len(notified), "auto_created": len(created), "findings": len(bundle.findings)}
        finally:
            self._running = False
