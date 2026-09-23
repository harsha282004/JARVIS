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
