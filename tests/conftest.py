"""Shared pytest fixtures.

Ensures required settings are present for the test environment before any
application module is imported, so config validation doesn't fail tests
just because no local `.env` exists.
"""

import os

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault(
    "DATABASE_URL", "postgresql+psycopg2://jarvis:jarvis@localhost:5432/jarvis_test"
)

from backend.core.config import get_settings  # noqa: E402

get_settings.cache_clear()


# ---- personal-memory database fixtures -------------------------------------
# Repository/service tests run against an isolated in-memory SQLite database
# (never the developer's PostgreSQL). Set JARVIS_TEST_DATABASE_URL to a
# *disposable* PostgreSQL database to run the same tests there as well.

import pytest  # noqa: E402
from sqlalchemy import create_engine, event  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

import backend.models.knowledge_graph  # noqa: E402,F401
import backend.models.memory  # noqa: E402,F401
from backend.models.base import Base  # noqa: E402


@pytest.fixture(params=["sqlite", "postgres"])
def memory_engine(request):
    if request.param == "sqlite":
        engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
        event.listen(engine, "connect", lambda conn, _: conn.execute("PRAGMA foreign_keys=ON"))  # enforce FKs like PostgreSQL
    else:
        url = os.environ.get("JARVIS_TEST_DATABASE_URL")
        if not url:
            pytest.skip("JARVIS_TEST_DATABASE_URL not set (no disposable PostgreSQL test database)")
        engine = create_engine(url)
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    yield engine
    Base.metadata.drop_all(engine)
    engine.dispose()


@pytest.fixture
def session_factory(memory_engine):
    return sessionmaker(bind=memory_engine, expire_on_commit=False)
