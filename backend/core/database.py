"""Database engine/session foundation (PostgreSQL via SQLAlchemy).

This module only establishes the connectivity infrastructure that later
phases will build models and migrations on top of. It does not define any
domain schema, and it does not fake a successful connection when the
database is unreachable.
"""

from collections.abc import Generator
from contextlib import contextmanager

from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from backend.core.config import get_settings
from backend.core.logging import get_logger

logger = get_logger(__name__)

settings = get_settings()

# pool_pre_ping avoids handing out dead connections; it does not mask a
# database that is unreachable at startup.
engine = create_engine(settings.DATABASE_URL, pool_pre_ping=True)

SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


def get_db() -> Generator[Session, None, None]:
    """FastAPI-style dependency yielding a scoped database session."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@contextmanager
def session_scope() -> Generator[Session, None, None]:
    """Context manager for a database session outside of FastAPI's DI."""
    db = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def check_database_connection() -> bool:
    """Verify connectivity to PostgreSQL.

    Returns True if a connection could be established, False otherwise.
    Never raises for a connectivity failure — callers decide how to react
    (e.g. log and continue, or fail startup).
    """
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except Exception as exc:  # noqa: BLE001 - deliberately broad, this is a health check
        logger.error("Database connectivity check failed: %s", exc)
        return False
