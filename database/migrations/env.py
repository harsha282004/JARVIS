"""Alembic migration environment.

Reads DATABASE_URL from application settings (not from alembic.ini) so
there is a single source of truth for the connection string.
"""

import sys
from pathlib import Path

from alembic import context
from sqlalchemy import engine_from_config, pool

# Allow `import backend...` when alembic is invoked from database/alembic.ini
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from backend.core.config import get_settings  # noqa: E402
from backend.models.base import Base  # noqa: E402

config = context.config
settings = get_settings()
config.set_main_option("sqlalchemy.url", settings.DATABASE_URL)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
