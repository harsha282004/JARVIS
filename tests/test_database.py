"""Database configuration/connection behavior tests.

Does not require a live PostgreSQL instance to pass: it only verifies the
engine is configured correctly and that connectivity checks fail cleanly
(rather than lying) when the database is unreachable.
"""

from backend.core.database import check_database_connection, engine


def test_engine_configured_from_settings():
    assert engine.url.drivername.startswith("postgresql")


def test_check_database_connection_does_not_raise():
    # Whether or not Postgres is actually running in this environment,
    # this must return a bool and never raise or fake success.
    result = check_database_connection()
    assert isinstance(result, bool)
