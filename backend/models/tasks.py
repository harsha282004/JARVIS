"""ORM tables for tasks and reminders (Phase 9).

Portable column types only (String/Text/JSON/DateTime/Integer), so the same
models run on PostgreSQL in production and on an in-memory SQLite database in
the repository unit tests. All timestamps are stored as UTC.
"""

from datetime import datetime

from sqlalchemy import JSON, DateTime, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from backend.models.base import Base


class TaskRow(Base):
    __tablename__ = "tasks"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    title: Mapped[str] = mapped_column(String(200))
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(16))
    priority: Mapped[int] = mapped_column(Integer)
    due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    session_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    source: Mapped[str] = mapped_column(String(32))
    extra: Mapped[dict] = mapped_column("metadata", JSON, default=dict)

    __table_args__ = (
        Index("ix_tasks_status_due_at", "status", "due_at"),
        Index("ix_tasks_due_at", "due_at"),
    )


class ReminderRow(Base):
    __tablename__ = "reminders"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    task_id: Mapped[str | None] = mapped_column(String(32), ForeignKey("tasks.id", ondelete="CASCADE"), nullable=True)
    message: Mapped[str] = mapped_column(String(300))
    scheduled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(16))
    timezone: Mapped[str] = mapped_column(String(64))
    recurrence: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    triggered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    occurrences: Mapped[int] = mapped_column(Integer, default=0)
    delivery_attempts: Mapped[int] = mapped_column(Integer, default=0)
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    session_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    source: Mapped[str] = mapped_column(String(32))
    extra: Mapped[dict] = mapped_column("metadata", JSON, default=dict)

    __table_args__ = (
        Index("ix_reminders_status_scheduled_at", "status", "scheduled_at"),
        Index("ix_reminders_task_id", "task_id"),
    )
