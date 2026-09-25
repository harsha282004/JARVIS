"""Database engine/session foundation (PostgreSQL via SQLAlchemy).

This module only establishes the connectivity infrastructure that later
phases will build models and migrations on top of. It does not define any
domain schema, and it does not fake a successful connection when the
database is unreachable.
"""

from collections.abc import Callable, Generator
from contextlib import contextmanager
from pathlib import Path
from typing import TypeVar

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import DBAPIError, OperationalError
from sqlalchemy.orm import Session, sessionmaker

from backend.core.config import Settings, get_settings
from backend.core.logging import get_logger
from backend.core.recovery import BackoffPolicy, retry_call

T = TypeVar("T")

logger = get_logger(__name__)

settings = get_settings()



def engine_options(url: str, cfg: Settings) -> dict:
    """Connection-pool options. pool_pre_ping avoids handing out dead connections (it does not mask a database that is
    unreachable at startup); recycling avoids connections a server or firewall silently closed. SQLite (tests) takes no pool options."""
    options: dict = {"pool_pre_ping": True}
    if url.startswith("sqlite"):
        return options
    options.update(pool_size=cfg.DB_POOL_SIZE, max_overflow=cfg.DB_MAX_OVERFLOW, pool_recycle=cfg.DB_POOL_RECYCLE_SECONDS)
    if url.startswith("postgresql"):
        options["connect_args"] = {"connect_timeout": cfg.DB_CONNECT_TIMEOUT_SECONDS}
    return options


engine = create_engine(settings.DATABASE_URL, **engine_options(settings.DATABASE_URL, settings))

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


# ---- reliability helpers ---------------------------------------------------------------------------------------------------

TRANSIENT_ERRORS = (OperationalError, ConnectionError, TimeoutError)


def with_retry(work: Callable[[], T], *, attempts: int = 3, what: str = "database operation", sleep=None) -> T:
    """Run `work` and retry with backoff on connection-type failures only (never on a constraint or SQL error, which would
    fail the same way again). The last error is re-raised, so callers can report the database as unavailable honestly."""
    kwargs = {"sleep": sleep} if sleep is not None else {}
    return retry_call(work, BackoffPolicy(initial_seconds=0.5, max_seconds=5.0, max_attempts=attempts), retry_on=TRANSIENT_ERRORS, what=what, **kwargs)


def _alembic_script_heads() -> set[str]:
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    ini = Path(__file__).resolve().parents[2] / "database" / "alembic.ini"
    config = Config(str(ini))
    config.set_main_option("script_location", str(ini.parent / "migrations"))
    return set(ScriptDirectory.from_config(config).get_heads())


def schema_status(bind: Engine | None = None, expected_heads: set[str] | None = None) -> tuple[bool, str]:
    """(ok, detail). ok is True only if the database is reachable AND its migration revision equals the code's head, so a JARVIS
    that would fail on a missing table is reported at startup instead of at the first request. Never raises."""
    from alembic.runtime.migration import MigrationContext

    target = bind or engine
    try:
        heads = expected_heads if expected_heads is not None else _alembic_script_heads()
        with target.connect() as conn:
            current = set(MigrationContext.configure(conn).get_current_heads())
    except DBAPIError as exc:
        return False, f"database unreachable ({type(exc).__name__})"
    except Exception as exc:  # noqa: BLE001 - alembic/config problems must not crash startup
        return False, f"schema could not be checked ({type(exc).__name__})"
    if not current:
        return False, "no migrations applied: run `alembic -c database/alembic.ini upgrade head`"
    if current != heads:
        return False, "schema is out of date: run `alembic -c database/alembic.ini upgrade head`"
    return True, "schema is current"


def dispose_engine() -> None:
    """Close every pooled connection (graceful shutdown)."""
    try:
        engine.dispose()
    except Exception as exc:  # noqa: BLE001
        logger.warning("Engine dispose failed (%s)", type(exc).__name__)
