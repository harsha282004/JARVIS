"""MemoryRepository: persistence only (SQLAlchemy). No memory rules live here.

Uses the project's existing session factory (backend.core.database.SessionLocal
in production; an isolated one in tests). Each call is its own transaction.
Database failures surface as MemoryStorageError with no memory content in the
message.
"""

from collections.abc import Callable, Sequence
from datetime import datetime, timezone

from sqlalchemy import delete, or_, select, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from agent.memory.models import (
    Confidence,
    Memory,
    MemoryBasis,
    MemorySource,
    MemoryStatus,
    MemoryStorageError,
    MemoryType,
)
from agent.memory.normalize import normalize_content
from backend.models.memory import PersonalMemory

SessionFactory = Callable[[], Session]


def _aware(value: datetime | None) -> datetime | None:
    # SQLite drops tzinfo; PostgreSQL keeps it. Everything is stored as UTC.
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def _to_model(row: PersonalMemory) -> Memory:
    return Memory(
        memory_id=row.id,
        type=MemoryType(row.type),
        content=row.content,
        source=MemorySource(row.source),
        basis=MemoryBasis(row.basis),
        confidence=Confidence(row.confidence),
        slot=row.slot,
        metadata=row.extra or {},
        status=MemoryStatus(row.status),
        created_at=_aware(row.created_at),
        updated_at=_aware(row.updated_at),
        last_accessed_at=_aware(row.last_accessed_at),
        superseded_by=row.superseded_by,
    )


def _apply(row: PersonalMemory, memory: Memory) -> None:
    row.type = memory.type.value
    row.content = memory.content
    row.content_norm = normalize_content(memory.content)[:400]
    row.slot = memory.slot
    row.source = memory.source.value
    row.basis = memory.basis.value
    row.confidence = int(memory.confidence)
    row.status = memory.status.value
    row.superseded_by = memory.superseded_by
    row.created_at = memory.created_at
    row.updated_at = memory.updated_at
    row.last_accessed_at = memory.last_accessed_at
    row.extra = dict(memory.metadata)


class MemoryRepository:
    def __init__(self, session_factory: SessionFactory):
        self._session_factory = session_factory

    def _run(self, work: Callable[[Session], object]):
        try:
            with self._session_factory() as session:
                result = work(session)
                session.commit()
                return result
        except SQLAlchemyError as exc:
            # Only the exception class: driver messages can echo SQL parameters (memory content).
            raise MemoryStorageError(f"Memory database error ({type(exc).__name__})") from None

    def add(self, memory: Memory) -> Memory:
        def work(session: Session) -> None:
            row = PersonalMemory(id=memory.memory_id)
            _apply(row, memory)
            session.add(row)

        self._run(work)
        return memory

    def get(self, memory_id: str) -> Memory | None:
        def work(session: Session) -> Memory | None:
            row = session.get(PersonalMemory, memory_id)
            return _to_model(row) if row else None

        return self._run(work)

    def save(self, memory: Memory) -> bool:
        """Write all fields of an existing memory. Returns False if it does not exist."""

        def work(session: Session) -> bool:
            row = session.get(PersonalMemory, memory.memory_id)
            if row is None:
                return False
            _apply(row, memory)
            return True

        return self._run(work)

    def active_with_norm(self, type_: MemoryType, content_norm: str) -> list[Memory]:
        return self._query(PersonalMemory.type == type_.value, PersonalMemory.content_norm == content_norm[:400])

    def active_with_slot(self, type_: MemoryType, slot: str) -> list[Memory]:
        return self._query(PersonalMemory.type == type_.value, PersonalMemory.slot == slot)

    def active_containing(self, type_: MemoryType, fragment_norm: str) -> list[Memory]:
        return self._query(PersonalMemory.type == type_.value, PersonalMemory.content_norm.contains(fragment_norm))

    def _query(self, *conditions) -> list[Memory]:
        stmt = (
            select(PersonalMemory)
            .where(PersonalMemory.status == MemoryStatus.ACTIVE.value, *conditions)
            .order_by(PersonalMemory.updated_at.desc(), PersonalMemory.id)
        )
        return self._run(lambda s: [_to_model(r) for r in s.scalars(stmt)])

    def search(
        self,
        keywords: Sequence[str] | None = None,
        types: Sequence[MemoryType] | None = None,
        statuses: Sequence[MemoryStatus] | None = None,
        limit: int = 20,
    ) -> list[Memory]:
        """Rows whose text contains ANY keyword (all rows if none given)."""
        stmt = select(PersonalMemory)
        if statuses is not None:
            stmt = stmt.where(PersonalMemory.status.in_([s.value for s in statuses]))
        if types:
            stmt = stmt.where(PersonalMemory.type.in_([t.value for t in types]))
        if keywords:
            stmt = stmt.where(or_(*(PersonalMemory.content_norm.contains(k) for k in keywords)))
        stmt = stmt.order_by(PersonalMemory.updated_at.desc(), PersonalMemory.id).limit(limit)
        return self._run(lambda s: [_to_model(r) for r in s.scalars(stmt)])

    def touch(self, memory_ids: Sequence[str], when: datetime) -> None:
        if not memory_ids:
            return
        stmt = update(PersonalMemory).where(PersonalMemory.id.in_(list(memory_ids))).values(last_accessed_at=when)
        self._run(lambda s: s.execute(stmt))

    def purge(self, memory_id: str) -> bool:
        """Physically remove a row (privacy erase). Returns False if it did not exist."""
        return bool(self._run(lambda s: s.execute(delete(PersonalMemory).where(PersonalMemory.id == memory_id)).rowcount))
