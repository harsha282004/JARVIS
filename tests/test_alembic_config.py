"""The Alembic configuration works from ANY working directory (script_location was relative to the current directory, so `alembic -c database/alembic.ini upgrade head`
from the repository root looked for ./migrations and failed), the migrations build exactly the schema the models describe, and the health check reports the real state."""

import subprocess
import sys
from pathlib import Path

import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import create_engine, inspect

ROOT = Path(__file__).resolve().parents[1]
INI = ROOT / "database" / "alembic.ini"


def alembic(*args, cwd=ROOT, url=None):
    cmd = [sys.executable, "-m", "alembic", "-c", str(INI), *(["-x", f"url={url}"] if url else []), *args]
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=180)


def test_ini_paths_are_not_cwd_relative():
    text = INI.read_text(encoding="utf-8")
    assert "script_location = %(here)s/migrations" in text and "prepend_sys_path = %(here)s/.." in text
    assert (ROOT / "database" / "migrations" / "versions").is_dir() and not (ROOT / "migrations").exists()     # no second migration tree


@pytest.mark.parametrize("cwd", [ROOT, ROOT / "database", ROOT / "backend"])
def test_upgrade_works_from_any_directory_and_matches_the_models(tmp_path, cwd):
    url = f"sqlite:///{(tmp_path / 'm.db').as_posix()}"
    up = alembic("upgrade", "head", cwd=cwd, url=url)
    assert up.returncode == 0, up.stderr[-400:]
    heads = alembic("heads", cwd=cwd, url=url).stdout
    current = alembic("current", cwd=cwd, url=url).stdout
    assert "(head)" in heads and heads.split()[0] in current

    import backend.models.events, backend.models.hub, backend.models.knowledge_graph, backend.models.memory, backend.models.proactive, backend.models.rag, backend.models.tasks  # noqa: E401,F401
    from backend.core.database import schema_status
    from backend.models.base import Base

    engine = create_engine(url)
    try:
        assert set(Base.metadata.tables) <= set(inspect(engine).get_table_names())
        with engine.connect() as conn:
            assert compare_metadata(MigrationContext.configure(conn), Base.metadata) == []           # zero drift between migrations and models
        assert schema_status(engine) == (True, "schema is current")
    finally:
        engine.dispose()


def test_health_reports_missing_migrations_honestly_and_a_migrated_db_as_current(tmp_path):
    from backend.core.database import schema_status
    from backend.models.base import Base

    import backend.models.hub, backend.models.memory  # noqa: E401,F401

    raw = create_engine(f"sqlite:///{(tmp_path / 'raw.db').as_posix()}")
    Base.metadata.create_all(raw)                                       # tables but no alembic_version: NOT a migrated database
    ok, detail = schema_status(raw)
    raw.dispose()
    assert not ok and "no migrations applied" in detail and "alembic -c database/alembic.ini upgrade head" in detail
