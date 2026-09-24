"""Proactive configuration, bootstrap wiring, scheduler reuse (no second scheduler) and the reversible migration."""

import argparse
import io
import subprocess
import threading
from datetime import timedelta
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from pydantic import ValidationError
from sqlalchemy import create_engine, inspect

from agent.proactive.engine import ProactiveEngine
from agent.proactive.sources import CalendarSignalSource, EventSignalSource, GmailSignalSource, TaskSignalSource
from agent.tasks.notifications import AnnouncementQueue
from agent.tasks.scheduler import ReminderScheduler
from agent.tasks.service import ReminderService
from agent.tasks.repository import TaskRepository
from agent.tasks.system import TaskSystem
from agent.tasks.timeparse import TimeParser
from backend.core.config import Settings
from tests.proactive_helpers import NOW, Env, RecordingNotifier
from tests.task_helpers import IST, Clock
from voice.bootstrap import build_proactive_engine, build_proactive_tools_for, build_reminder_scheduler, build_task_system

ROOT = Path(__file__).resolve().parents[1]


def settings(**overrides) -> Settings:
    base = {
        "DATABASE_URL": "postgresql+psycopg2://jarvis:jarvis@localhost:5432/jarvis_test",
        "JARVIS_MEMORY_ENABLED": False, "JARVIS_RAG_ENABLED": False, "JARVIS_KG_ENABLED": False,
        "JARVIS_TASKS_ENABLED": True, "JARVIS_REMINDERS_ENABLED": True, "JARVIS_EVENTS_ENABLED": True,
        "JARVIS_GMAIL_ENABLED": False, "JARVIS_CALENDAR_ENABLED": False, "JARVIS_MESSAGING_ENABLED": False,
        "JARVIS_TIMEZONE": "Asia/Kolkata", "JARVIS_PROACTIVE_ENABLED": True,
    }
    base.update(overrides)
    return Settings(_env_file=None, **base)


# ---- settings -----------------------------------------------------------------------------------------------------------


def test_proactive_settings_have_safe_defaults_and_bounds():
    s = Settings(_env_file=None, DATABASE_URL="postgresql+psycopg2://u:p@localhost/x")
    assert s.JARVIS_PROACTIVE_ENABLED is False  # off until the user turns it on
    assert (s.JARVIS_PROACTIVE_POLL_SECONDS, s.JARVIS_PROACTIVE_LOOKAHEAD_MINUTES, s.JARVIS_PROACTIVE_COOLDOWN_MINUTES) == (60.0, 1440, 60)
    assert (s.JARVIS_PROACTIVE_QUIET_HOURS_ENABLED, s.JARVIS_PROACTIVE_QUIET_START, s.JARVIS_PROACTIVE_QUIET_END) == (True, "23:00", "07:00")
    assert (s.JARVIS_PROACTIVE_MAX_PER_HOUR, s.JARVIS_PROACTIVE_EXTERNAL_POLL_MINUTES, s.JARVIS_PROACTIVE_CALENDAR, s.JARVIS_PROACTIVE_GMAIL) == (6, 10.0, True, False)
    for bad in ({"JARVIS_PROACTIVE_POLL_SECONDS": 1}, {"JARVIS_PROACTIVE_LOOKAHEAD_MINUTES": 5}, {"JARVIS_PROACTIVE_LOOKAHEAD_MINUTES": 20000},
                {"JARVIS_PROACTIVE_COOLDOWN_MINUTES": -1}, {"JARVIS_PROACTIVE_MAX_PER_HOUR": 0}, {"JARVIS_PROACTIVE_EXTERNAL_POLL_MINUTES": 1},
                {"JARVIS_PROACTIVE_QUIET_START": "25:00"}, {"JARVIS_PROACTIVE_QUIET_END": "7"}, {"JARVIS_PROACTIVE_QUIET_START": "ab:cd"}, {"JARVIS_PROACTIVE_QUIET_END": "07:60"}):
        with pytest.raises(ValidationError):
            Settings(_env_file=None, DATABASE_URL="postgresql+psycopg2://u:p@localhost/x", **bad)
    assert Settings(_env_file=None, DATABASE_URL="postgresql+psycopg2://u:p@localhost/x", JARVIS_PROACTIVE_QUIET_START="9:05").JARVIS_PROACTIVE_QUIET_START == "09:05"


# ---- bootstrap ------------------------------------------------------------------------------------------------------------


def system_for(cfg) -> TaskSystem | None:
    return build_task_system(cfg)


def test_disabled_builds_nothing():
    cfg = settings(JARVIS_PROACTIVE_ENABLED=False)
    system = system_for(cfg)
    assert build_proactive_engine(cfg, system, lambda t, m: None) is None and build_proactive_tools_for(cfg, IST) == []


def test_enabled_engine_reuses_the_existing_services_and_notifiers():
    cfg = settings()
    system = system_for(cfg)
    engine = build_proactive_engine(cfg, system, lambda title, text: None)
    assert isinstance(engine, ProactiveEngine) and engine.enabled
    kinds = [type(s) for s in engine._sources]
    assert kinds == [TaskSignalSource, EventSignalSource]  # Gmail and Calendar are not enabled: they are not observed
    assert set(engine._notifiers) == {c for c in engine._notifiers} and {c.value for c in engine._notifiers} == {"desktop", "voice"}
    assert engine._notifiers[next(c for c in engine._notifiers if c.value == "voice")]._queue is system.announcements  # the existing announcement queue


def test_external_sources_need_their_integration_and_their_own_switch():
    system = system_for(settings())
    both = build_proactive_engine(settings(JARVIS_CALENDAR_ENABLED=True, JARVIS_GMAIL_ENABLED=True, JARVIS_PROACTIVE_GMAIL=True), system, lambda t, m: None)
    assert [type(s) for s in both._sources] == [TaskSignalSource, EventSignalSource, CalendarSignalSource, GmailSignalSource]
    assert both._sources[1]._calendar_active is True  # the events source then skips calendar mirrors
    no_gmail_opt_in = build_proactive_engine(settings(JARVIS_CALENDAR_ENABLED=True, JARVIS_GMAIL_ENABLED=True), system, lambda t, m: None)
    assert GmailSignalSource not in [type(s) for s in no_gmail_opt_in._sources]  # Gmail is opt-in even when enabled
    calendar_off = build_proactive_engine(settings(JARVIS_CALENDAR_ENABLED=True, JARVIS_PROACTIVE_CALENDAR=False), system, lambda t, m: None)
    assert CalendarSignalSource not in [type(s) for s in calendar_off._sources]
    gmail_no_integration = build_proactive_engine(settings(JARVIS_PROACTIVE_GMAIL=True), system, lambda t, m: None)
    assert GmailSignalSource not in [type(s) for s in gmail_no_integration._sources]


def test_no_channel_or_no_source_means_no_engine_and_no_crash():
    system = system_for(settings())
    assert build_proactive_engine(settings(JARVIS_REMINDER_VOICE_NOTIFICATIONS=False), system, None) is None  # nowhere to notify
    assert build_proactive_engine(settings(JARVIS_REMINDER_VOICE_NOTIFICATIONS=False, JARVIS_REMINDER_DESKTOP_NOTIFICATIONS=False), system, lambda t, m: None) is None
    nothing = settings(JARVIS_TASKS_ENABLED=False, JARVIS_REMINDERS_ENABLED=False, JARVIS_EVENTS_ENABLED=False)
    assert build_proactive_engine(nothing, system_for(nothing), lambda t, m: None) is None  # nothing to observe
    no_tray = build_proactive_engine(settings(), system, None)
    assert {c.value for c in no_tray._notifiers} == {"voice"}  # the tray is optional: voice alone still works


def test_the_explain_tool_is_registered_only_when_proactive_is_enabled():
    tools = build_proactive_tools_for(settings(), IST)
    assert [t.name for t in tools] == ["proactive_explain"] and (tools[0].requires_permission, tools[0].risk.name) == (False, "LOW")


def test_an_invalid_timezone_or_setting_never_crashes_startup(monkeypatch):
    system = system_for(settings())
    import voice.bootstrap as bootstrap

    monkeypatch.setattr(bootstrap, "parse_clock", lambda value: (_ for _ in ()).throw(ValueError("bad")))
    assert build_proactive_engine(settings(), system, lambda t, m: None) is None


# ---- one scheduler, reused --------------------------------------------------------------------------------------------------


def test_reminders_and_proactive_share_one_scheduler_thread():
    cfg = settings()
    system = system_for(cfg)
    engine = build_proactive_engine(cfg, system, lambda t, m: None)
    scheduler = build_reminder_scheduler(cfg, system, lambda t, m: None, engine)
    assert isinstance(scheduler, ReminderScheduler) and scheduler._extra_passes == [engine.run_once] and scheduler._reminders is system.reminders
    plain = build_reminder_scheduler(cfg, system, lambda t, m: None)
    assert plain._extra_passes == []  # unchanged Phase 9 behaviour without the engine


def test_proactive_can_run_with_reminders_disabled_and_reminders_with_proactive_disabled():
    cfg = settings(JARVIS_REMINDERS_ENABLED=False)
    system = system_for(cfg)
    engine = build_proactive_engine(cfg, system, lambda t, m: None)
    scheduler = build_reminder_scheduler(cfg, system, lambda t, m: None, engine)
    assert scheduler is not None and scheduler._reminders is None and scheduler._extra_passes == [engine.run_once]
    assert build_reminder_scheduler(cfg, system, lambda t, m: None) is None  # neither reminders nor proactive: no scheduler at all
    cfg2 = settings(JARVIS_PROACTIVE_ENABLED=False)
    system2 = system_for(cfg2)
    only_reminders = build_reminder_scheduler(cfg2, system2, lambda t, m: None, build_proactive_engine(cfg2, system2, lambda t, m: None))
    assert only_reminders._reminders is system2.reminders and only_reminders._extra_passes == []


def test_the_extra_pass_runs_after_the_reminder_pass_and_its_failure_never_affects_reminders(session_factory):
    clock = Clock(NOW)
    reminders = ReminderService(TaskRepository(session_factory), zone=IST, clock=clock)
    reminders.create_reminder("Take medicine", NOW + timedelta(minutes=5))
    rings, order = RecordingNotifier(), []

    def bad_pass():
        order.append("bad")
        raise RuntimeError("proactive exploded")

    scheduler = ReminderScheduler(reminders, rings, clock=clock, extra_passes=[bad_pass, lambda: order.append("good")])
    clock.advance(minutes=6)
    assert scheduler.run_once() == 1 and rings.messages == ["Reminder: Take medicine"] and order == ["bad", "good"]


def test_the_extra_pass_still_runs_when_the_reminder_database_fails(session_factory):
    clock, calls = Clock(NOW), []
    reminders = ReminderService(TaskRepository(session_factory), zone=IST, clock=clock)
    reminders.find_due_reminders = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("db down"))
    scheduler = ReminderScheduler(reminders, RecordingNotifier(), clock=clock, extra_passes=[lambda: calls.append(1)])
    with pytest.raises(RuntimeError):
        scheduler.run_once()  # the existing behaviour: the loop backs off
    assert calls == [1]


def test_the_real_scheduler_thread_runs_the_engine_and_stops_cleanly(session_factory):
    env = Env(session_factory, calendar=False)
    env.tasks.create_task("Report", due_at=NOW + timedelta(minutes=40))
    ran = threading.Event()
    scheduler = ReminderScheduler(None, None, poll_seconds=0.05, clock=env.clock, extra_passes=[lambda: (env.engine.run_once(), ran.set())])
    before = {t.name for t in threading.enumerate()}
    assert scheduler.start() is True and scheduler.start() is False  # never a second thread
    assert ran.wait(10) and scheduler.stop(5) is True
    assert len(env.desktop.messages) == 1  # many polls, one notification
    assert {t.name for t in threading.enumerate()} - before == set()  # the thread is gone


def test_no_second_scheduler_or_thread_exists_in_the_proactive_package():
    import re

    for path in (ROOT / "agent" / "proactive").glob("*.py"):
        text = path.read_text(encoding="utf-8")
        assert not re.search(r"threading\.Thread|apscheduler|import schedule|asyncio\.create_task|while True|time\.sleep", text), path.name


# ---- migration --------------------------------------------------------------------------------------------------------------


def alembic_config(url: str, output: io.StringIO | None = None) -> Config:
    cfg = Config(str(ROOT / "database" / "alembic.ini"), output_buffer=output)
    cfg.set_main_option("script_location", str(ROOT / "database" / "migrations"))
    cfg.cmd_opts = argparse.Namespace(x=[f"url={url}"])
    return cfg


def test_migration_upgrades_downgrades_and_matches_the_model(tmp_path):
    from backend.models.proactive import NotificationRow

    url = f"sqlite:///{tmp_path / 'proactive.db'}"
    command.upgrade(alembic_config(url), "head")
    engine = create_engine(url)
    inspector = inspect(engine)
    assert "proactive_notifications" in inspector.get_table_names()
    assert {c["name"] for c in inspector.get_columns("proactive_notifications")} == {c.name for c in NotificationRow.__table__.columns}
    assert {i["name"] for i in inspector.get_indexes("proactive_notifications")} >= {"ix_proactive_source", "ix_proactive_status_delivered"}
    assert any(u["column_names"] == ["dedupe_key"] for u in inspector.get_unique_constraints("proactive_notifications")) or any(
        i["unique"] and i["column_names"] == ["dedupe_key"] for i in inspector.get_indexes("proactive_notifications"))  # the atomic-claim guarantee
    engine.dispose()
    command.downgrade(alembic_config(url), "0005_events")  # reversible: drops only this table
    engine = create_engine(url)
    tables = inspect(engine).get_table_names()
    assert "proactive_notifications" not in tables and "events" in tables and "tasks" in tables
    engine.dispose()
    command.upgrade(alembic_config(url), "head")  # and can be applied again


def test_postgresql_ddl_is_generated_and_is_additive_only():
    out = io.StringIO()
    command.upgrade(alembic_config("postgresql+psycopg2://u:p@localhost/x", out), "head", sql=True)
    sql = out.getvalue().split("Running upgrade 0005_events -> 0006_proactive")[1]
    assert "CREATE TABLE proactive_notifications" in sql and "dedupe_key VARCHAR(64) NOT NULL" in sql and "UNIQUE (dedupe_key)" in sql
    assert "DROP" not in sql and "ALTER TABLE" not in sql  # no existing table is changed


# ---- git hygiene ----------------------------------------------------------------------------------------------------------------


def test_no_new_secret_or_token_settings_were_added():
    text = (ROOT / ".env.example").read_text(encoding="utf-8")
    block = text.split("# --- Proactive intelligence")[1].split("# ---")[0]
    assert "TOKEN" not in block.upper() and "SECRET" not in block.upper() and "PASSWORD" not in block.upper() and "KEY=" not in block.upper()


@pytest.mark.skipif(not (ROOT / ".git").exists(), reason="not a git checkout")
def test_the_proactive_files_are_not_git_ignored():
    for path in ("agent/proactive/engine.py", "docs/proactive-intelligence.md", "database/migrations/versions/0006_create_proactive_notifications.py"):
        assert subprocess.run(["git", "check-ignore", "-q", path], cwd=ROOT).returncode == 1, path
