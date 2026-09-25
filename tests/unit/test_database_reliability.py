"""Database reliability: retries only for transient errors, schema validation, pooling options, integrity (indexes, foreign keys, persistence)."""

from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.core.config import Settings
from backend.core.database import _alembic_script_heads, engine_options, schema_status, with_retry
from backend.models.base import Base
import backend.models.events, backend.models.memory, backend.models.proactive, backend.models.tasks  # noqa: E401,F401


def test_with_retry_recovers_from_transient_connection_errors():
    n = {"c": 0}

    def flaky():
        n["c"] += 1
        if n["c"] < 3:
            raise OperationalError("SELECT 1", {}, Exception("connection refused"))
        return "ok"

    assert with_retry(flaky, attempts=3, sleep=lambda s: None) == "ok" and n["c"] == 3


def test_with_retry_does_not_retry_integrity_errors():
    n = {"c": 0}

    def bad():
        n["c"] += 1
        raise IntegrityError("INSERT", {}, Exception("duplicate"))

    with pytest.raises(IntegrityError):
        with_retry(bad, attempts=3, sleep=lambda s: None)
    assert n["c"] == 1  # a constraint violation would fail the same way again


def test_with_retry_gives_up_and_reraises():
    def down():
        raise OperationalError("SELECT 1", {}, Exception("down"))

    with pytest.raises(OperationalError):
        with_retry(down, attempts=2, sleep=lambda s: None)


def test_engine_options_pool_only_for_server_databases():
    cfg = Settings(_env_file=None, DATABASE_URL="postgresql+psycopg2://u:p@localhost/x", DB_POOL_SIZE=7)
    pg = engine_options(cfg.DATABASE_URL, cfg)
    assert pg["pool_size"] == 7 and pg["pool_pre_ping"] and pg["pool_recycle"] == 1800 and pg["connect_args"]["connect_timeout"] == 10
    assert engine_options("sqlite://", cfg) == {"pool_pre_ping": True}


def test_schema_status_reports_missing_stale_current_and_unreachable():
    heads = _alembic_script_heads()
    assert len(heads) == 1  # one linear migration history
    eng = create_engine("sqlite://", poolclass=StaticPool)
    assert schema_status(eng, heads)[0] is False and "no migrations" in schema_status(eng, heads)[1]
    with eng.begin() as c:
        c.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)"))
        c.execute(text("INSERT INTO alembic_version VALUES ('0001_old')"))
    assert "out of date" in schema_status(eng, heads)[1]
    with eng.begin() as c:
        c.execute(text("UPDATE alembic_version SET version_num = :v"), {"v": next(iter(heads))})
    assert schema_status(eng, heads) == (True, "schema is current")
    down = create_engine("postgresql+psycopg2://u:p@127.0.0.1:1/none", connect_args={"connect_timeout": 1})
    ok, detail = schema_status(down, heads)
    assert not ok and "unreachable" in detail  # reported, never raised, never faked


def test_indexes_needed_for_scheduler_queries_exist():
    eng = create_engine("sqlite://")
    Base.metadata.create_all(eng)
    insp = inspect(eng)
    cols = lambda t: [tuple(i["column_names"]) for i in insp.get_indexes(t)]  # noqa: E731
    assert ("status", "due_at") in cols("tasks") and ("status", "scheduled_at") in cols("reminders")
    assert ("status", "start_at") in cols("events") and ("status", "due_at") in cols("events")


def test_foreign_keys_and_cascade_keep_data_consistent():
    from sqlalchemy import event

    eng = create_engine("sqlite://", poolclass=StaticPool)
    event.listen(eng, "connect", lambda conn, _: conn.execute("PRAGMA foreign_keys=ON"))
    Base.metadata.create_all(eng)
    from agent.tasks.repository import TaskRepository
    from agent.tasks.service import ReminderService, TaskService
    from zoneinfo import ZoneInfo

    factory = sessionmaker(bind=eng, expire_on_commit=False)
    now = lambda: datetime(2026, 9, 24, 9, tzinfo=timezone.utc)  # noqa: E731
    repo = TaskRepository(factory)
    tasks, reminders = TaskService(repo, zone=ZoneInfo("UTC"), clock=now), ReminderService(repo, zone=ZoneInfo("UTC"), clock=now)
    t = tasks.create_task("Persist me")
    reminders.create_reminder("ring", datetime(2026, 9, 25, 9, tzinfo=timezone.utc), task_id=t.task_id)
    # a new service over the same database (a JARVIS restart) sees exactly the same rows
    tasks2 = TaskService(TaskRepository(factory), zone=ZoneInfo("UTC"), clock=now)
    assert [x.title for x in tasks2.list_tasks()] == ["Persist me"]
    tasks2.delete_task(t.task_id)
    assert reminders.list_reminders() == []  # the reminder went with its task: no orphan rows
