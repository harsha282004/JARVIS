"""Shared helpers for briefing tests: the REAL task/reminder/event services over isolated SQLite, plus doubles at the integration
boundary (Google Calendar client, Gmail mailbox, messaging provider), wired to a real BriefingService."""

from datetime import datetime, timedelta, timezone

from agent.briefing.builder import Builder
from agent.briefing.collector import ProductivityCollector
from agent.briefing.service import BriefingService
from agent.events.models import EventType
from agent.events.repository import EventRepository
from agent.events.service import EventService
from agent.tasks.models import TaskPriority
from agent.tasks.repository import TaskRepository
from agent.tasks.service import ReminderService, TaskService
from integrations.calendar.service import CalendarService
from integrations.gmail.service import GmailService
from integrations.messaging.base import ProviderRegistry
from integrations.messaging.service import MessagingService
from tests.calendar_helpers import PRIMARY, FakeCalendarClient
from tests.gmail_helpers import ScriptedLLM
from tests.messaging_helpers import FakeProvider
from tests.proactive_helpers import UnreadFriendlyGmail
from tests.task_helpers import IST, Clock, ist  # noqa: F401  (re-exported)

NOW = datetime(2030, 3, 4, 3, 30, tzinfo=timezone.utc)  # Monday 09:00 in Asia/Kolkata: a morning


def at(day: int, hour: int, minute: int = 0, month: int = 3) -> datetime:
    """A local (IST) time in March 2030."""
    return ist(2030, month, day, hour, minute)


class Bench:
    def __init__(self, session_factory, *, calendar_events=None, mailbox=None, chat_messages=None, llm_replies=(), use_llm=False, max_items=10,
                 email_limit=5, tasks=True, reminders=True, events=True, lookahead_days=7):
        self.clock = Clock(NOW)
        repo = TaskRepository(session_factory)
        self.tasks = TaskService(repo, zone=IST, clock=self.clock)
        self.reminders = ReminderService(repo, zone=IST, clock=self.clock)
        self.events = EventService(EventRepository(session_factory), zone=IST, clock=self.clock, tasks=self.tasks)
        self.calendar_client = FakeCalendarClient((PRIMARY,), calendar_events or []) if calendar_events is not None else None
        self.calendar = CalendarService(self.calendar_client, zone=IST, clock=self.clock) if self.calendar_client else None
        self.mailbox = UnreadFriendlyGmail(mailbox) if mailbox is not None else None
        self.llm = ScriptedLLM(*llm_replies)
        self.gmail = GmailService(self.mailbox, self.llm) if self.mailbox is not None else None
        self.provider = FakeProvider(chat_messages) if chat_messages is not None else None
        registry = ProviderRegistry()
        if self.provider is not None:
            registry.register(self.provider)
        self.messaging = MessagingService(registry, self.llm) if chat_messages is not None else None
        self.collector = ProductivityCollector(
            zone=IST, clock=self.clock, tasks=self.tasks if tasks else None, reminders=self.reminders if reminders else None,
            events=self.events if events else None, calendar=self.calendar, gmail=self.gmail, messaging=self.messaging, max_items=max_items,
            lookahead_days=lookahead_days, email_limit=email_limit,
        )
        self.service = BriefingService(self.collector, Builder(IST, max_items), llm=self.llm, use_llm=use_llm, clock=self.clock)

    def brief(self, *args, **kw):
        return self.service.brief(*args, **kw)

    def task(self, title, due=None, priority=None):
        return self.tasks.create_task(title, due_at=due, priority=priority)

    def event(self, title, event_type=EventType.EVENT, **kw):
        return self.events.create_event(title, event_type, **kw).event


__all__ = ["Bench", "at", "NOW", "IST", "ist", "TaskPriority", "EventType", "timedelta"]
