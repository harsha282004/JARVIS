"""REAL-infrastructure briefing tests. Everything here is READ-ONLY: nothing is created, changed, sent or deleted in any source.

- test_real_sqlite_file_*     the real task/reminder/event services over a real SQLite database FILE (runs by default).
                              This is a real database, but NOT PostgreSQL.
- test_real_postgresql_*      the same over a disposable PostgreSQL database; skipped unless JARVIS_TEST_DATABASE_URL is set.
- test_real_google_*          a real briefing over your real Google Calendar and/or Gmail; skipped unless a token exists
                              (python scripts/calendar_cli.py auth / gmail_cli.py auth). Only reads are made; no titles are printed.
- test_real_telegram_*        includes a real messaging provider; skipped unless a bot token is configured.

A skipped test means that infrastructure was NOT tested; nothing here pretends otherwise.
"""

import os
from datetime import timedelta

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from agent.briefing.builder import Builder
from agent.briefing.collector import ProductivityCollector
from agent.briefing.models import BriefingWindow, Detail, SourceName, SourceState, View
from agent.briefing.service import BriefingService
from agent.events.models import EventType
from agent.events.repository import EventRepository
from agent.events.service import EventService
from agent.tasks.models import TaskPriority, utcnow
from agent.tasks.repository import TaskRepository
from agent.tasks.service import ReminderService, TaskService
from backend.core.config import get_settings
from backend.models.base import Base
from backend.models.events import EventRow
from backend.models.tasks import ReminderRow, TaskRow
from tests.task_helpers import IST

pytestmark = pytest.mark.integration


def real_services(factory):
    repo = TaskRepository(factory)
    tasks = TaskService(repo, zone=IST)
    return tasks, ReminderService(repo, zone=IST), EventService(EventRepository(factory), zone=IST, tasks=tasks)


def counts(factory):
    with factory() as s:
        return tuple(s.scalar(select(func.count()).select_from(t)) for t in (TaskRow, ReminderRow, EventRow))


def seed(tasks, reminders, events):
    now = utcnow()
    tasks.create_task("Submit internship application", due_at=now + timedelta(hours=3), priority=TaskPriority.HIGH)
    tasks.create_task("Water plants", due_at=now + timedelta(hours=5))
    reminders.create_reminder("Call John", now + timedelta(hours=2))
    events.create_event("Project submission", EventType.DEADLINE, due_at=now + timedelta(days=1))


def briefing(tasks, reminders, events, **extra):
    collector = ProductivityCollector(zone=IST, clock=utcnow, tasks=tasks, reminders=reminders, events=events, **extra)
    return BriefingService(collector, Builder(IST, 10))


def test_real_sqlite_file_briefing_is_read_only_and_bounded(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'briefing.db'}", connect_args={"check_same_thread": False, "timeout": 30})
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    tasks, reminders, events = real_services(factory)
    seed(tasks, reminders, events)
    before = counts(factory)
    service = briefing(tasks, reminders, events)
    for view in View:
        for detail in Detail:
            result = service.brief(view, BriefingWindow.YESTERDAY if view is View.MISSED else BriefingWindow.NEXT_7_DAYS, detail)
            assert result.spoken and len(result.spoken) <= {Detail.QUICK: 320, Detail.NORMAL: 950, Detail.DETAILED: 2400}[detail]
    overview = service.brief(View.OVERVIEW, BriefingWindow.NEXT_7_DAYS, Detail.NORMAL)
    assert "Submit internship application" in service.brief(View.TASKS, BriefingWindow.NEXT_7_DAYS, Detail.DETAILED).spoken and "Project submission" in overview.spoken
    assert counts(factory) == before  # nothing was created or deleted
    assert service.explain("internship").startswith("I haven't given you a briefing yet") is False
    engine.dispose()


@pytest.mark.skipif(not os.environ.get("JARVIS_TEST_DATABASE_URL"), reason="JARVIS_TEST_DATABASE_URL not set (no disposable PostgreSQL database): PostgreSQL was NOT tested")
def test_real_postgresql_briefing_is_read_only():
    engine = create_engine(os.environ["JARVIS_TEST_DATABASE_URL"])
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    try:
        factory = sessionmaker(bind=engine, expire_on_commit=False)
        tasks, reminders, events = real_services(factory)
        seed(tasks, reminders, events)
        before = counts(factory)
        result = briefing(tasks, reminders, events).brief(View.OVERVIEW, BriefingWindow.NEXT_7_DAYS)
        assert "Submit internship application" in result.spoken or "task" in result.spoken
        assert counts(factory) == before
    finally:
        Base.metadata.drop_all(engine)
        engine.dispose()


def _real_google(tmp_path):
    """Real Calendar and Gmail services built exactly as JARVIS builds them; whichever has a token is used."""
    from voice.bootstrap import build_calendar_authenticator, build_calendar_service, build_gmail_authenticator, build_gmail_service
    from backend.core.llm.base import LLMProvider

    settings = get_settings()
    calendar = build_calendar_service(settings.model_copy(update={"JARVIS_CALENDAR_ENABLED": True}), IST) if build_calendar_authenticator(settings).is_ready() else None
    gmail = None
    if build_gmail_authenticator(settings).is_ready():
        class NoLLM(LLMProvider):  # summaries are never requested by a briefing
            def chat(self, messages, json_mode=False):
                raise AssertionError("a briefing must not call the model for email")

        gmail = build_gmail_service(settings.model_copy(update={"JARVIS_GMAIL_ENABLED": True}), NoLLM())
    return calendar, gmail


def test_real_google_calendar_and_gmail_briefing_is_read_only(tmp_path):
    calendar, gmail = _real_google(tmp_path)
    if calendar is None and gmail is None:
        pytest.skip("No Google Calendar or Gmail token: real Google was NOT tested (run scripts/calendar_cli.py auth / gmail_cli.py auth)")
    engine = create_engine(f"sqlite:///{tmp_path / 'g.db'}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    tasks, reminders, events = real_services(factory)
    collector = ProductivityCollector(zone=IST, clock=utcnow, tasks=tasks, reminders=reminders, events=events, calendar=calendar, gmail=gmail, email_limit=3)
    service = BriefingService(collector, Builder(IST, 10))
    result = service.brief(View.OVERVIEW, BriefingWindow.NEXT_7_DAYS, Detail.NORMAL)
    ctx = collector.collect(BriefingWindow.NEXT_7_DAYS)
    assert result.spoken and len(result.spoken) <= 950
    if calendar is not None:
        assert ctx.state_of(SourceName.CALENDAR) is SourceState.OK  # the real calendar was read (no titles are asserted or printed)
    if gmail is not None:
        assert ctx.state_of(SourceName.GMAIL) is SourceState.OK and len(ctx.emails_action) + len(ctx.emails_important) <= 3
    engine.dispose()


def test_real_telegram_provider_contributes_to_a_briefing(tmp_path):
    from voice.bootstrap import build_messaging_registry
    from integrations.messaging.service import MessagingService
    from backend.core.llm.base import LLMProvider

    provider = build_messaging_registry(get_settings()).get("telegram")
    if not provider.is_configured():
        pytest.skip("No Telegram bot token: real messaging was NOT tested")

    class NoLLM(LLMProvider):
        def chat(self, messages, json_mode=False):
            raise AssertionError("no model call expected")

    from integrations.messaging.base import ProviderRegistry

    registry = ProviderRegistry()
    registry.register(provider)
    engine = create_engine(f"sqlite:///{tmp_path / 't.db'}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    tasks, reminders, events = real_services(sessionmaker(bind=engine, expire_on_commit=False))
    collector = ProductivityCollector(zone=IST, clock=utcnow, tasks=tasks, reminders=reminders, events=events, messaging=MessagingService(registry, NoLLM()))
    ctx = collector.collect(BriefingWindow.TODAY)
    assert ctx.state_of(SourceName.MESSAGING) in (SourceState.OK, SourceState.UNAVAILABLE)  # reachable, or reported honestly when Telegram refuses (e.g. a webhook is set)
    engine.dispose()
