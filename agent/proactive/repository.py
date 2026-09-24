"""NotificationRepository: persistence of the notification history (SQLAlchemy). No policy lives here.

The history is what makes de-duplication, cooldown and "why did you notify me?" work, and it is the atomic claim that
lets several schedulers or processes run at once with exactly one delivery per signal:

  claim()    INSERT a PENDING row keyed by the signal's unique key. Only one caller can insert it; a second caller (or
             a second process) gets a unique-key conflict and is told the signal is taken. A stale lease (a crashed
             worker) or a FAILED row whose backoff has passed can be re-claimed by exactly one caller through a
             conditional UPDATE.
  complete() PENDING -> DELIVERED, only for the caller holding the claim, and only called after a channel accepted it.
  fail()     PENDING -> FAILED (retryable with backoff, at most MAX_ATTEMPTS times).

Database failures surface as ProactiveStorageError with the exception type only (driver messages can echo SQL values).
"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from agent.proactive.models import (
    CandidateStatus,
    HistoryRecord,
    NotificationCandidate,
    ProactiveSignal,
    ProactiveStorageError,
    SignalType,
    SourceKind,
    Urgency,
)
from agent.tasks.models import TaskPriority, to_utc
from backend.models.proactive import NotificationRow

SessionFactory = Callable[[], Session]

LEASE_SECONDS = 120.0  # a claim older than this belongs to a crashed worker and may be taken over
MAX_ATTEMPTS = 5
BACKOFF_BASE_SECONDS = 60.0
BACKOFF_MAX_SECONDS = 900.0


def _aware(value: datetime | None) -> datetime | None:
    return value.replace(tzinfo=timezone.utc) if value is not None and value.tzinfo is None else value


def _record(row: NotificationRow) -> HistoryRecord:
    return HistoryRecord(
        notification_id=row.id, dedupe_key=row.dedupe_key, signal_type=SignalType(row.signal_type), source_type=SourceKind(row.source_type),
        source_id=row.source_id, source_reference=row.source_reference, message=row.message, reason=row.reason,
        priority=TaskPriority(row.priority), urgency=Urgency(row.urgency), status=CandidateStatus(row.status),
        channels=[c for c in row.channels.split(",") if c], attempts=row.attempts, relevant_at=_aware(row.relevant_at), created_at=_aware(row.created_at),
        claimed_at=_aware(row.claimed_at), delivered_at=_aware(row.delivered_at), failed_at=_aware(row.failed_at),
    )


def backoff_seconds(attempts: int) -> float:
    return min(BACKOFF_BASE_SECONDS * 2 ** max(attempts - 1, 0), BACKOFF_MAX_SECONDS)


@dataclass(frozen=True)
class ClaimResult:
    won: bool
    reason: str  # "claimed", "reclaimed", or why not: "delivered", "in_progress", "backoff", "gave_up"
    notification_id: str = ""
    claimed_at: datetime | None = None


class NotificationRepository:
    def __init__(self, session_factory: SessionFactory):
        self._session_factory = session_factory

    def _run(self, work: Callable[[Session], Any]) -> Any:
        try:
            with self._session_factory() as session:
                result = work(session)
                session.commit()
                return result
        except SQLAlchemyError as exc:
            raise ProactiveStorageError(f"Notification history error ({type(exc).__name__})") from None

    # ---- claim / complete / fail -------------------------------------------------------------------------------------

    def claim(self, candidate: NotificationCandidate, signal: ProactiveSignal, now: datetime) -> ClaimResult:
        """Take the right to deliver this signal. Exactly one caller wins."""
        now = to_utc(now)
        row = NotificationRow(
            id=candidate.candidate_id, dedupe_key=signal.signal_id, signal_type=signal.signal_type.value,
            source_type=signal.source_type.value, source_id=signal.source_id, source_reference=signal.source_reference[:200],
            message=candidate.message, reason=candidate.reason[:300], priority=int(candidate.priority), urgency=int(candidate.urgency),
            status=CandidateStatus.PENDING.value, channels="", attempts=0, relevant_at=signal.relevant_at, created_at=now, claimed_at=now,
        )
        try:
            with self._session_factory() as session:
                session.add(row)
                try:
                    session.commit()
                    return ClaimResult(True, "claimed", candidate.candidate_id, now)
                except IntegrityError:
                    session.rollback()  # somebody already holds or finished this signal
        except SQLAlchemyError as exc:
            raise ProactiveStorageError(f"Notification history error ({type(exc).__name__})") from None
        return self._reclaim(signal.signal_id, now)

    def _reclaim(self, key: str, now: datetime) -> ClaimResult:
        def work(s: Session) -> ClaimResult:
            row = s.scalars(select(NotificationRow).where(NotificationRow.dedupe_key == key)).first()
            if row is None:  # deleted between the conflict and now: try again next cycle
                return ClaimResult(False, "in_progress")
            if row.status == CandidateStatus.DELIVERED.value:
                return ClaimResult(False, "delivered", row.id)
            seen_status, seen_claim = row.status, row.claimed_at
            if row.status == CandidateStatus.FAILED.value:
                if row.attempts >= MAX_ATTEMPTS:
                    return ClaimResult(False, "gave_up", row.id)
                failed = _aware(row.failed_at)
                if failed is not None and now - failed < timedelta(seconds=backoff_seconds(row.attempts)):
                    return ClaimResult(False, "backoff", row.id)
            elif row.status == CandidateStatus.PENDING.value:
                claimed = _aware(row.claimed_at)
                if claimed is not None and now - claimed < timedelta(seconds=LEASE_SECONDS):
                    return ClaimResult(False, "in_progress", row.id)
            else:
                return ClaimResult(False, "in_progress", row.id)
            stmt = update(NotificationRow).where(
                NotificationRow.id == row.id, NotificationRow.status == seen_status, NotificationRow.claimed_at == seen_claim,
            ).values(status=CandidateStatus.PENDING.value, claimed_at=now)
            if s.execute(stmt).rowcount == 1:
                return ClaimResult(True, "reclaimed", row.id, now)
            return ClaimResult(False, "in_progress", row.id)  # another caller re-claimed it first

        return self._run(work)

    def complete(self, notification_id: str, claimed_at: datetime, channels: list[str], now: datetime) -> bool:
        stmt = update(NotificationRow).where(
            NotificationRow.id == notification_id, NotificationRow.status == CandidateStatus.PENDING.value,
            NotificationRow.claimed_at == to_utc(claimed_at),
        ).values(status=CandidateStatus.DELIVERED.value, delivered_at=to_utc(now), channels=",".join(channels)[:64])
        return self._run(lambda s: s.execute(stmt).rowcount == 1)

    def fail(self, notification_id: str, claimed_at: datetime, now: datetime) -> bool:
        stmt = update(NotificationRow).where(
            NotificationRow.id == notification_id, NotificationRow.status == CandidateStatus.PENDING.value,
            NotificationRow.claimed_at == to_utc(claimed_at),
        ).values(status=CandidateStatus.FAILED.value, failed_at=to_utc(now), attempts=NotificationRow.attempts + 1)
        return self._run(lambda s: s.execute(stmt).rowcount == 1)

    # ---- reads -------------------------------------------------------------------------------------------------------

    def get_by_key(self, key: str) -> HistoryRecord | None:
        stmt = select(NotificationRow).where(NotificationRow.dedupe_key == key)
        return self._run(lambda s: (lambda r: _record(r) if r is not None else None)(s.scalars(stmt).first()))

    def last_delivered_for_source(self, source_type: SourceKind, source_id: str) -> HistoryRecord | None:
        stmt = (
            select(NotificationRow)
            .where(
                NotificationRow.source_type == source_type.value, NotificationRow.source_id == source_id,
                NotificationRow.status == CandidateStatus.DELIVERED.value,
            )
            .order_by(NotificationRow.delivered_at.desc())
            .limit(1)
        )
        return self._run(lambda s: (lambda r: _record(r) if r is not None else None)(s.scalars(stmt).first()))

    def delivered_since(self, since: datetime) -> int:
        stmt = select(func.count()).select_from(NotificationRow).where(
            NotificationRow.status == CandidateStatus.DELIVERED.value, NotificationRow.delivered_at >= to_utc(since)
        )
        return int(self._run(lambda s: s.scalar(stmt)) or 0)

    def recent(self, limit: int = 5, *, delivered_only: bool = True) -> list[HistoryRecord]:
        stmt = select(NotificationRow)
        if delivered_only:
            stmt = stmt.where(NotificationRow.status == CandidateStatus.DELIVERED.value)
        stmt = stmt.order_by(func.coalesce(NotificationRow.delivered_at, NotificationRow.created_at).desc()).limit(max(1, min(limit, 50)))
        return self._run(lambda s: [_record(r) for r in s.scalars(stmt)])

    def prune(self, before: datetime) -> int:
        """Delete finished history older than `before` (never a live claim). Returns how many rows were removed."""
        stmt = delete(NotificationRow).where(
            NotificationRow.created_at < to_utc(before), NotificationRow.status != CandidateStatus.PENDING.value
        )
        return int(self._run(lambda s: s.execute(stmt).rowcount))
