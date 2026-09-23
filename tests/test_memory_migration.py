"""Alembic migration for personal_memories.

Applied for real to a scratch SQLite file, and rendered as PostgreSQL DDL
(offline SQL). A live PostgreSQL upgrade is only run when
JARVIS_TEST_DATABASE_URL points at a disposable database.
"""

import argparse
import io
import os
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect

ROOT = Path(__file__).resolve().parents[1]


def alembic_config(url: str, output: io.StringIO | None = None) -> Config:
    cfg = Config(str(ROOT / "database" / "alembic.ini"), output_buffer=output)
    cfg.set_main_option("script_location", str(ROOT / "database" / "migrations"))
    cfg.cmd_opts = argparse.Namespace(x=[f"url={url}"])
    return cfg


def test_upgrade_and_downgrade_on_a_scratch_database(tmp_path):
    url = f"sqlite:///{tmp_path / 'migrate.db'}"
    command.upgrade(alembic_config(url), "head")

    engine = create_engine(url)
    inspector = inspect(engine)
    assert "personal_memories" in inspector.get_table_names()
    columns = {c["name"] for c in inspector.get_columns("personal_memories")}
    assert columns >= {
        "id", "type", "content", "content_norm", "slot", "source", "basis", "confidence",
        "status", "superseded_by", "created_at", "updated_at", "last_accessed_at", "metadata",
    }
    assert {i["name"] for i in inspector.get_indexes("personal_memories")} >= {
        "ix_personal_memories_content_norm", "ix_personal_memories_slot", "ix_personal_memories_status_type",
    }
    engine.dispose()

    command.downgrade(alembic_config(url), "base")
    engine = create_engine(url)
    assert "personal_memories" not in inspect(engine).get_table_names()
    engine.dispose()


def test_migration_matches_the_orm_model(tmp_path):
    from backend.models.memory import PersonalMemory

    url = f"sqlite:///{tmp_path / 'match.db'}"
    command.upgrade(alembic_config(url), "head")
    engine = create_engine(url)
    migrated = {c["name"] for c in inspect(engine).get_columns("personal_memories")}
    assert migrated == {c.name for c in PersonalMemory.__table__.columns}
    engine.dispose()


def test_postgresql_sql_is_generated():
    out = io.StringIO()
    command.upgrade(alembic_config("postgresql+psycopg2://u:p@localhost/x", out), "head", sql=True)
    sql = out.getvalue()
    assert "CREATE TABLE personal_memories" in sql
    assert "created_at TIMESTAMP WITH TIME ZONE NOT NULL" in sql
    assert "CREATE INDEX ix_personal_memories_status_type" in sql


@pytest.mark.skipif(not os.environ.get("JARVIS_TEST_DATABASE_URL"), reason="JARVIS_TEST_DATABASE_URL not set")
def test_upgrade_on_a_disposable_postgresql_database():
    url = os.environ["JARVIS_TEST_DATABASE_URL"]
    command.upgrade(alembic_config(url), "head")
    try:
        assert "personal_memories" in inspect(create_engine(url)).get_table_names()
    finally:
        command.downgrade(alembic_config(url), "base")
