"""SyncEngine: incremental, idempotent, rate-limit-aware synchronization of every integration into the hub's normalized store.

Per integration the registry keeps a cursor, the last sync, the last error and a retry time. A sync:
  1. is skipped when the integration is off / not connected / needs the user (authentication) / still backing off / not yet due;
  2. asks the adapter for what changed since the cursor (never "everything again" unless the adapter has no cursor);
  3. upserts the normalized items (same source item = same row: idempotent, re-running changes nothing) and marks vanished ones;
  4. records success (cursor advanced, last sync time) or a classified failure with a backoff:
       RATE_LIMIT -> at least Retry-After, else exponential;  NETWORK/SERVER -> exponential (1 min doubling, capped at 1 h);
       AUTH/CONFIGURATION/PERMISSION -> no automatic retry (the user must reconnect; JARVIS says so), never hammered.
Events are published on the bus (started/completed/failed) so the intelligence layer reacts instead of polling.
`SyncRunner` is the small background thread that calls `sync_due()`; it wakes early on connect/resume/recovery events, and does nothing in PRIVATE mode.
"""

import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

from backend.core.events import Event, EventBus, SystemEvent
from backend.core.logging import get_logger
from backend.core.metrics import metrics
from integrations.hub.models import NEEDS_USER_KINDS, ErrorKind, HubError, NormalizedItem, classify_error, utcnow
from integrations.hub.registry import IntegrationRegistry
from integrations.hub.repository import HubRepository

logger = get_logger(__name__)

BASE_BACKOFF_SECONDS = 60.0
MAX_BACKOFF_SECONDS = 3600.0
BATCH_LIMIT = 50

ItemsCallback = Callable[[str, list[tuple[NormalizedItem, str]]], None]  # (source, [(item, 'created'|'updated')]) for what actually changed


@dataclass
class SyncOutcome:
    integration: str
    ok: bool
    created: int = 0
    updated: int = 0
    unchanged: int = 0
    removed: int = 0
    skipped: str | None = None  # why nothing was attempted
    error_kind: str | None = None
    error: str | None = None


class SyncEngine:
    def __init__(self, registry: IntegrationRegistry, repo: HubRepository, bus: EventBus | None = None, clock: Callable[[], datetime] = utcnow,
                 on_items: ItemsCallback | None = None, batch_limit: int = BATCH_LIMIT):
        self._registry, self._repo, self._bus, self._clock = registry, repo, bus, clock
        self._on_items = on_items
        self._limit = batch_limit
        self._locks: dict[str, threading.Lock] = {}

    def _lock(self, name: str) -> threading.Lock:
        return self._locks.setdefault(name, threading.Lock())

    @staticmethod
    def backoff_seconds(failures: int, retry_after: float | None = None) -> float:
        delay = min(MAX_BACKOFF_SECONDS, BASE_BACKOFF_SECONDS * (2 ** max(0, failures - 1)))
        return max(delay, min(retry_after, MAX_BACKOFF_SECONDS)) if retry_after else delay

    def is_due(self, name: str) -> tuple[bool, str]:
        adapter = self._registry.adapter(name)
        if adapter is None or "sync" not in adapter.supported:
            return False, "does not synchronize"
        ok, reason = self._registry.allowed(name)
        if not ok:
            return False, reason
        info = self._registry.info(name)
        if info.last_error_kind in {k.value for k in NEEDS_USER_KINDS}:
            return False, "needs you to reconnect"
        retry_at = self._registry.next_allowed_at(name)
        now = self._clock()
        if retry_at is not None and now < retry_at:
            return False, f"backing off until {retry_at.astimezone().strftime('%H:%M')}"
        if info.last_sync_at is not None and now - info.last_sync_at < timedelta(seconds=adapter.sync_interval_seconds):
            return False, "not due yet"
        return True, ""

    def sync(self, name: str, *, force: bool = False) -> SyncOutcome:
        adapter = self._registry.adapter(name)
        if adapter is None:
            return SyncOutcome(name, False, skipped="unknown integration")
        if not force:
            due, why = self.is_due(name)
            if not due:
                return SyncOutcome(adapter.name, True, skipped=why)
        else:
            ok, why = self._registry.allowed(name)
            if not ok or "sync" not in adapter.supported:
                return SyncOutcome(adapter.name, False, skipped=why or "does not synchronize")
        if not self._lock(adapter.name).acquire(blocking=False):
            return SyncOutcome(adapter.name, True, skipped="a sync is already running")
        self._registry.set_syncing(adapter.name, True)
        self._publish(SystemEvent.INTEGRATION_SYNC_STARTED, integration=adapter.name)
        try:
            with metrics.timer(f"sync.{adapter.name}_ms"):
                batch = adapter.sync(self._registry.cursor(adapter.name), self._limit)
                counts = self._repo.upsert_many(batch.items)
                removed = sum(self._repo.mark_deleted(adapter.name, kind, [sid]) for kind, sid in batch.removed)
            self._registry.record_success(adapter.name, batch.cursor if batch.cursor is not None else self._registry.cursor(adapter.name), counts.changed)
            outcome = SyncOutcome(adapter.name, True, counts.created, counts.updated, counts.unchanged, removed)
            self._publish(SystemEvent.INTEGRATION_SYNC_COMPLETED, integration=adapter.name, created=counts.created, updated=counts.updated, removed=removed)
            if self._on_items and (counts.changed or removed):
                try:
                    self._on_items(adapter.name, counts.items)
                except Exception as exc:  # noqa: BLE001 - a consumer failing must not undo a successful sync
                    logger.error("Sync consumer for %s failed (%s)", adapter.name, type(exc).__name__)
            return outcome
        except Exception as exc:  # noqa: BLE001 - classified, recorded, never raised
            err = self._fail(adapter.name, exc, adapter.display_name)
            return SyncOutcome(adapter.name, False, error_kind=err.kind.value, error=err.message)
        finally:
            self._registry.set_syncing(adapter.name, False)
            self._lock(adapter.name).release()

    def _fail(self, name: str, exc: BaseException, label: str) -> HubError:
        err = classify_error(exc, label)
        retry_at = None
        if err.kind not in NEEDS_USER_KINDS and err.kind is not ErrorKind.NOT_FOUND:
            retry_at = self._clock() + timedelta(seconds=self.backoff_seconds(self._registry.failures(name) + 1, err.retry_after))
        self._registry.record_failure(name, err, retry_at)
        logger.warning("Sync of %s failed (%s)", name, err.kind.value)
        self._publish(SystemEvent.INTEGRATION_SYNC_FAILED, integration=name, kind=err.kind.value)
        return err

    def sync_due(self) -> list[SyncOutcome]:
        return [o for n in self._registry.names() if not (o := self.sync(n)).skipped]

    def _publish(self, event: SystemEvent, **payload) -> None:
        if self._bus:
            self._bus.publish(event, **payload)


class SyncRunner:
    """Background thread: `sync_due()` every `interval` seconds, and early when something makes a sync worthwhile."""

    WAKE = (SystemEvent.INTEGRATION_CONNECTED, SystemEvent.SYSTEM_RESUME, SystemEvent.INTEGRATION_RECOVERED)

    def __init__(self, engine: SyncEngine, bus: EventBus | None = None, privacy=None, interval_seconds: float = 60.0, first_delay: float = 10.0):
        self._engine, self._bus, self._privacy = engine, bus, privacy
        self._interval, self._first = interval_seconds, first_delay
        self._wake, self._stop = threading.Event(), threading.Event()
        self._thread: threading.Thread | None = None
        self._unsub: list[Callable[[], None]] = []

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        if self._bus:
            self._unsub = [self._bus.subscribe(e, lambda _e: self._wake.set()) for e in self.WAKE]
        self._thread = threading.Thread(target=self._loop, name="jarvis-sync", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        for u in self._unsub:
            u()
        self._unsub = []
        thread, self._thread = self._thread, None
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=15)

    def is_alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def run_once(self) -> list[SyncOutcome]:
        if self._privacy is not None and not self._privacy.capabilities.external_monitoring:
            return []  # PRIVATE mode: nothing external is observed
        return self._engine.sync_due()

    def _loop(self) -> None:
        delay = self._first
        while not self._stop.is_set():
            self._wake.wait(delay)
            if self._stop.is_set():
                break
            self._wake.clear()
            try:
                self.run_once()
            except Exception as exc:  # noqa: BLE001
                logger.error("Sync run failed (%s)", type(exc).__name__)
            delay = self._interval
