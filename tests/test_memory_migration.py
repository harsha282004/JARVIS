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
