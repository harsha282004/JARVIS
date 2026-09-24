"""ORM table for events and deadlines (Phase 11).

Portable column types only, so the same model runs on PostgreSQL and on SQLite in tests. All timestamps are UTC.
`source_id` is never NULL ("" = no id) so the unique (source, dedupe key) constraint also holds on PostgreSQL,
where NULLs are distinct: the same email or document processed twice cannot store the same event twice.
"""

from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from backend.models.base import Base


class EventRow(Base):
    __tablename__ = "events"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    title: Mapped[str] = mapped_column(String(200))
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    event_type: Mapped[str] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(16))
    priority: Mapped[int | None] = mapped_column(Integer, nullable=True)
    start_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    end_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    timezone: Mapped[str] = mapped_column(String(64))
    all_day: Mapped[bool] = mapped_column(Boolean, default=False)
    source_type: Mapped[str] = mapped_column(String(16))
    source_id: Mapped[str] = mapped_column(String(128), default="")
    source_reference: Mapped[str | None] = mapped_column(String(300), nullable=True)
    confidence: Mapped[int] = mapped_column(Integer)
    task_id: Mapped[str | None] = mapped_column(String(32), ForeignKey("tasks.id", ondelete="SET NULL"), nullable=True)
    dedupe_key: Mapped[str] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    extra: Mapped[dict] = mapped_column("metadata", JSON, default=dict)

    __table_args__ = (
        UniqueConstraint("source_type", "source_id", "dedupe_key", name="uq_events_source_dedupe"),
        Index("ix_events_status_start_at", "status", "start_at"),
        Index("ix_events_status_due_at", "status", "due_at"),
        Index("ix_events_task_id", "task_id"),
    )
