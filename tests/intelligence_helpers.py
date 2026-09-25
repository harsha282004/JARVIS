"""Test harness for the intelligence layer: REAL services (tasks, memory, calendar service, Gmail service) over in-memory SQLite and
in-memory fake API clients, plus safe synthetic data. No real credentials, mail or calendar are ever used.

Synthetic scenario (the fixed clock is Thursday 2026-09-24 09:00 IST):
    email     "Your JARVIS project review is scheduled for tomorrow at 11 AM. Please submit the documentation before the review."
    calendar  "JARVIS Project Review" Friday 11:00
    task      "Finish JARVIS documentation" (pending, due Friday 09:00) and "Prepare project review slides"
"""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from agent.intelligence.confirmation import ConfirmationEngine
from agent.intelligence.context_engine import PersonalContextEngine
from agent.intelligence.dependencies import DependencyStore
from agent.intelligence.plan_executor import PlanExecutor
from agent.intelligence.planner import DayPlanner
from agent.intelligence.proactive import IntelligenceNotifier
from agent.intelligence.router import IntelligenceRouter
from agent.intelligence.runner import IntelligenceRunner
from agent.intelligence.service import IntelligenceService
from agent.intelligence.snapshot import SnapshotCollector
from agent.intelligence.timeline import ActivityTimeline
from agent.memory.models import Confidence, MemoryBasis, MemoryCandidate, MemorySource, MemoryType
from agent.memory.policy import MemoryPolicy
from agent.memory.repository import MemoryRepository
from agent.memory.service import MemoryService
from agent.tasks.models import TaskPriority
from agent.tasks.repository import TaskRepository
from agent.tasks.service import ReminderService, TaskService
from backend.core.action_audit import ActionAuditLog
from backend.core.events import EventBus
from backend.core.notifications import NotificationCenter
from backend.core.preferences import PreferenceStore
from backend.core.privacy import PrivacyController
from backend.models.base import Base
from integrations.calendar.service import CalendarService
from integrations.gmail.service import GmailService
from tests.calendar_helpers import PRIMARY, FakeCalendarClient, cal_event
from tests.gmail_helpers import FakeGmailClient, raw_message

import backend.models.memory  # noqa: F401
import backend.models.tasks  # noqa: F401

IST = ZoneInfo("Asia/Kolkata")
NOW = datetime(2026, 9, 24, 9, 0, tzinfo=IST)  # a Thursday


def ist(day: int, hour: int = 0, minute: int = 0, month: int = 9, year: int = 2026) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=IST)


class Clock:
    def __init__(self, now: datetime = NOW):
        self.now = now.astimezone(timezone.utc)

    def __call__(self):
        return self.now

    def set(self, moment: datetime) -> None:
        self.now = moment.astimezone(timezone.utc)

    def advance(self, **kw) -> None:
        self.now += timedelta(**kw)


class NoLLM:
    """Fails loudly if the intelligence layer ever calls a language model."""

    calls = 0

    def chat(self, messages, json_mode=False):
        NoLLM.calls += 1
        raise AssertionError("the intelligence layer must not call the LLM")


SYNTH_EMAIL_BODY = "Your JARVIS project review is scheduled for tomorrow at 11 AM. Please submit the documentation before the review."


@dataclass
class Harness:
    service: IntelligenceService
    router: IntelligenceRouter
    tasks: TaskService
    reminders: ReminderService
    memory: MemoryService | None
    calendar_client: FakeCalendarClient | None
    gmail_client: FakeGmailClient | None
    calendar: CalendarService | None
    clock: Clock
    bus: EventBus
    audit: ActionAuditLog
    center: NotificationCenter
    prefs: PreferenceStore
    privacy: PrivacyController
    confirmations: ConfirmationEngine
    delivered: list
    runner: IntelligenceRunner
    state_dir: Path

    def say(self, text: str, session: str = "s1"):
        reply = self.router.handle(text, session)
        return None if reply is None else reply.text


def email_raw(id="m1", subject="JARVIS project review", body=SYNTH_EMAIL_BODY, sender="Prof Rao <rao@example.edu>", hours_ago=2, labels=("INBOX", "UNREAD")):
    when = NOW - timedelta(hours=hours_ago)
    return raw_message(id=id, subject=subject, sender=sender, body=body, date_ms=int(when.timestamp() * 1000), labels=labels)


def build_harness(
    tmp_path: Path,
    *,
    emails=(),
    calendar_events=(),
    tasks=(),
    memories=(),
    with_calendar=True,
    with_gmail=True,
    with_memory=True,
    offline=False,
    auto_create=False,
    calendar_client: FakeCalendarClient | None = None,
) -> Harness:
    engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    clock = Clock()
    repo = TaskRepository(factory)
    task_service = TaskService(repo, zone=IST, clock=clock)
    reminder_service = ReminderService(repo, zone=IST, clock=clock)
    for title, due, priority, notes in tasks:
        task_service.create_task(title, due_at=due, priority=priority, notes=notes)

    memory = None
    if with_memory:
        memory = MemoryService(MemoryRepository(factory), policy=MemoryPolicy(), clock=clock)
        for content, mtype in memories:
            memory.store(MemoryCandidate(type=mtype, content=content, source=MemorySource.EXPLICIT_USER_STATEMENT, basis=MemoryBasis.EXPLICIT, confidence=Confidence.HIGH))

    cal_client = calendar_client or FakeCalendarClient(events=list(calendar_events))
    calendar = CalendarService(cal_client, zone=IST, clock=clock) if with_calendar else None
    gmail_client = FakeGmailClient(list(emails))
    gmail = GmailService(gmail_client, NoLLM(), is_ready=lambda: True) if with_gmail else None

    state = tmp_path / "state"
    state.mkdir(exist_ok=True)
    bus = EventBus()
    prefs = PreferenceStore(state / "preferences.json")
    privacy = PrivacyController(state / "privacy.json", bus)
    delivered: list = []
    center = NotificationCenter(preferences=prefs, zone=IST, deliver=lambda ch, n: delivered.append((ch, n.title)), privacy=privacy, bus=bus,
                                state_file=state / "notifications.json", clock=clock)
    audit = ActionAuditLog(state / "audit.jsonl")
    confirmations = ConfirmationEngine(audit, clock=clock)
    deps = DependencyStore(state / "dependencies.json")
    planner = DayPlanner(IST, datetime.strptime("09:00", "%H:%M").time(), datetime.strptime("18:00", "%H:%M").time(), 60, 10)
    executor = PlanExecutor(calendar, IST, confirmations, clock) if calendar is not None else None
    collector = SnapshotCollector(zone=IST, clock=clock, tasks=task_service, reminders=reminder_service, calendar=calendar, gmail=gmail, memory=memory,
                                  offline=lambda: offline)
    engine_ = PersonalContextEngine(IST, clock, deps)
    service = IntelligenceService(
        zone=IST, collector=collector, engine=engine_, planner=planner, confirmations=confirmations, prefs=prefs, deps=deps,
        timeline=ActivityTimeline(state / "timeline.jsonl"), audit=audit, bus=bus, executor=executor, tasks=task_service,
        state_file=state / "processed.json", clock=clock, auto_create_default=auto_create, notifications=center,
    )
    runner = IntelligenceRunner(service, IntelligenceNotifier(center, service.explanations), center=center, bus=bus, privacy=privacy)
    return Harness(service, IntelligenceRouter(service), task_service, reminder_service, memory, cal_client if with_calendar else None, gmail_client, calendar,
                   clock, bus, audit, center, prefs, privacy, confirmations, delivered, runner, state)


def scenario_harness(tmp_path: Path, **kw) -> Harness:
    """The synthetic project-review scenario used across the tests."""
    events = [cal_event("rev1", "JARVIS Project Review", ist(25, 11), ist(25, 12))]
    tasks = [
        ("Finish JARVIS documentation", ist(25, 9), TaskPriority.HIGH, None),
        ("Prepare project review slides", None, TaskPriority.MEDIUM, None),
    ]
    memories = [("User is currently working on the JARVIS project.", MemoryType.CONTEXT)]
    return build_harness(tmp_path, emails=[email_raw()], calendar_events=events, tasks=tasks, memories=memories, **kw)
