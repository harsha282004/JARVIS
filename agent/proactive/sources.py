"""Signal sources: read-only adapters over the existing Phase 9-12 services. They OBSERVE and produce signals; they never
change anything (no task, event, calendar or email is touched) and never call an LLM.

Every source is bounded. Local sources (tasks, Phase 11 events) are cheap database reads done every cycle. External
sources (Google Calendar, Gmail) are throttled: they refresh their small cache at most every `refresh_minutes` and then
compute time-based signals from the cache, so the engine never polls Google continuously.

Only situations the existing systems can genuinely report are produced. Not produced, on purpose: reminders (Phase 9's
scheduler already delivers them; a second path would duplicate them), messaging (Phase 13 has no unread/new-message
signal and reading it in the background is forbidden there) and memory (no time relationship to signal).
"""

from abc import ABC, abstractmethod
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from agent.events.models import DUE_TYPES, EventStatus, EventType, SourceType
from agent.events.service import EventService
from agent.events.temporal import EventScope
from agent.memory.models import Confidence
from agent.proactive.messages import safe_text
from agent.proactive.models import ProactiveSignal, SignalType, SourceKind, Urgency, make_key
from agent.proactive.policy import urgency_for
from agent.tasks.models import OPEN_STATUSES, TaskPriority
from agent.tasks.service import TaskService
from backend.core.logging import get_logger
from integrations.calendar.models import CalendarEvent
from integrations.calendar.service import CalendarService
from integrations.gmail.models import EmailCategory
from integrations.gmail.service import GmailService

logger = get_logger(__name__)

TASK_LIMIT = 100
CALENDAR_LIMIT = 50
EMAIL_SEARCH_LIMIT = 10
MAX_EMAIL_SIGNALS = 3
EMAIL_QUERY = "is:unread in:inbox newer_than:2d"
CALENDAR_TIERS = (60, 15)  # minutes before an event starts
TASK_TIER_SOON = 60

_TYPE_PRIORITY = {
    EventType.INTERVIEW: TaskPriority.HIGH, EventType.EXAM: TaskPriority.HIGH, EventType.DEADLINE: TaskPriority.HIGH,
    EventType.ASSIGNMENT: TaskPriority.HIGH, EventType.APPLICATION: TaskPriority.HIGH,
    EventType.MEETING: TaskPriority.MEDIUM, EventType.APPOINTMENT: TaskPriority.MEDIUM,
}


def current_tier(minutes: float, tiers: tuple[int, ...]) -> int | None:
    """The tightest tier already entered (smallest tier >= minutes), or None if the thing is further away than every tier.
    A late start (JARVIS was off) therefore produces ONE signal for the tier that applies now, not one per skipped tier."""
    entered = [t for t in tiers if minutes <= t]
    return min(entered) if entered else None


class SignalSource(ABC):
    kind: SourceKind

    def is_available(self) -> bool:
        """False when the underlying integration is not set up (skipped quietly, never an error)."""
        return True

    @abstractmethod
    def collect(self, now: datetime) -> list[ProactiveSignal]:
        raise NotImplementedError


class TaskSignalSource(SignalSource):
    kind = SourceKind.TASK

    def __init__(self, tasks: TaskService, lookahead_minutes: int):
        self._tasks = tasks
        self._lookahead = lookahead_minutes

    def collect(self, now: datetime) -> list[ProactiveSignal]:
        horizon = now + timedelta(minutes=self._lookahead)
        tiers = tuple(sorted({self._lookahead, TASK_TIER_SOON}))
        signals: list[ProactiveSignal] = []
        for task in self._tasks.list_tasks(statuses=set(OPEN_STATUSES), due_before=horizon, limit=TASK_LIMIT, now=now):
            due = task.due_at
            if due is None or task.status not in OPEN_STATUSES:
                continue
            minutes = (due - now).total_seconds() / 60
            common = dict(
                source_type=SourceKind.TASK, source_id=task.task_id, source_reference="your task list", title=task.title,
                priority=task.priority, confidence=Confidence.HIGH, detected_at=now, relevant_at=due,
            )
            if minutes < 0:
                if -minutes > max(self._lookahead, 1440):
                    continue  # long overdue: a briefing's job (a later phase), not a fresh interruption
                key = make_key(SignalType.TASK_OVERDUE, SourceKind.TASK, task.task_id, "overdue", due.isoformat())
                signals.append(ProactiveSignal(signal_id=key, signal_type=SignalType.TASK_OVERDUE, urgency=Urgency.IMMEDIATE, tier="overdue", **common))
                continue
            tier = current_tier(minutes, tiers)
            if tier is None:
                continue
            key = make_key(SignalType.TASK_DUE, SourceKind.TASK, task.task_id, str(tier), due.isoformat())
            signals.append(ProactiveSignal(
                signal_id=key, signal_type=SignalType.TASK_DUE, urgency=urgency_for(minutes, self._lookahead), tier=str(tier),
                expires_at=due, **common,
            ))
        return signals


class EventSignalSource(SignalSource):
    """Phase 11 events and deadlines. An event linked to a task is skipped (the task's own signal covers it), and so is a
    Phase 12 mirror of a Google Calendar event when the calendar source is active (it covers it directly)."""

    kind = SourceKind.EVENT

    def __init__(self, events: EventService, lookahead_minutes: int, *, calendar_active: bool = False):
        self._events = events
        self._lookahead = lookahead_minutes
        self._calendar_active = calendar_active

    def collect(self, now: datetime) -> list[ProactiveSignal]:
        signals: list[ProactiveSignal] = []
        for event in self._events.list_scope(EventScope.UPCOMING, limit=100).events:
            signal = self._upcoming(event, now)
            if signal is not None:
                signals.append(signal)
        for event in self._events.list_scope(EventScope.OVERDUE, limit=100).events:
            signal = self._overdue(event, now)
            if signal is not None:
                signals.append(signal)
        return signals

    def _skip(self, event) -> bool:
        if event.task_id is not None or event.status is EventStatus.UNKNOWN:
            return True
        return self._calendar_active and event.source.source_type is SourceType.GOOGLE_CALENDAR

    @staticmethod
    def _common(event, now: datetime) -> dict:
        return dict(
            source_type=SourceKind.EVENT, source_id=event.event_id, source_reference="your events and deadlines", title=event.title,
            priority=event.priority or _TYPE_PRIORITY.get(event.event_type, TaskPriority.LOW), confidence=event.confidence,
            detected_at=now, relevant_at=event.anchor,
            metadata={"label": event.event_type.value, **({"all_day": "1"} if event.all_day else {})},
        )

    def _upcoming(self, event, now: datetime) -> ProactiveSignal | None:
        if self._skip(event) or not event.is_open:
            return None
        minutes = (event.anchor - now).total_seconds() / 60
        if minutes < 0:
            return None  # already started or due: not "approaching"
        tiers = (self._lookahead,) if event.all_day else tuple(sorted({self._lookahead, TASK_TIER_SOON}))
        tier = current_tier(minutes, tiers)
        if tier is None:
            return None
        kind = SignalType.DEADLINE_APPROACHING if (event.is_deadline or event.event_type in DUE_TYPES) else SignalType.EVENT_APPROACHING
        key = make_key(kind, SourceKind.EVENT, event.event_id, str(tier), event.anchor.isoformat())
        return ProactiveSignal(
            signal_id=key, signal_type=kind, urgency=urgency_for(minutes, self._lookahead), tier=str(tier), expires_at=event.anchor,
            **self._common(event, now),
        )

    def _overdue(self, event, now: datetime) -> ProactiveSignal | None:
        if self._skip(event) or event.due_at is None:
            return None
        if (now - event.due_at).total_seconds() / 60 > max(self._lookahead, 1440):
            return None
        key = make_key(SignalType.DEADLINE_OVERDUE, SourceKind.EVENT, event.event_id, "overdue", event.due_at.isoformat())
        return ProactiveSignal(signal_id=key, signal_type=SignalType.DEADLINE_OVERDUE, urgency=Urgency.IMMEDIATE, tier="overdue", **self._common(event, now))


def _calendar_source_id(event: CalendarEvent) -> str:
    text = f"{event.calendar_id}/{event.event_id}"
    return text if len(text) <= 128 else text[-128:]


class CalendarSignalSource(SignalSource):
    """Google Calendar (Phase 12), read-only: events starting within an hour / 15 minutes, and overlapping events."""

    kind = SourceKind.CALENDAR

    def __init__(self, calendar: CalendarService, lookahead_minutes: int, refresh_minutes: float):
        self._calendar = calendar
        self._lookahead = lookahead_minutes
        self._refresh = timedelta(minutes=refresh_minutes)
        self._cache: list[CalendarEvent] = []
        self._loaded_at: datetime | None = None

    def is_available(self) -> bool:
        return self._calendar.is_configured()

    def _events(self, now: datetime) -> list[CalendarEvent]:
        if self._loaded_at is None or now - self._loaded_at >= self._refresh:
            listing = self._calendar.events_between(now, now + timedelta(minutes=self._lookahead), limit=CALENDAR_LIMIT)
            self._cache, self._loaded_at = list(listing.events), now  # only assigned on success: an outage keeps the old cache
        return self._cache

    def collect(self, now: datetime) -> list[ProactiveSignal]:
        events = [e for e in self._events(now) if not e.is_cancelled and not e.declined_by_me]
        signals: list[ProactiveSignal] = []
        for event in events:
            if event.all_day or not event.blocks_time:
                continue  # all-day and "free" entries are not time-critical interruptions
            minutes = (event.start - now).total_seconds() / 60
            tier = current_tier(minutes, CALENDAR_TIERS) if minutes >= 0 else None
            if tier is None:
                continue
            key = make_key(SignalType.EVENT_APPROACHING, SourceKind.CALENDAR, _calendar_source_id(event), str(tier), event.start.isoformat())
            signals.append(ProactiveSignal(
                signal_id=key, signal_type=SignalType.EVENT_APPROACHING, source_type=SourceKind.CALENDAR, source_id=_calendar_source_id(event),
                source_reference="your Google Calendar", title=safe_text(event.summary or "(no title)", 80), priority=TaskPriority.MEDIUM,
                urgency=urgency_for(minutes, self._lookahead), confidence=Confidence.HIGH, detected_at=now, relevant_at=event.start,
                expires_at=event.start, tier=str(tier),
            ))
        timed = [e for e in events if not e.all_day]  # an all-day entry beside a meeting is not worth an interruption
        for conflict in self._calendar.conflicts_among(timed):
            first, second = conflict.first, conflict.second
            if first.start_at is None or second.start_at is None or max(first.start_at, second.start_at) <= now:
                continue  # already under way: nothing left to rearrange
            minutes = (first.start_at - now).total_seconds() / 60
            ids = sorted(str(x.metadata.get("calendar_event_id", x.event_id)) for x in (first, second))
            source_id = "+".join(ids)[:128]
            key = make_key(SignalType.CALENDAR_CONFLICT, SourceKind.CALENDAR, source_id, "conflict", f"{first.start_at.isoformat()}|{second.start_at.isoformat()}")
            signals.append(ProactiveSignal(
                signal_id=key, signal_type=SignalType.CALENDAR_CONFLICT, source_type=SourceKind.CALENDAR, source_id=source_id,
                source_reference="your Google Calendar", title=safe_text(first.title, 80), priority=TaskPriority.MEDIUM,
                urgency=max(urgency_for(minutes, self._lookahead), Urgency.UPCOMING), confidence=Confidence.HIGH, detected_at=now,
                relevant_at=first.start_at, expires_at=max(first.start_at, second.start_at), tier="conflict",
                metadata={"other": safe_text(second.title, 80)},
            ))
        return signals


class GmailSignalSource(SignalSource):
    """Gmail (Phase 10), read-only and opt-in. Only unread inbox mail from the last two days that JARVIS's deterministic
    classifier calls ACTION_REQUIRED or IMPORTANT produces a signal; ordinary mail never does (no "new email" spam).
    Email has no time relationship, so its urgency is fixed at UPCOMING rather than guessed from wording. Only the sender,
    subject and category are cached, in memory, until the next refresh; no body is kept."""

    kind = SourceKind.GMAIL

    def __init__(self, gmail: GmailService, refresh_minutes: float):
        self._gmail = gmail
        self._refresh = timedelta(minutes=refresh_minutes)
        self._cache: list[tuple[str, str, str, EmailCategory]] = []
        self._loaded_at: datetime | None = None

    def is_available(self) -> bool:
        return self._gmail.is_configured()

    def _emails(self, now: datetime) -> list[tuple[str, str, str, EmailCategory]]:
        if self._loaded_at is None or now - self._loaded_at >= self._refresh:
            found = []
            for message in self._gmail.search(EMAIL_QUERY, EMAIL_SEARCH_LIMIT).messages:
                category = self._gmail.classify(message).category
                if message.is_unread and category in (EmailCategory.ACTION_REQUIRED, EmailCategory.IMPORTANT):
                    sender = message.sender.display if message.sender else "an unknown sender"
                    found.append((message.message_id, sender, message.subject or "", category))
            self._cache, self._loaded_at = found, now
        return self._cache

    def collect(self, now: datetime) -> list[ProactiveSignal]:
        signals: list[ProactiveSignal] = []
        for message_id, sender, subject, category in self._emails(now)[:MAX_EMAIL_SIGNALS]:
            kind = SignalType.ACTION_REQUIRED_EMAIL if category is EmailCategory.ACTION_REQUIRED else SignalType.IMPORTANT_EMAIL
            key = make_key(kind, SourceKind.GMAIL, message_id, "unread", "")
            signals.append(ProactiveSignal(
                signal_id=key, signal_type=kind, source_type=SourceKind.GMAIL, source_id=message_id, source_reference="your Gmail inbox",
                title=safe_text(subject, 80), description=safe_text(subject, 80), priority=TaskPriority.MEDIUM, urgency=Urgency.UPCOMING,
                confidence=Confidence.MEDIUM, detected_at=now, tier="unread", metadata={"sender": safe_text(sender, 60)},
            ))
        return signals
