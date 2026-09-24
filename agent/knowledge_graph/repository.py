"""GraphRepository: persistence only (SQLAlchemy). No graph rules live here.

Each call is its own transaction unless wrapped in `unit_of_work()`, which
makes several calls atomic (all written, or nothing). Database failures
surface as GraphStorageError with the exception type only (driver messages
can echo SQL parameters, i.e. names from documents).
"""

from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from datetime import datetime, timezone

from sqlalchemy import or_, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from agent.knowledge_graph.models import (
    Confidence,
    Entity,
    EntityType,
    GraphStatus,
    GraphStorageError,
    Provenance,
    Relationship,
    RelationshipType,
    SourceKind,
    TrustLevel,
)
from backend.models.knowledge_graph import KgEntity, KgProvenance, KgRelationship

SessionFactory = Callable[[], Session]


def _aware(value: datetime | None) -> datetime | None:
    return value.replace(tzinfo=timezone.utc) if value is not None and value.tzinfo is None else value


def _entity(row: KgEntity) -> Entity:
    return Entity(
        entity_id=row.id, entity_type=EntityType(row.entity_type), canonical_name=row.canonical_name,
        name_key=row.name_key, description=row.description, metadata=row.extra or {},
        confidence=Confidence(row.confidence), trust=TrustLevel(row.trust), status=GraphStatus(row.status),
        created_at=_aware(row.created_at), updated_at=_aware(row.updated_at),
    )


def _relationship(row: KgRelationship) -> Relationship:
    return Relationship(
        relationship_id=row.id, source_entity_id=row.source_entity_id,
        relationship_type=RelationshipType(row.relationship_type), target_entity_id=row.target_entity_id,
        confidence=Confidence(row.confidence), trust=TrustLevel(row.trust), status=GraphStatus(row.status),
        valid_from=_aware(row.valid_from), valid_until=_aware(row.valid_until), metadata=row.extra or {},
        created_at=_aware(row.created_at), updated_at=_aware(row.updated_at),
    )


def _provenance(row: KgProvenance) -> Provenance:
    return Provenance.model_construct(
        provenance_id=row.id, source_kind=SourceKind(row.source_kind), source_id=row.source_id,
        source_name=row.source_name, page=row.page, chunk_id=row.chunk_id,
        confidence=Confidence(row.confidence), trust=TrustLevel(row.trust), active=row.active,
        created_at=_aware(row.created_at),
    )


class GraphRepository:
    def __init__(self, session_factory: SessionFactory):
        self._session_factory = session_factory
        self._session: Session | None = None

    @contextmanager
    def unit_of_work(self) -> Iterator[None]:
        """Run several repository calls as one transaction."""
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
            raise GraphStorageError(f"Graph database error ({type(exc).__name__})") from None

    def _run(self, work: Callable[[Session], object]):
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
            raise GraphStorageError(f"Graph database error ({type(exc).__name__})") from None

    # ---- entities ----

    def add_entity(self, e: Entity) -> Entity:
        def work(s: Session) -> None:
            s.add(KgEntity(
                id=e.entity_id, entity_type=e.entity_type.value, canonical_name=e.canonical_name, name_key=e.name_key,
                description=e.description, extra=dict(e.metadata), confidence=int(e.confidence), trust=int(e.trust),
                status=e.status.value, created_at=e.created_at, updated_at=e.updated_at,
            ))

        self._run(work)
        return e

    def get_entity(self, entity_id: str) -> Entity | None:
        return self._run(lambda s: (lambda r: _entity(r) if r else None)(s.get(KgEntity, entity_id)))

    def save_entity(self, e: Entity) -> bool:
        def work(s: Session) -> bool:
            row = s.get(KgEntity, e.entity_id)
            if row is None:
                return False
            row.canonical_name, row.description, row.extra = e.canonical_name, e.description, dict(e.metadata)
            row.confidence, row.trust, row.status = int(e.confidence), int(e.trust), e.status.value
            row.updated_at = e.updated_at
            return True

        return bool(self._run(work))

    def find_entities(
        self, entity_type: EntityType | None = None, name_key: str | None = None,
        status: GraphStatus | None = GraphStatus.ACTIVE, limit: int = 1000,
    ) -> list[Entity]:
        stmt = select(KgEntity)
        if entity_type is not None:
            stmt = stmt.where(KgEntity.entity_type == entity_type.value)
        if name_key is not None:
            stmt = stmt.where(KgEntity.name_key == name_key)
        if status is not None:
            stmt = stmt.where(KgEntity.status == status.value)
        stmt = stmt.order_by(KgEntity.created_at, KgEntity.id).limit(limit)
        return self._run(lambda s: [_entity(r) for r in s.scalars(stmt)])

    def search_entities(self, keys: Sequence[str] | None, types: Sequence[EntityType] | None, limit: int) -> list[Entity]:
        stmt = select(KgEntity).where(KgEntity.status == GraphStatus.ACTIVE.value)
        if types:
            stmt = stmt.where(KgEntity.entity_type.in_([t.value for t in types]))
        if keys:
            stmt = stmt.where(or_(*(KgEntity.name_key.contains(k) for k in keys)))
        stmt = stmt.order_by(KgEntity.name_key, KgEntity.id).limit(limit)
        return self._run(lambda s: [_entity(r) for r in s.scalars(stmt)])

    # ---- relationships ----

    def add_relationship(self, r: Relationship) -> Relationship:
        def work(s: Session) -> None:
            s.add(KgRelationship(
                id=r.relationship_id, source_entity_id=r.source_entity_id, relationship_type=r.relationship_type.value,
                target_entity_id=r.target_entity_id, confidence=int(r.confidence), trust=int(r.trust),
                status=r.status.value, valid_from=r.valid_from, valid_until=r.valid_until, extra=dict(r.metadata),
                created_at=r.created_at, updated_at=r.updated_at,
            ))

        self._run(work)
        return r

    def get_relationship(self, relationship_id: str) -> Relationship | None:
        return self._run(lambda s: (lambda r: _relationship(r) if r else None)(s.get(KgRelationship, relationship_id)))

    def save_relationship(self, r: Relationship) -> bool:
        def work(s: Session) -> bool:
            row = s.get(KgRelationship, r.relationship_id)
            if row is None:
                return False
            row.confidence, row.trust, row.status = int(r.confidence), int(r.trust), r.status.value
            row.valid_from, row.valid_until, row.extra, row.updated_at = r.valid_from, r.valid_until, dict(r.metadata), r.updated_at
            return True

        return bool(self._run(work))

    def find_relationships(
        self, source_id: str | None = None, target_id: str | None = None,
        relationship_type: RelationshipType | None = None, status: GraphStatus | None = GraphStatus.ACTIVE,
        involving: str | None = None, limit: int = 1000,
    ) -> list[Relationship]:
        stmt = select(KgRelationship)
        if source_id is not None:
            stmt = stmt.where(KgRelationship.source_entity_id == source_id)
        if target_id is not None:
            stmt = stmt.where(KgRelationship.target_entity_id == target_id)
        if involving is not None:
            stmt = stmt.where(or_(KgRelationship.source_entity_id == involving, KgRelationship.target_entity_id == involving))
        if relationship_type is not None:
            stmt = stmt.where(KgRelationship.relationship_type == relationship_type.value)
        if status is not None:
            stmt = stmt.where(KgRelationship.status == status.value)
        stmt = stmt.order_by(KgRelationship.created_at, KgRelationship.id).limit(limit)
        return self._run(lambda s: [_relationship(r) for r in s.scalars(stmt)])

    # ---- provenance ----

    def add_provenance(self, relationship_id: str, p: Provenance) -> Provenance:
        def work(s: Session) -> None:
            s.add(KgProvenance(
                id=p.provenance_id, relationship_id=relationship_id, source_kind=p.source_kind.value,
                source_id=p.source_id, source_name=p.source_name, page=p.page, chunk_id=p.chunk_id,
                confidence=int(p.confidence), trust=int(p.trust), active=p.active, created_at=p.created_at,
            ))

        self._run(work)
        return p

    def list_provenance(self, relationship_id: str, active_only: bool = False) -> list[Provenance]:
        stmt = select(KgProvenance).where(KgProvenance.relationship_id == relationship_id)
        if active_only:
            stmt = stmt.where(KgProvenance.active.is_(True))
        stmt = stmt.order_by(KgProvenance.created_at, KgProvenance.id)
        return self._run(lambda s: [_provenance(r) for r in s.scalars(stmt)])

    def set_provenance_active(self, provenance_id: str, active: bool) -> None:
        def work(s: Session) -> None:
            row = s.get(KgProvenance, provenance_id)
            if row is not None:
                row.active = active

        self._run(work)

    def find_provenance_by_source(self, source_kind: SourceKind, source_id: str) -> list[tuple[str, Provenance]]:
        """(relationship_id, provenance) for every ACTIVE provenance row from that source."""
        stmt = select(KgProvenance).where(
            KgProvenance.source_kind == source_kind.value, KgProvenance.source_id == source_id,
            KgProvenance.active.is_(True),
        )
        return self._run(lambda s: [(r.relationship_id, _provenance(r)) for r in s.scalars(stmt)])

    def count(self) -> dict[str, int]:
        from sqlalchemy import func

        def work(s: Session) -> dict[str, int]:
            return {
                "entities": s.scalar(select(func.count()).select_from(KgEntity).where(KgEntity.status == "active")) or 0,
                "relationships": s.scalar(select(func.count()).select_from(KgRelationship).where(KgRelationship.status == "active")) or 0,
            }

        return self._run(work)
