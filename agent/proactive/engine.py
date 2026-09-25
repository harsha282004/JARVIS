"""ProactiveEngine: OBSERVE -> ANALYZE -> DECIDE -> NOTIFY.

    sources (read-only) -> signals -> candidates -> NotificationPolicy -> claim -> existing NotificationService channels

`run_once()` is called by the existing scheduler thread (agent.tasks.scheduler.ReminderScheduler runs it as an extra
pass; there is no second scheduler or thread). It throttles itself to `interval_seconds`, so a 15-second scheduler poll
does not mean a 15-second proactive check.

What it never does: modify a task, event, calendar entry or email; send anything; call a tool; use a language model; or
touch audio hardware (voice goes through the existing announcement queue the VoiceEngine drains between conversations).
A signal is information, not authorization: the only outward effect is one short sentence handed to the notifiers.

Delivery is claim-based (see NotificationRepository): exactly one caller wins each signal, a signal is recorded as
delivered only after a channel accepted it, and a failed delivery stays retryable with backoff.
"""

import threading
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from agent.proactive.messages import GENERIC_EMAIL_MESSAGE, build_candidate
from agent.proactive.models import (
    CandidateStatus,
    Channel,
    NotificationCandidate,
    PolicyAction,
    ProactiveSignal,
    ProactiveStorageError,
    SourceKind,
    Urgency,
)
from agent.proactive.policy import NotificationPolicy
from agent.proactive.repository import NotificationRepository
from agent.proactive.sources import SignalSource
from agent.tasks.models import utcnow
from agent.tasks.notifications import NotificationService
from backend.core.logging import get_logger

logger = get_logger(__name__)

SOURCE_BACKOFF_SECONDS = 600.0  # a source that failed is not retried for this long (one broken source never stops the rest)
RETENTION_DAYS = 30
PRUNE_INTERVAL = timedelta(days=1)


@dataclass
class CycleReport:
    skipped: bool = False
    skip_reason: str = ""
    signals: int = 0
    delivered: int = 0
    failed: int = 0
    deferred: int = 0
    expired: int = 0
    suppressed: Counter = field(default_factory=Counter)  # reason -> count
    source_errors: list[str] = field(default_factory=list)  # source kinds that failed (never any content)
    storage_error: bool = False


class ProactiveEngine:
    def __init__(
        self,
        sources: Sequence[SignalSource],
        policy: NotificationPolicy,
        repository: NotificationRepository,
        notifiers: Mapping[Channel, NotificationService],
        zone: ZoneInfo,
        *,
        clock: Callable[[], datetime] = utcnow,
        interval_seconds: float = 60.0,
    ):
        self._sources = list(sources)
        self._policy = policy
        self._repo = repository
        self._notifiers = dict(notifiers)
        self._zone = zone
        self._clock = clock
        self._interval = timedelta(seconds=interval_seconds)
        self._last_run: datetime | None = None
        self._last_prune: datetime | None = None
        self._source_retry: dict[SourceKind, datetime] = {}
        self._lock = threading.Lock()

    @property
    def enabled(self) -> bool:
        return self._policy.config.enabled

    def run_once(self, force: bool = False) -> CycleReport:
        """One proactive pass. Never raises: every failure is contained, logged (types only) and reported."""
        report = CycleReport()
        if not self.enabled:
            report.skipped, report.skip_reason = True, "disabled"
            return report
        now = self._clock()
        if not force and self._last_run is not None and now - self._last_run < self._interval:
            report.skipped, report.skip_reason = True, "interval"
            return report
        if not self._lock.acquire(blocking=False):  # another pass of this engine is already running
            report.skipped, report.skip_reason = True, "busy"
            return report
        try:
            self._last_run = now
            signals = self._collect(now, report)
            report.signals = len(signals)
            for signal in sorted(signals, key=lambda s: (-int(s.urgency), -int(s.priority), s.relevant_at or now, s.signal_id)):
                self._handle(signal, now, report)
            self._prune(now)
        except ProactiveStorageError as exc:
            report.storage_error = True
            logger.error("Proactive pass stopped: the notification history is unavailable (%s)", type(exc).__name__)
        except Exception as exc:  # noqa: BLE001 - the runtime must outlive any proactive failure
            report.storage_error = True
            logger.error("Proactive pass failed (%s)", type(exc).__name__)
        finally:
            self._lock.release()
        if report.signals or report.delivered:
            logger.info(
                "Proactive pass: signals=%d delivered=%d deferred=%d failed=%d expired=%d suppressed=%d",
                report.signals, report.delivered, report.deferred, report.failed, report.expired, sum(report.suppressed.values()),
            )
        return report

    # ---- observe ---------------------------------------------------------------------------------------------------------

    def _collect(self, now: datetime, report: CycleReport) -> list[ProactiveSignal]:
        signals: list[ProactiveSignal] = []
        for source in self._sources:
            retry_at = self._source_retry.get(source.kind)
            if retry_at is not None and now < retry_at:
                continue
            try:
                if not source.is_available():
                    continue
                signals.extend(source.collect(now))
                self._source_retry.pop(source.kind, None)
            except Exception as exc:  # noqa: BLE001 - one unavailable or malformed source must not affect the others
                self._source_retry[source.kind] = now + timedelta(seconds=SOURCE_BACKOFF_SECONDS)
                report.source_errors.append(source.kind.value)
                logger.warning("Proactive source %s failed (%s); skipping it for a while", source.kind.value, type(exc).__name__)
        return signals

    # ---- decide and notify ---------------------------------------------------------------------------------------------------

    def _handle(self, signal: ProactiveSignal, now: datetime, report: CycleReport) -> None:
        candidate = build_candidate(signal, now, self._zone)
        decision = self._policy.decide(
            signal, now,
            existing=self._repo.get_by_key(signal.signal_id),
            last_for_source=self._repo.last_delivered_for_source(signal.source_type, signal.source_id),
            delivered_last_hour=self._repo.delivered_since(now - timedelta(hours=1)),
            available=frozenset(self._notifiers),
        )
        if decision.action is PolicyAction.EXPIRE:
            candidate.status = CandidateStatus.EXPIRED
            report.expired += 1
            return
        if decision.action is PolicyAction.DEFER:
            report.deferred += 1  # stays pending: a later cycle decides again (e.g. once quiet hours end)
            return
        if decision.action is PolicyAction.SUPPRESS:
            candidate.status, candidate.suppression_reason = CandidateStatus.SUPPRESSED, decision.reason
            report.suppressed[decision.reason] += 1
            return
        candidate.delivery_channels = list(decision.channels)
        self._deliver(candidate, signal, now, report)

    def _deliver(self, candidate: NotificationCandidate, signal: ProactiveSignal, now: datetime, report: CycleReport) -> None:
        stored = candidate.model_copy(update={"message": GENERIC_EMAIL_MESSAGE}) if signal.source_type is SourceKind.GMAIL else candidate
        claim = self._repo.claim(stored, signal, now)
        if not claim.won:
            report.suppressed[f"claim: {claim.reason}"] += 1  # another worker has it, it is done, or it is backing off
            return
        delivered_on: list[str] = []
        metadata = {"proactive": True, "candidate_id": claim.notification_id, "signal_type": signal.signal_type.value, "source_type": signal.source_type.value,
                    "priority": {1: "low", 2: "normal", 3: "high", 4: "critical"}.get(int(candidate.priority), "normal")}
        for channel in candidate.delivery_channels:
            notifier = self._notifiers.get(channel)
            if notifier is None:
                continue
            try:
                notifier.notify(candidate.message, metadata)
                delivered_on.append(channel.value)
            except Exception as exc:  # noqa: BLE001 - one broken channel must not stop the others
                logger.warning("Proactive channel %s failed (%s)", channel.value, type(exc).__name__)
        assert claim.claimed_at is not None
        if delivered_on:
            if self._repo.complete(claim.notification_id, claim.claimed_at, delivered_on, now):
                candidate.status = CandidateStatus.DELIVERED
                report.delivered += 1
            else:  # delivered, but the claim was lost meanwhile (a crashed-worker takeover): never count it twice
                logger.warning("A proactive notification was delivered but its claim had been taken over")
            return
        candidate.status = CandidateStatus.FAILED
        self._repo.fail(claim.notification_id, claim.claimed_at, now)  # retryable after a backoff
        report.failed += 1

    def _prune(self, now: datetime) -> None:
        if self._last_prune is None or now - self._last_prune >= PRUNE_INTERVAL:
            self._last_prune = now
            removed = self._repo.prune(now - timedelta(days=RETENTION_DAYS))
            if removed:
                logger.info("Proactive history pruned (removed=%d)", removed)
