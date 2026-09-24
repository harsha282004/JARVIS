"""ORM tables for the personal knowledge graph (Phase 8): entities, relationships, provenance.

Relational graph in the existing PostgreSQL database (no graph database).
Relationships reference entities by foreign key; provenance rows reference
relationships. Portable column types only, so unit tests run on SQLite.
"""

from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from backend.models.base import Base


class KgEntity(Base):
    __tablename__ = "kg_entities"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    entity_type: Mapped[str] = mapped_column(String(16))
    canonical_name: Mapped[str] = mapped_column(String(200))
    name_key: Mapped[str] = mapped_column(String(200))
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    extra: Mapped[dict] = mapped_column("metadata", JSON, default=dict)
    confidence: Mapped[int] = mapped_column(Integer)
    trust: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(16))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    __table_args__ = (Index("ix_kg_entities_identity", "entity_type", "name_key", "status"),)


class KgRelationship(Base):
    __tablename__ = "kg_relationships"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    source_entity_id: Mapped[str] = mapped_column(String(32), ForeignKey("kg_entities.id", ondelete="CASCADE"), index=True)
    relationship_type: Mapped[str] = mapped_column(String(24))
    target_entity_id: Mapped[str] = mapped_column(String(32), ForeignKey("kg_entities.id", ondelete="CASCADE"), index=True)
    confidence: Mapped[int] = mapped_column(Integer)
    trust: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(16))
    valid_from: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    valid_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    extra: Mapped[dict] = mapped_column("metadata", JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    __table_args__ = (Index("ix_kg_relationships_edge", "source_entity_id", "relationship_type", "target_entity_id"),)


class KgProvenance(Base):
    __tablename__ = "kg_provenance"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    relationship_id: Mapped[str] = mapped_column(String(32), ForeignKey("kg_relationships.id", ondelete="CASCADE"), index=True)
    source_kind: Mapped[str] = mapped_column(String(24))
    source_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    source_name: Mapped[str | None] = mapped_column(String(260), nullable=True)
    page: Mapped[int | None] = mapped_column(Integer, nullable=True)
    chunk_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    confidence: Mapped[int] = mapped_column(Integer)
    trust: Mapped[int] = mapped_column(Integer)
    active: Mapped[bool] = mapped_column(Boolean)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    __table_args__ = (Index("ix_kg_provenance_source", "source_kind", "source_id"),)
