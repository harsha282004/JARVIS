"""Shared helpers for event/deadline tests."""

from datetime import datetime, timezone

from agent.events.models import EventSource, SourceType
from agent.events.repository import EventRepository
from agent.events.service import EventService
from agent.tasks.repository import TaskRepository
from agent.tasks.service import TaskService
from tests.task_helpers import IST, Clock, ist  # noqa: F401  (re-exported)

NOW = datetime(2030, 3, 4, 9, 0, tzinfo=timezone.utc)  # Monday 14:30 in Asia/Kolkata


def make_events(session_factory, clock=None, **kw):
    clock = clock or Clock()
    tasks = TaskService(TaskRepository(session_factory), zone=IST, clock=clock)
    events = EventService(EventRepository(session_factory), zone=IST, clock=clock, tasks=tasks, **kw)
    return events, tasks, clock


def explicit_source(reference="you told me"):
    return EventSource(source_type=SourceType.USER_EXPLICIT, reference=reference)


def gmail_source(message_id="m1", reference="email from John, dated March 1, 2030"):
    return EventSource(source_type=SourceType.GMAIL, source_id=message_id, reference=reference)
