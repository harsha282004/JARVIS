"""Shared SQLAlchemy declarative base for future ORM models.

Phase 0 intentionally defines no domain models (users, tasks, emails,
memory records, etc.) — only the base class and a timestamp mixin that
those future models will build on.
"""

from datetime import datetime, timezone

from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class TimestampMixin:
    """Adds created_at / updated_at columns to a model."""

    created_at: Mapped[datetime] = mapped_column(
        default=lambda: datetime.now(timezone.utc)
    )
    updated_at: Mapped[datetime] = mapped_column(
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )
