"""ORM table for normalized integration items (Phase 18).

One row per (source, kind, source_id): re-retrieving the same email/event/commit updates the row instead of adding one, so synchronization is
idempotent. Only short titles/summaries and small metadata are stored, never whole emails or documents. Portable column types (PostgreSQL and
SQLite); timestamps are UTC. `deleted_at` hides an item that disappeared at the source without losing its history.
"""

from datetime import datetime

from sqlalchemy import JSON, DateTime, Index, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from backend.models.base import Base


class HubItemRow(Base):
    __tablename__ = "hub_items"

    id: Mapped[str] = mapped_column(String(24), primary_key=True)
    source: Mapped[str] = mapped_column(String(24))
    kind: Mapped[str] = mapped_column(String(24))
    source_id: Mapped[str] = mapped_column(String(200))
    external_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    title: Mapped[str] = mapped_column(String(300))
    summary: Mapped[str] = mapped_column(Text, default="")
    extra: Mapped[dict] = mapped_column(JSON, default=dict)
    confidence: Mapped[str] = mapped_column(String(8), default="high")
    content_hash: Mapped[str] = mapped_column(String(40))
    source_timestamp: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    retrieved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        UniqueConstraint("source", "kind", "source_id", name="uq_hub_items_source"),
        Index("ix_hub_items_source_kind_ts", "source", "kind", "source_timestamp"),
        Index("ix_hub_items_retrieved", "retrieved_at"),
    )
