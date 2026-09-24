"""Briefing configuration and bootstrap wiring: it reuses the existing services, adds no scheduler, notifier or table."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from agent.briefing.intents import BRIEFING_ACTION_NAMES
from agent.briefing.service import BriefingService
from backend.core.config import Settings
from backend.core.security import PermissionStatus
from tests.briefing_helpers import IST
from tests.gmail_helpers import ScriptedLLM
from voice.bootstrap import _build_conversation, build_briefing_service, build_briefing_tools_for, build_task_system

ROOT = Path(__file__).resolve().parents[1]
DB = "postgresql+psycopg2://jarvis:jarvis@localhost:5432/jarvis_test"


def settings(**overrides) -> Settings:
    base = {
        "DATABASE_URL": DB, "JARVIS_MEMORY_ENABLED": False, "JARVIS_RAG_ENABLED": False, "JARVIS_KG_ENABLED": False, "JARVIS_TASKS_ENABLED": True,
        "JARVIS_REMINDERS_ENABLED": True, "JARVIS_EVENTS_ENABLED": True, "JARVIS_GMAIL_ENABLED": False, "JARVIS_CALENDAR_ENABLED": False,
        "JARVIS_MESSAGING_ENABLED": False, "JARVIS_PROACTIVE_ENABLED": False, "JARVIS_TIMEZONE": "Asia/Kolkata",
    }
    base.update(overrides)
    return Settings(_env_file=None, **base)


def test_briefing_settings_have_safe_defaults_and_bounds():
    s = Settings(_env_file=None, DATABASE_URL=DB)
    assert (s.JARVIS_BRIEFING_ENABLED, s.JARVIS_BRIEFING_MAX_ITEMS, s.JARVIS_BRIEFING_LOOKAHEAD_DAYS, s.JARVIS_BRIEFING_EMAIL_LIMIT, s.JARVIS_BRIEFING_USE_LLM) == (True, 10, 7, 5, False)
    for bad in ({"JARVIS_BRIEFING_MAX_ITEMS": 0}, {"JARVIS_BRIEFING_MAX_ITEMS": 51}, {"JARVIS_BRIEFING_LOOKAHEAD_DAYS": 0}, {"JARVIS_BRIEFING_LOOKAHEAD_DAYS": 31},
                {"JARVIS_BRIEFING_EMAIL_LIMIT": -1}, {"JARVIS_BRIEFING_EMAIL_LIMIT": 21}):
        with pytest.raises(ValidationError):
            Settings(_env_file=None, DATABASE_URL=DB, **bad)
    assert Settings(_env_file=None, DATABASE_URL=DB, JARVIS_BRIEFING_EMAIL_LIMIT=0).JARVIS_BRIEFING_EMAIL_LIMIT == 0  # 0 = leave email out


def test_disabled_briefings_build_nothing():
    cfg = settings(JARVIS_BRIEFING_ENABLED=False)
    assert build_briefing_service(cfg, ScriptedLLM(), IST) is None and build_briefing_tools_for(cfg, ScriptedLLM(), IST) == []
    engine = _build_conversation(cfg, None)
    assert engine._permissions.request_permission("briefing_generate", "execute").status is PermissionStatus.DENIED


def test_the_service_reuses_the_existing_services_it_is_given():
    cfg = settings()
    system = build_task_system(cfg)
    service = build_briefing_service(cfg, ScriptedLLM(), IST, tasks=system.tasks, reminders=system.reminders)
    assert isinstance(service, BriefingService)
    collector = service._collector
    assert collector._tasks is system.tasks and collector._reminders is system.reminders  # the same instances: no second task/reminder system
    assert collector._events is None and collector._calendar is None and collector._gmail is None and collector._messaging is None  # nothing invented for what is not set up
    assert (collector._max, collector._lookahead, collector._email_limit) == (10, 7, 5) and service._use_llm is False


def test_configuration_reaches_the_collector_and_the_llm_switch():
    service = build_briefing_service(settings(JARVIS_BRIEFING_MAX_ITEMS=4, JARVIS_BRIEFING_LOOKAHEAD_DAYS=3, JARVIS_BRIEFING_EMAIL_LIMIT=2, JARVIS_BRIEFING_USE_LLM=True), ScriptedLLM(), IST)
    assert (service._collector._max, service._collector._lookahead, service._collector._email_limit, service._use_llm) == (4, 3, 2, True)


def test_enabled_conversation_registers_only_the_two_read_only_tools_and_denies_everything_else():
    engine = _build_conversation(settings(), None)
    perms = engine._permissions
    for name in BRIEFING_ACTION_NAMES:
        assert perms.request_permission(name, "execute").status is PermissionStatus.APPROVED  # LOW risk, registered
    for name in ("briefing_send", "briefing_create_task", "briefing_schedule", "briefing_reschedule", "send_email"):
        assert perms.request_permission(name, "execute").status is PermissionStatus.DENIED
    assert BRIEFING_ACTION_NAMES <= set(engine._actions._tools)


def test_briefings_work_alongside_every_other_integration(tmp_path):
    cfg = settings(JARVIS_GMAIL_ENABLED=True, JARVIS_CALENDAR_ENABLED=True, JARVIS_MESSAGING_ENABLED=True, JARVIS_PROACTIVE_ENABLED=True,
                   JARVIS_GMAIL_TOKEN_PATH=str(tmp_path / "g.json"), JARVIS_GMAIL_CREDENTIALS_PATH=str(tmp_path / "gc.json"),
                   JARVIS_CALENDAR_TOKEN_PATH=str(tmp_path / "c.json"), JARVIS_CALENDAR_CREDENTIALS_PATH=str(tmp_path / "cc.json"),
                   JARVIS_MESSAGING_TELEGRAM_TOKEN_PATH=str(tmp_path / "t"))
    tools = set(_build_conversation(cfg, build_task_system(cfg))._actions._tools)
    from integrations.calendar.intents import CALENDAR_ACTION_NAMES
    from integrations.gmail.intents import GMAIL_ACTION_NAMES
    from integrations.messaging.intents import MESSAGE_ACTION_NAMES
    from agent.proactive.intents import PROACTIVE_ACTION_NAMES

    assert BRIEFING_ACTION_NAMES <= tools and GMAIL_ACTION_NAMES <= tools and CALENDAR_ACTION_NAMES <= tools and MESSAGE_ACTION_NAMES <= tools and PROACTIVE_ACTION_NAMES <= tools
    assert not BRIEFING_ACTION_NAMES & (GMAIL_ACTION_NAMES | CALENDAR_ACTION_NAMES | MESSAGE_ACTION_NAMES | PROACTIVE_ACTION_NAMES)


def test_a_briefing_with_no_integrations_set_up_still_answers_honestly(tmp_path):
    """Gmail/Calendar/messaging enabled but never set up: they are left out silently, and the tasks-only briefing is not degraded."""
    cfg = settings(JARVIS_GMAIL_ENABLED=True, JARVIS_CALENDAR_ENABLED=True, JARVIS_MESSAGING_ENABLED=True,
                   JARVIS_GMAIL_TOKEN_PATH=str(tmp_path / "g.json"), JARVIS_GMAIL_CREDENTIALS_PATH=str(tmp_path / "gc.json"),
                   JARVIS_CALENDAR_TOKEN_PATH=str(tmp_path / "c.json"), JARVIS_CALENDAR_CREDENTIALS_PATH=str(tmp_path / "cc.json"),
                   JARVIS_MESSAGING_TELEGRAM_TOKEN_PATH=str(tmp_path / "t"))
    system = build_task_system(cfg)
    from voice.bootstrap import build_calendar_service, build_gmail_service, build_messaging_service

    service = build_briefing_service(cfg, ScriptedLLM(), IST, tasks=system.tasks, gmail=build_gmail_service(cfg, ScriptedLLM()), calendar=build_calendar_service(cfg, IST),
                                     messaging=build_messaging_service(cfg, ScriptedLLM()))
    from agent.briefing.models import BriefingWindow, SourceName, SourceState

    # (the task database itself is not reachable in this unit test, so tasks are reported unavailable; the three integrations are simply not set up)
    ctx = service._collector.collect(BriefingWindow.TODAY)
    assert {n: ctx.state_of(n) for n in (SourceName.CALENDAR, SourceName.GMAIL, SourceName.MESSAGING)} == {
        SourceName.CALENDAR: SourceState.NOT_CONFIGURED, SourceName.GMAIL: SourceState.NOT_CONFIGURED, SourceName.MESSAGING: SourceState.NOT_CONFIGURED}
    assert list(tmp_path.iterdir()) == []  # no file, token or request was created


def test_env_example_documents_the_briefing_settings_without_secrets():
    text = (ROOT / ".env.example").read_text(encoding="utf-8")
    block = text.split("# --- Daily briefing")[1].split("# --- Proactive")[0]
    for key in ("JARVIS_BRIEFING_ENABLED", "JARVIS_BRIEFING_MAX_ITEMS", "JARVIS_BRIEFING_LOOKAHEAD_DAYS", "JARVIS_BRIEFING_EMAIL_LIMIT", "JARVIS_BRIEFING_USE_LLM"):
        assert key in block
    assert "TOKEN" not in block.upper() and "SECRET" not in block.upper() and "PASSWORD" not in block.upper()


def test_no_migration_table_scheduler_or_notifier_was_added():
    versions = sorted(p.name for p in (ROOT / "database" / "migrations" / "versions").glob("*.py"))
    assert versions[-1] == "0006_create_proactive_notifications.py" and not any("brief" in v for v in versions)  # the latest migration is still Phase 14's
    text = (ROOT / "voice" / "bootstrap.py").read_text(encoding="utf-8")
    assert "BriefingScheduler" not in text and "briefing_scheduler" not in text.lower()
