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


def test_rag_tables_are_created_and_removed_with_the_migration(tmp_path):
    from backend.models.rag import RagChunk, RagDocument

    url = f"sqlite:///{tmp_path / 'rag.db'}"
    command.upgrade(alembic_config(url), "head")
    engine = create_engine(url)
    inspector = inspect(engine)
    assert {"rag_documents", "rag_chunks", "personal_memories"} <= set(inspector.get_table_names())
    for model in (RagDocument, RagChunk):
        assert {c["name"] for c in inspector.get_columns(model.__tablename__)} == {c.name for c in model.__table__.columns}
    assert {i["name"] for i in inspector.get_indexes("rag_chunks")} >= {"ix_rag_chunks_document_id", "ix_rag_chunks_document_order"}
    engine.dispose()

    command.downgrade(alembic_config(url), "0001_personal_memories")
    engine = create_engine(url)
    tables = set(inspect(engine).get_table_names())
    assert "rag_documents" not in tables and "rag_chunks" not in tables and "personal_memories" in tables
    engine.dispose()


def test_rag_postgresql_sql_needs_no_extension():
    out = io.StringIO()
    command.upgrade(alembic_config("postgresql+psycopg2://u:p@localhost/x", out), "head", sql=True)
    sql = out.getvalue()
    assert "CREATE TABLE rag_documents" in sql and "CREATE TABLE rag_chunks" in sql and "BYTEA" in sql
    assert "CREATE EXTENSION" not in sql and "vector" not in sql.lower()


def test_knowledge_graph_tables_migration_and_foreign_keys(tmp_path):
    from backend.models.knowledge_graph import KgEntity, KgProvenance, KgRelationship

    url = f"sqlite:///{tmp_path / 'kg.db'}"
    command.upgrade(alembic_config(url), "head")
    engine = create_engine(url)
    inspector = inspect(engine)
    for model in (KgEntity, KgRelationship, KgProvenance):
        assert {c["name"] for c in inspector.get_columns(model.__tablename__)} == {c.name for c in model.__table__.columns}
    fks = {(fk["referred_table"], tuple(fk["constrained_columns"])) for fk in inspector.get_foreign_keys("kg_relationships")}
    assert fks == {("kg_entities", ("source_entity_id",)), ("kg_entities", ("target_entity_id",))}
    assert [fk["referred_table"] for fk in inspector.get_foreign_keys("kg_provenance")] == ["kg_relationships"]
    engine.dispose()

    command.downgrade(alembic_config(url), "0002_rag_tables")
    engine = create_engine(url)
    tables = set(inspect(engine).get_table_names())
    assert not tables & {"kg_entities", "kg_relationships", "kg_provenance"} and "rag_documents" in tables
    engine.dispose()


def test_knowledge_graph_postgresql_sql_has_foreign_keys_and_no_extension():
    out = io.StringIO()
    command.upgrade(alembic_config("postgresql+psycopg2://u:p@localhost/x", out), "head", sql=True)
    sql = out.getvalue()
    assert "CREATE TABLE kg_entities" in sql and "CREATE TABLE kg_relationships" in sql and "CREATE TABLE kg_provenance" in sql
    assert sql.count("FOREIGN KEY(source_entity_id) REFERENCES kg_entities (id) ON DELETE CASCADE") == 1
    assert "CREATE EXTENSION" not in sql


def test_task_reminder_tables_migration_indexes_and_foreign_key(tmp_path):
    from backend.models.tasks import ReminderRow, TaskRow

    url = f"sqlite:///{tmp_path / 'tasks.db'}"
    command.upgrade(alembic_config(url), "head")
    engine = create_engine(url)
    inspector = inspect(engine)
    for model in (TaskRow, ReminderRow):
        assert {c["name"] for c in inspector.get_columns(model.__tablename__)} == {c.name for c in model.__table__.columns}
    assert {i["name"] for i in inspector.get_indexes("tasks")} >= {"ix_tasks_status_due_at", "ix_tasks_due_at"}
    assert {i["name"] for i in inspector.get_indexes("reminders")} >= {"ix_reminders_status_scheduled_at", "ix_reminders_task_id"}
    fks = inspector.get_foreign_keys("reminders")
    assert [(fk["referred_table"], fk["constrained_columns"]) for fk in fks] == [("tasks", ["task_id"])]
    engine.dispose()


def test_task_reminder_migration_is_reversible_and_leaves_earlier_tables(tmp_path):
    url = f"sqlite:///{tmp_path / 'tasks_down.db'}"
    command.upgrade(alembic_config(url), "head")
    command.downgrade(alembic_config(url), "0003_knowledge_graph")
    engine = create_engine(url)
    tables = set(inspect(engine).get_table_names())
    assert not tables & {"tasks", "reminders"}
    assert {"personal_memories", "rag_documents", "kg_entities"} <= tables  # earlier phases untouched
    engine.dispose()
    command.upgrade(alembic_config(url), "head")  # and it can be applied again
    engine = create_engine(url)
    assert {"tasks", "reminders"} <= set(inspect(engine).get_table_names())
    engine.dispose()


def test_task_reminder_postgresql_sql_uses_timezone_aware_columns_and_cascade():
    out = io.StringIO()
    command.upgrade(alembic_config("postgresql+psycopg2://u:p@localhost/x", out), "head", sql=True)
    sql = out.getvalue()
    assert "CREATE TABLE tasks" in sql and "CREATE TABLE reminders" in sql
    assert "due_at TIMESTAMP WITH TIME ZONE" in sql and "scheduled_at TIMESTAMP WITH TIME ZONE NOT NULL" in sql
    assert "FOREIGN KEY(task_id) REFERENCES tasks (id) ON DELETE CASCADE" in sql
    assert "CREATE INDEX ix_reminders_status_scheduled_at" in sql and "CREATE INDEX ix_tasks_status_due_at" in sql
    assert "CREATE EXTENSION" not in sql


def test_events_table_migration_indexes_unique_constraint_and_foreign_key(tmp_path):
    from backend.models.events import EventRow

    url = f"sqlite:///{tmp_path / 'events.db'}"
    command.upgrade(alembic_config(url), "head")
    engine = create_engine(url)
    inspector = inspect(engine)
    assert {c["name"] for c in inspector.get_columns("events")} == {c.name for c in EventRow.__table__.columns}
    assert {i["name"] for i in inspector.get_indexes("events")} >= {"ix_events_status_start_at", "ix_events_status_due_at", "ix_events_task_id"}
    assert [(u["name"], u["column_names"]) for u in inspector.get_unique_constraints("events")] == [
        ("uq_events_source_dedupe", ["source_type", "source_id", "dedupe_key"])]
    assert [(fk["referred_table"], fk["constrained_columns"]) for fk in inspector.get_foreign_keys("events")] == [("tasks", ["task_id"])]
    engine.dispose()


def test_events_migration_is_reversible_and_additive(tmp_path):
    url = f"sqlite:///{tmp_path / 'events_down.db'}"
    command.upgrade(alembic_config(url), "head")
    command.downgrade(alembic_config(url), "0004_tasks_reminders")
    engine = create_engine(url)
    tables = set(inspect(engine).get_table_names())
    assert "events" not in tables and {"tasks", "reminders", "kg_entities", "personal_memories", "rag_documents"} <= tables
    engine.dispose()
    command.upgrade(alembic_config(url), "head")  # and it applies again
    engine = create_engine(url)
    assert "events" in set(inspect(engine).get_table_names())
    engine.dispose()


def test_events_postgresql_sql_is_timezone_aware_with_provenance_and_no_extension():
    out = io.StringIO()
    command.upgrade(alembic_config("postgresql+psycopg2://u:p@localhost/x", out), "head", sql=True)
    sql = out.getvalue()
    assert "CREATE TABLE events" in sql and "start_at TIMESTAMP WITH TIME ZONE" in sql and "due_at TIMESTAMP WITH TIME ZONE" in sql
    assert "source_type VARCHAR(16) NOT NULL" in sql and "confidence INTEGER NOT NULL" in sql
    assert "CONSTRAINT uq_events_source_dedupe UNIQUE (source_type, source_id, dedupe_key)" in sql
    assert "FOREIGN KEY(task_id) REFERENCES tasks (id) ON DELETE SET NULL" in sql
    assert "CREATE INDEX ix_events_status_start_at" in sql and "CREATE EXTENSION" not in sql
    assert "DROP TABLE" not in sql.split("CREATE TABLE events")[0].split("0004_tasks_reminders")[-1]  # no destructive change to older tables
