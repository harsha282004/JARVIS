"""ORM table for the proactive notification history (Phase 14).

One row per notification the proactive engine decided to deliver. `dedupe_key` is unique, so the same signal can be
claimed by only one caller (exactly one delivery wins) and can never be delivered twice. The row also carries the
provenance (source type/id) and the short reason, which is how "why did you notify me?" is answered.
Portable column types only (PostgreSQL and SQLite). All timestamps are UTC. Message bodies are not stored: `message`
is JARVIS's own short notification sentence (and a generic sentence for email-derived signals).
"""

from datetime import datetime

from sqlalchemy import DateTime, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from backend.models.base import Base


class NotificationRow(Base):
    __tablename__ = "proactive_notifications"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    dedupe_key: Mapped[str] = mapped_column(String(64), unique=True)
    signal_type: Mapped[str] = mapped_column(String(32))
    source_type: Mapped[str] = mapped_column(String(24))
    source_id: Mapped[str] = mapped_column(String(128), default="")
    source_reference: Mapped[str] = mapped_column(String(200), default="")
    message: Mapped[str] = mapped_column(String(300))
    reason: Mapped[str] = mapped_column(String(300))
    priority: Mapped[int] = mapped_column(Integer)
    urgency: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(12))
    channels: Mapped[str] = mapped_column(String(64), default="")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    relevant_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    failed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        Index("ix_proactive_source", "source_type", "source_id", "delivered_at"),
        Index("ix_proactive_status_delivered", "status", "delivered_at"),
    )
