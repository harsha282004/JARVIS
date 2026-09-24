"""EventRepository: persistence only (SQLAlchemy). No event rules live here.

Each call is its own transaction unless wrapped in `unit_of_work()`. State changes are conditional UPDATEs
(`WHERE status = <expected>`), so two racing callers cannot both change the same event. Database failures
surface as EventStorageError with the exception type only (driver messages can echo SQL parameters).
"""

import threading
from collections.abc import Callable, Collection, Iterator, Mapping
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from agent.events.models import (
    Event,
    EventSource,
    EventStatus,
    EventStorageError,
    EventType,
    SourceType,
)
from agent.memory.models import Confidence
from agent.tasks.models import TaskPriority, to_utc
from backend.models.events import EventRow

SessionFactory = Callable[[], Session]


def _aware(value: datetime | None) -> datetime | None:
    return value.replace(tzinfo=timezone.utc) if value is not None and value.tzinfo is None else value


def _normalize(values: Mapping[str, Any]) -> dict[str, Any]:
    return {k: (to_utc(v) if isinstance(v, datetime) else v) for k, v in values.items()}


def _event(row: EventRow) -> Event:
    return Event(
        event_id=row.id, title=row.title, description=row.description, event_type=EventType(row.event_type),
        status=EventStatus(row.status), priority=TaskPriority(row.priority) if row.priority is not None else None,
        start_at=_aware(row.start_at), end_at=_aware(row.end_at), due_at=_aware(row.due_at), timezone=row.timezone,
        all_day=bool(row.all_day),
        source=EventSource(source_type=SourceType(row.source_type), source_id=row.source_id or None,
                           reference=row.source_reference),
        confidence=Confidence(row.confidence), task_id=row.task_id, dedupe_key=row.dedupe_key,
        created_at=_aware(row.created_at), updated_at=_aware(row.updated_at),
        completed_at=_aware(row.completed_at), cancelled_at=_aware(row.cancelled_at), metadata=row.extra or {},
    )


class EventRepository:
    def __init__(self, session_factory: SessionFactory):
        self._session_factory = session_factory
        self._local = threading.local()  # the open unit of work belongs to the calling thread only

    @property
    def _session(self) -> Session | None:
        return getattr(self._local, "session", None)

    @_session.setter
    def _session(self, value: Session | None) -> None:
        self._local.session = value

    @contextmanager
    def unit_of_work(self) -> Iterator[None]:
        if self._session is not None:
            yield
            return
        try:
            with self._session_factory() as session:
                self._session = session
                try:
                    yield
                    session.commit()
                except Exception:
                    session.rollback()
                    raise
                finally:
                    self._session = None
        except SQLAlchemyError as exc:
            raise EventStorageError(f"Event database error ({type(exc).__name__})") from None

    def _run(self, work: Callable[[Session], Any]) -> Any:
        try:
            if self._session is not None:
                result = work(self._session)
                self._session.flush()
                return result
            with self._session_factory() as session:
                result = work(session)
                session.commit()
                return result
        except SQLAlchemyError as exc:
            raise EventStorageError(f"Event database error ({type(exc).__name__})") from None

    def add_event(self, e: Event) -> Event:
        def work(s: Session) -> None:
            s.add(EventRow(
                id=e.event_id, title=e.title, description=e.description, event_type=e.event_type.value,
                status=e.status.value, priority=int(e.priority) if e.priority is not None else None,
                start_at=e.start_at, end_at=e.end_at, due_at=e.due_at, timezone=e.timezone, all_day=e.all_day,
                source_type=e.source.source_type.value, source_id=e.source.source_id or "",
                source_reference=e.source.reference, confidence=int(e.confidence), task_id=e.task_id,
                dedupe_key=e.dedupe_key, created_at=e.created_at, updated_at=e.updated_at,
                completed_at=e.completed_at, cancelled_at=e.cancelled_at, extra=dict(e.metadata),
            ))

        self._run(work)
        return e

    def get_event(self, event_id: str) -> Event | None:
        return self._run(lambda s: (lambda r: _event(r) if r is not None else None)(s.get(EventRow, event_id)))

    def find_by_dedupe(self, source_type: SourceType, source_id: str | None, dedupe_key: str) -> Event | None:
        stmt = select(EventRow).where(
            EventRow.source_type == source_type.value, EventRow.source_id == (source_id or ""),
            EventRow.dedupe_key == dedupe_key,
        )
        return self._run(lambda s: (lambda r: _event(r) if r is not None else None)(s.scalars(stmt).first()))

    def list_by_source(self, source_type: SourceType, statuses: Collection[EventStatus] | None = None, limit: int = 100) -> list[Event]:
        stmt = select(EventRow).where(EventRow.source_type == source_type.value)
        if statuses is not None:
            stmt = stmt.where(EventRow.status.in_([s.value for s in statuses]))
        stmt = stmt.order_by(func.coalesce(EventRow.start_at, EventRow.due_at).asc(), EventRow.id.asc()).limit(limit)
        return self._run(lambda s: [_event(r) for r in s.scalars(stmt)])

    def update_event(self, event_id: str, values: Mapping[str, Any], expected_status: EventStatus | None = None) -> bool:
        """Apply `values` (EventRow attribute names) if the event still has `expected_status`. True if it matched."""
        conditions = [EventRow.id == event_id]
        if expected_status is not None:
            conditions.append(EventRow.status == expected_status.value)
        return self._run(lambda s: s.execute(update(EventRow).where(*conditions).values(**_normalize(values))).rowcount == 1)

    def list_events(
        self,
        *,
        statuses: Collection[EventStatus] | None = None,
        event_type: EventType | None = None,
        task_id: str | None = None,
        limit: int = 500,
    ) -> list[Event]:
        """Events ordered by when they start or are due (soonest first), then id (a stable tiebreak)."""
        stmt = select(EventRow)
        if statuses is not None:
            stmt = stmt.where(EventRow.status.in_([s.value for s in statuses]))
        if event_type is not None:
            stmt = stmt.where(EventRow.event_type == event_type.value)
        if task_id is not None:
            stmt = stmt.where(EventRow.task_id == task_id)
        stmt = stmt.order_by(func.coalesce(EventRow.start_at, EventRow.due_at).asc(), EventRow.id.asc()).limit(limit)
        return self._run(lambda s: [_event(r) for r in s.scalars(stmt)])
