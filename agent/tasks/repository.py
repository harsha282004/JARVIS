"""TaskRepository: persistence only (SQLAlchemy). No task or reminder rules live here.

Each call is its own transaction unless wrapped in `unit_of_work()`, which
makes several calls atomic (all written, or nothing). State changes are
*conditional* UPDATEs (`WHERE status = <expected> ...`) and report whether
they matched, so two racing callers (the scheduler and a voice request, or two
schedulers) cannot both win: that is what prevents double completion, a
duplicate reminder trigger and a lost cancellation. This works on PostgreSQL
(row locks) and SQLite alike. Database failures surface as TaskStorageError
with the exception type only (driver messages can echo SQL parameters).
"""

import threading
from collections.abc import Callable, Collection, Iterator, Mapping
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import and_, case, delete, or_, select, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from agent.tasks.models import (
    OPEN_STATUSES,
    Recurrence,
    Reminder,
    ReminderStatus,
    Task,
    TaskPriority,
    TaskStatus,
    TaskStorageError,
    to_utc,
)
from backend.models.tasks import ReminderRow, TaskRow

SessionFactory = Callable[[], Session]

_OPEN_VALUES = [s.value for s in OPEN_STATUSES]


def _aware(value: datetime | None) -> datetime | None:
    return value.replace(tzinfo=timezone.utc) if value is not None and value.tzinfo is None else value


def _normalize(values: Mapping[str, Any]) -> dict[str, Any]:
    return {k: (to_utc(v) if isinstance(v, datetime) else v) for k, v in values.items()}


def _task(row: TaskRow) -> Task:
    return Task(
        task_id=row.id, title=row.title, notes=row.notes, status=TaskStatus(row.status),
        priority=TaskPriority(row.priority), created_at=_aware(row.created_at), updated_at=_aware(row.updated_at),
        due_at=_aware(row.due_at), completed_at=_aware(row.completed_at), cancelled_at=_aware(row.cancelled_at),
        session_id=row.session_id, source=row.source, metadata=row.extra or {},
    )


def _reminder(row: ReminderRow) -> Reminder:
    return Reminder(
        reminder_id=row.id, task_id=row.task_id, message=row.message, scheduled_at=_aware(row.scheduled_at),
        status=ReminderStatus(row.status), timezone=row.timezone,
        recurrence=Recurrence.model_validate(row.recurrence) if row.recurrence else None,
        created_at=_aware(row.created_at), updated_at=_aware(row.updated_at),
        triggered_at=_aware(row.triggered_at), cancelled_at=_aware(row.cancelled_at),
        occurrences=row.occurrences or 0, delivery_attempts=row.delivery_attempts or 0,
        claimed_at=_aware(row.claimed_at), session_id=row.session_id, source=row.source, metadata=row.extra or {},
    )


class TaskRepository:
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
        """Run several repository calls as one transaction on the calling thread."""
        if self._session is not None:  # already inside one
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
            raise TaskStorageError(f"Task database error ({type(exc).__name__})") from None

    def _run(self, work: Callable[[Session], Any]) -> Any:
        try:
            if self._session is not None:
                result = work(self._session)
                self._session.flush()  # later calls in the unit of work must see this write
                return result
            with self._session_factory() as session:
                result = work(session)
                session.commit()
                return result
        except SQLAlchemyError as exc:
            raise TaskStorageError(f"Task database error ({type(exc).__name__})") from None

    # ---- tasks -------------------------------------------------------------

    def add_task(self, task: Task) -> Task:
        def work(s: Session) -> None:
            s.add(TaskRow(
                id=task.task_id, title=task.title, notes=task.notes, status=task.status.value,
                priority=int(task.priority), due_at=task.due_at, created_at=task.created_at,
                updated_at=task.updated_at, completed_at=task.completed_at, cancelled_at=task.cancelled_at,
                session_id=task.session_id, source=task.source, extra=dict(task.metadata),
            ))

        self._run(work)
        return task

    def get_task(self, task_id: str) -> Task | None:
        return self._run(lambda s: (lambda r: _task(r) if r is not None else None)(s.get(TaskRow, task_id)))

    def update_task(self, task_id: str, values: Mapping[str, Any], expected_status: TaskStatus | None = None) -> bool:
        """Apply `values` (TaskRow attribute names) if the task still has `expected_status`. True if it matched."""
        conditions = [TaskRow.id == task_id]
        if expected_status is not None:
            conditions.append(TaskRow.status == expected_status.value)
        return self._run(lambda s: s.execute(update(TaskRow).where(*conditions).values(**_normalize(values))).rowcount == 1)

    def delete_task(self, task_id: str) -> bool:
        def work(s: Session) -> bool:
            s.execute(delete(ReminderRow).where(ReminderRow.task_id == task_id))  # not left to the DB cascade alone
            return s.execute(delete(TaskRow).where(TaskRow.id == task_id)).rowcount == 1

        return self._run(work)

    def list_tasks(
        self,
        *,
        now: datetime,
        statuses: Collection[TaskStatus] | None = None,
        due_from: datetime | None = None,
        due_before: datetime | None = None,
        overdue_only: bool = False,
        limit: int = 50,
    ) -> list[Task]:
        """Tasks in the documented default order: overdue first, then due date (no due date last),
        then priority (highest first), then creation time, then id (a stable tiebreak)."""
        now = to_utc(now)
        stmt = select(TaskRow)
        if statuses is not None:
            stmt = stmt.where(TaskRow.status.in_([s.value for s in statuses]))
        if due_from is not None:
            stmt = stmt.where(TaskRow.due_at >= to_utc(due_from))
        if due_before is not None:
            stmt = stmt.where(TaskRow.due_at < to_utc(due_before))
        overdue = and_(TaskRow.status.in_(_OPEN_VALUES), TaskRow.due_at.is_not(None), TaskRow.due_at < now)
        if overdue_only:
            stmt = stmt.where(overdue)
        stmt = stmt.order_by(
            case((overdue, 0), else_=1),
            case((TaskRow.due_at.is_(None), 1), else_=0),
            TaskRow.due_at.asc(),
            TaskRow.priority.desc(),
            TaskRow.created_at.asc(),
            TaskRow.id.asc(),
        ).limit(limit)
        return self._run(lambda s: [_task(r) for r in s.scalars(stmt)])

    # ---- reminders ---------------------------------------------------------

    def add_reminder(self, r: Reminder) -> Reminder:
        def work(s: Session) -> None:
            s.add(ReminderRow(
                id=r.reminder_id, task_id=r.task_id, message=r.message, scheduled_at=r.scheduled_at,
                status=r.status.value, timezone=r.timezone,
                recurrence=r.recurrence.to_json() if r.recurrence else None,
                created_at=r.created_at, updated_at=r.updated_at, triggered_at=r.triggered_at,
                cancelled_at=r.cancelled_at, occurrences=r.occurrences, delivery_attempts=r.delivery_attempts,
                claimed_at=r.claimed_at, session_id=r.session_id, source=r.source, extra=dict(r.metadata),
            ))

        self._run(work)
        return r

    def get_reminder(self, reminder_id: str) -> Reminder | None:
        return self._run(lambda s: (lambda r: _reminder(r) if r is not None else None)(s.get(ReminderRow, reminder_id)))

    def list_reminders(
        self,
        *,
        statuses: Collection[ReminderStatus] | None = None,
        scheduled_from: datetime | None = None,
        scheduled_before: datetime | None = None,
        task_id: str | None = None,
        limit: int = 50,
    ) -> list[Reminder]:
        """Reminders soonest first."""
        stmt = select(ReminderRow)
        if statuses is not None:
            stmt = stmt.where(ReminderRow.status.in_([s.value for s in statuses]))
        if scheduled_from is not None:
            stmt = stmt.where(ReminderRow.scheduled_at >= to_utc(scheduled_from))
        if scheduled_before is not None:
            stmt = stmt.where(ReminderRow.scheduled_at < to_utc(scheduled_before))
        if task_id is not None:
            stmt = stmt.where(ReminderRow.task_id == task_id)
        stmt = stmt.order_by(ReminderRow.scheduled_at.asc(), ReminderRow.id.asc()).limit(limit)
        return self._run(lambda s: [_reminder(r) for r in s.scalars(stmt)])

    def find_due_reminders(self, now: datetime, lease_cutoff: datetime, limit: int) -> list[Reminder]:
        """Scheduled reminders whose time has come and that no live delivery lease holds."""
        stmt = (
            select(ReminderRow)
            .where(
                ReminderRow.status == ReminderStatus.SCHEDULED.value,
                ReminderRow.scheduled_at <= to_utc(now),
                or_(ReminderRow.claimed_at.is_(None), ReminderRow.claimed_at <= to_utc(lease_cutoff)),
            )
            .order_by(ReminderRow.scheduled_at.asc(), ReminderRow.id.asc())
            .limit(limit)
        )
        return self._run(lambda s: [_reminder(r) for r in s.scalars(stmt)])

    def claim_reminder(self, reminder_id: str, now: datetime, lease_cutoff: datetime) -> bool:
        """Take the delivery lease on a due reminder. Exactly one caller gets True."""
        stmt = update(ReminderRow).where(
            ReminderRow.id == reminder_id,
            ReminderRow.status == ReminderStatus.SCHEDULED.value,
            ReminderRow.scheduled_at <= to_utc(now),
            or_(ReminderRow.claimed_at.is_(None), ReminderRow.claimed_at <= to_utc(lease_cutoff)),
        ).values(claimed_at=to_utc(now))
        return self._run(lambda s: s.execute(stmt).rowcount == 1)

    def update_reminder(
        self,
        reminder_id: str,
        values: Mapping[str, Any],
        *,
        expected_status: ReminderStatus | None = None,
        expected_claim: datetime | None = None,
        expected_scheduled_at: datetime | None = None,
    ) -> bool:
        """Apply `values` (ReminderRow attribute names) if every given guard still holds. True if it matched."""
        conditions = [ReminderRow.id == reminder_id]
        if expected_status is not None:
            conditions.append(ReminderRow.status == expected_status.value)
        if expected_claim is not None:
            conditions.append(ReminderRow.claimed_at == to_utc(expected_claim))
        if expected_scheduled_at is not None:
            conditions.append(ReminderRow.scheduled_at == to_utc(expected_scheduled_at))
        return self._run(
            lambda s: s.execute(update(ReminderRow).where(*conditions).values(**_normalize(values))).rowcount == 1
        )

    def cancel_reminders_for_task(self, task_id: str, now: datetime) -> int:
        stmt = update(ReminderRow).where(
            ReminderRow.task_id == task_id, ReminderRow.status == ReminderStatus.SCHEDULED.value
        ).values(status=ReminderStatus.CANCELLED.value, cancelled_at=to_utc(now), updated_at=to_utc(now), claimed_at=None)
        return self._run(lambda s: s.execute(stmt).rowcount)
