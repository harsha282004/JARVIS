"""Shared helpers for proactive-intelligence tests: an environment wiring real Phase 9-12 services (over isolated SQLite
or in-memory doubles) to a real ProactiveEngine, plus recording notifiers."""

from datetime import datetime, timedelta, timezone

from agent.events.models import EventType
from agent.proactive.engine import ProactiveEngine
from agent.proactive.models import Channel, ProactiveSignal, SignalType, SourceKind, Urgency, make_key
from agent.proactive.policy import NotificationPolicy, PolicyConfig
from agent.proactive.repository import NotificationRepository
from agent.proactive.sources import CalendarSignalSource, EventSignalSource, GmailSignalSource, TaskSignalSource
from agent.memory.models import Confidence
from agent.tasks.models import TaskPriority
from agent.tasks.notifications import (
    AnnouncementQueue,
    DesktopNotifier,
    NotificationError,
    NotificationService,
    VoiceNotifier,
)
from integrations.calendar.service import CalendarService
from integrations.gmail.service import GmailService
from tests.calendar_helpers import PRIMARY, FakeCalendarClient
from tests.event_helpers import make_events
from tests.gmail_helpers import FakeGmailClient, ScriptedLLM, raw_message
from tests.task_helpers import IST, Clock, ist  # noqa: F401  (re-exported)

NOW = datetime(2030, 3, 4, 9, 0, tzinfo=timezone.utc)  # Monday 14:30 in Asia/Kolkata (outside quiet hours)
QUIET_NOW = datetime(2030, 3, 4, 18, 0, tzinfo=timezone.utc)  # Monday 23:30 in Asia/Kolkata (inside 23:00-07:00)


class RecordingNotifier(NotificationService):
    def __init__(self, fail: bool = False):
        self.messages: list[str] = []
        self.metadata: list[dict] = []
        self.fail = fail

    def notify(self, message, metadata=None):
        if self.fail:
            raise NotificationError("channel down")
        self.messages.append(message)
        self.metadata.append(dict(metadata or {}))


class UnreadFriendlyGmail(FakeGmailClient):
    """The in-memory mailbox, but tolerant of the query operators the proactive source uses (in:inbox, newer_than:)."""

    def search(self, query, max_results, page_token=None):
        self.queries = getattr(self, "queries", []) + [(query, max_results)]
        simple = " ".join(t for t in query.split() if t == "is:unread")
        return super().search(simple, max_results, page_token)


def sig(signal_type=SignalType.TASK_DUE, *, source=SourceKind.TASK, source_id="t1", tier="60", title="Submit report", priority=TaskPriority.MEDIUM,
        urgency=Urgency.SOON, confidence=Confidence.HIGH, now=NOW, relevant_in_minutes=45, expires=True, metadata=None) -> ProactiveSignal:
    relevant = now + timedelta(minutes=relevant_in_minutes)
    return ProactiveSignal(
        signal_id=make_key(signal_type, source, source_id, tier, relevant.isoformat()), signal_type=signal_type, source_type=source, source_id=source_id,
        source_reference="your task list", title=title, priority=priority, urgency=urgency, confidence=confidence, detected_at=now,
        relevant_at=relevant, expires_at=relevant if expires else None, tier=tier, metadata=metadata or {},
    )


def default_config(**overrides) -> PolicyConfig:
    return PolicyConfig(**{"enabled": True, "quiet_hours_enabled": True, "cooldown_minutes": 60, "lookahead_minutes": 1440, "max_per_hour": 6, **overrides})


class Env:
    """Real TaskService/EventService (SQLite), CalendarService over a client double, GmailService over a mailbox double."""

    def __init__(self, session_factory, *, config=None, calendar_events=(), mailbox=(), channels=("desktop", "voice"), calendar_active=True,
                 gmail=False, calendar=True, interval=0.0):
        self.clock = Clock(NOW)
        self.events, self.tasks, _ = make_events(session_factory, self.clock)
        self.calendar_client = FakeCalendarClient((PRIMARY,), calendar_events)
        self.calendar = CalendarService(self.calendar_client, zone=IST, clock=self.clock)
        self.mailbox = UnreadFriendlyGmail(mailbox)
        self.gmail = GmailService(self.mailbox, ScriptedLLM())
        self.repo = NotificationRepository(session_factory)
        self.desktop, self.voice_sink = RecordingNotifier(), AnnouncementQueue()
        self.voice_sink.set_accepting(True)
        self.notifiers: dict[Channel, NotificationService] = {}
        if "desktop" in channels:
            self.notifiers[Channel.DESKTOP] = self.desktop
        if "voice" in channels:
            self.notifiers[Channel.VOICE] = VoiceNotifier(self.voice_sink)
        self.config = config or default_config()
        self.policy = NotificationPolicy(self.config, IST)
        lookahead = self.config.lookahead_minutes
        self.sources = [TaskSignalSource(self.tasks, lookahead), EventSignalSource(self.events, lookahead, calendar_active=calendar_active and calendar)]
        if calendar:
            self.sources.append(CalendarSignalSource(self.calendar, lookahead, refresh_minutes=10))
        if gmail:
            self.sources.append(GmailSignalSource(self.gmail, refresh_minutes=10))
        self.engine = ProactiveEngine(self.sources, self.policy, self.repo, self.notifiers, IST, clock=self.clock, interval_seconds=interval)

    def run(self, **kw):
        return self.engine.run_once(**kw)

    def spoken(self) -> list[str]:
        out = []
        while (text := self.voice_sink.get_nowait()) is not None:
            out.append(text)
        return out

    def advance(self, **kw):
        self.clock.advance(**kw)


def action_email(mid="m1", sender="John Smith <john@example.com>", subject="Internship update"):
    return raw_message(id=mid, thread=f"t-{mid}", subject=subject, sender=sender, body="Could you please confirm your start date by Friday?",
                       labels=("INBOX", "UNREAD"))


def important_email(mid="m2", subject="Offer letter"):
    return raw_message(id=mid, thread=f"t-{mid}", subject=subject, sender="HR <hr@example.com>", body="Your offer letter is ready.",
                       labels=("INBOX", "UNREAD", "IMPORTANT"))


def plain_email(mid="m3"):
    return raw_message(id=mid, thread=f"t-{mid}", subject="Lunch", sender="Sam <sam@example.com>", body="See you at noon.", labels=("INBOX", "UNREAD"))


def newsletter(mid="m4"):
    return raw_message(id=mid, thread=f"t-{mid}", subject="Big sale", sender="News <newsletter@news.example.com>", body="50% off. Unsubscribe at any time.",
                       labels=("INBOX", "UNREAD", "CATEGORY_PROMOTIONS"), extra_headers=[{"name": "List-Unsubscribe", "value": "<mailto:u@x.com>"}])


__all__ = ["Env", "RecordingNotifier", "sig", "default_config", "action_email", "important_email", "plain_email", "newsletter", "NOW", "QUIET_NOW", "EventType"]
