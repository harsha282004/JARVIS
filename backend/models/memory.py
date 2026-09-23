"""ORM table for persistent personal memory (Phase 6).

Portable column types only (String/Text/JSON/DateTime), so the same model
runs on PostgreSQL in production and on an in-memory SQLite database in the
repository unit tests. It holds extracted memories only, never transcripts.
"""

from datetime import datetime

from sqlalchemy import JSON, DateTime, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from backend.models.base import Base


class PersonalMemory(Base):
    __tablename__ = "personal_memories"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    type: Mapped[str] = mapped_column(String(16))
    content: Mapped[str] = mapped_column(Text)
    # Lowercased, punctuation-free form used for deduplication and keyword search.
    content_norm: Mapped[str] = mapped_column(String(400), index=True)
    slot: Mapped[str | None] = mapped_column(String(120), nullable=True, index=True)
    source: Mapped[str] = mapped_column(String(32))
    basis: Mapped[str] = mapped_column(String(16))
    confidence: Mapped[int] = mapped_column()
    status: Mapped[str] = mapped_column(String(16))
    superseded_by: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_accessed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    extra: Mapped[dict] = mapped_column("metadata", JSON, default=dict)

    __table_args__ = (Index("ix_personal_memories_status_type", "status", "type"),)
