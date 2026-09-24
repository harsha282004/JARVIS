"""ProductivityCollector: gathers what the existing services already know and normalizes it into a ProductivityContext.

It only READS, through the public read methods of the existing services (TaskService, ReminderService, EventService,
CalendarService, GmailService, MessagingService). It creates no task, reminder, event or email, calls no mutating method, and
keeps no copy of source data: items hold a short sanitized title, a time, the analysis result and a reference to the source.

Every source is isolated. A source that is not set up is silently left out; one that is set up but failing is recorded as
UNAVAILABLE and the briefing says so honestly; neither stops the others. Failures are logged with the exception type only.
Everything is bounded (task scan 200, calendar 50, e-mail and message limits from configuration).
"""

import re
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from agent.briefing.models import (
    BriefingItem,
    BriefingWindow,
    ConflictInfo,
    ConflictKind,
    ItemKind,
    PreparationLink,
    PriorityLevel,
    ProductivityContext,
    SourceName,
    SourceRef,
    SourceState,
    SourceStatus,
)
from agent.briefing.priority import Facts, PriorityAnalyzer
from agent.briefing.windows import horizon_end, window_bounds
from agent.events.models import DUE_TYPES, EventStatus, EventType, SourceType
from agent.events.temporal import EventScope
from agent.proactive.messages import safe_text
from agent.tasks.models import OPEN_STATUSES, ReminderStatus, TaskPriority, TaskStatus
from agent.tasks.formatting import local_day_bounds
from backend.core.logging import get_logger
from integrations.calendar.models import CalendarNotConfigured
from integrations.gmail.models import EmailCategory, GmailNotConfigured
from integrations.messaging.models import MessageCategory, MessagingNotConfigured, UnsupportedCapability

logger = get_logger(__name__)

TASK_SCAN = 200
EVENT_SCAN = 100
CALENDAR_SCAN = 50
UPCOMING_CAP = 30
MESSAGE_SCAN = 20
MAX_MESSAGE_ITEMS = 3
EMAIL_QUERY = "is:unread in:inbox newer_than:3d"

_TASKS = SourceRef(source=SourceName.TASKS, label="your task list")
_REMINDERS = SourceRef(source=SourceName.REMINDERS, label="your reminders")
_PREP_VERB = re.compile(r"^\s*(prepare|prep|review|practice|practise|study|revise|rehearse|polish|update|research)\b", re.I)
_PREP_EVENT = re.compile(r"\b(interview|exam|presentation|demo|viva|defen[cs]e|pitch|review|appointment)\b", re.I)
_GENERIC = frozenset({"the", "a", "an", "and", "for", "with", "my", "to", "of", "on", "in", "task", "meeting", "call", "prepare", "review", "update", "final", "new", "work"})
_ADDRESS = re.compile(r"\S+@\S+")


def _clean(text: str, limit: int = 80) -> str:
    return safe_text(_ADDRESS.sub("an address", text), limit)


def _sender_name(display: str) -> str:
    """A spoken sender: a name, never an address."""
    return "a sender" if not display or "@" in display else safe_text(display, 40)


def _words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z]{4,}", text.lower()) if w not in _GENERIC}


class ProductivityCollector:
    def __init__(
        self,
        *,
        zone: ZoneInfo,
        clock,
        tasks=None,
        reminders=None,
        events=None,
        calendar=None,
        gmail=None,
        messaging=None,
        max_items: int = 10,
        lookahead_days: int = 7,
        email_limit: int = 5,
    ):
        self._zone = zone
        self._clock = clock
        self._tasks, self._reminders, self._events = tasks, reminders, events
        self._calendar, self._gmail, self._messaging = calendar, gmail, messaging
        self._max = max(1, max_items)
        self._lookahead = max(1, lookahead_days)
        self._email_limit = max(0, email_limit)
        self._analyzer = PriorityAnalyzer(zone)

    # ---- entry points ---------------------------------------------------------------------------------------------------------

    def collect(self, window: BriefingWindow) -> ProductivityContext:
        """Forward-looking context for TODAY, TOMORROW, THIS_WEEK or NEXT_7_DAYS (a past window is treated as `collect_missed`)."""
        if window.is_past:
            return self.collect_missed(window)
        now = self._clock()
        start, end = window_bounds(window, now, self._zone)
        horizon = horizon_end(end, now, self._zone, self._lookahead)
        ctx = ProductivityContext(now=now, timezone=self._zone.key, window=window, start=start, end=end)
        statuses: dict[SourceName, SourceState] = {}
        open_tasks = self._collect_tasks(ctx, now, start, end, horizon, statuses)
        self._collect_reminders(ctx, now, start, end, statuses)
        calendar_events = self._collect_calendar(ctx, now, start, end, horizon, statuses)
        event_rows = self._collect_events(ctx, now, window, end, horizon, statuses, calendar_ok=statuses.get(SourceName.CALENDAR) is SourceState.OK)
        self._collect_email(ctx, now, statuses, start=None, end=None)
        self._collect_messages(ctx, now, statuses)
        self._cluster_deadlines(ctx)
        self._prepare(ctx, now, open_tasks, event_rows, calendar_events)
        self._sort(ctx)
        ctx.statuses = [SourceStatus(name=n, state=s) for n, s in statuses.items()]
        return ctx

    def collect_missed(self, window: BriefingWindow) -> ProductivityContext:
        """What already happened in a past window: tasks that became overdue, reminders that expired unnoticed, deadlines that
        passed, important e-mail from then. Only what the existing services report; nothing is guessed."""
        now = self._clock()
        start, end = window_bounds(window, now, self._zone)
        ctx = ProductivityContext(now=now, timezone=self._zone.key, window=window, start=start, end=end)
        statuses: dict[SourceName, SourceState] = {}
        self._guard(SourceName.TASKS, statuses, lambda: self._missed_tasks(ctx, now, start, end), self._tasks)
        self._guard(SourceName.REMINDERS, statuses, lambda: self._missed_reminders(ctx, now, start, end), self._reminders)
        self._guard(SourceName.EVENTS, statuses, lambda: self._missed_deadlines(ctx, now, start, end), self._events)
        self._guard(SourceName.CALENDAR, statuses, lambda: self._past_events(ctx, start, end), self._calendar, integration=True)
        self._collect_email(ctx, now, statuses, start=start, end=end)
        ctx.missed.sort(key=lambda i: (-i.score, i.when is None, i.when, i.key))
        ctx.statuses = [SourceStatus(name=n, state=s) for n, s in statuses.items()]
        return ctx

    # ---- isolation ------------------------------------------------------------------------------------------------------------------

    def _guard(self, name: SourceName, statuses: dict, work, service: Any, *, integration: bool = False) -> Any:
        """Run one source's work. Missing service: NOT_CONFIGURED. Not set up: NOT_CONFIGURED. Failing: UNAVAILABLE."""
        if service is None:
            statuses.setdefault(name, SourceState.NOT_CONFIGURED)
            return None
        if integration and hasattr(service, "is_configured") and not service.is_configured():
            statuses[name] = SourceState.NOT_CONFIGURED
            return None
        try:
            result = work()
            statuses.setdefault(name, SourceState.OK)
            return result
        except (CalendarNotConfigured, GmailNotConfigured, MessagingNotConfigured):
            statuses[name] = SourceState.NOT_CONFIGURED
        except UnsupportedCapability:
            statuses[name] = SourceState.UNSUPPORTED
        except Exception as exc:  # noqa: BLE001 - one broken source must never break the briefing
            statuses[name] = SourceState.UNAVAILABLE
            logger.warning("Briefing source %s unavailable (%s)", name.value, type(exc).__name__)
        return None

    # ---- item construction ----------------------------------------------------------------------------------------------------------

    def _item(self, kind: ItemKind, source: SourceRef, source_id: str, title: str, now: datetime, *, when=None, ends=None, all_day=False,
              explicit: TaskPriority | None = None, event_type: EventType | None = None, action_email: bool = False, detail: str = "",
              flags: tuple[str, ...] = ()) -> BriefingItem:
        assessment = self._analyzer.assess(Facts(kind=kind, when=when, explicit=explicit, event_type=event_type, action_email=action_email, all_day=all_day), now)
        if not source.source_id:
            source = source.model_copy(update={"source_id": source_id[:128]})  # every item points at its own record
        extra = tuple(f for f in flags)
        if explicit is not None and explicit >= TaskPriority.HIGH:
            extra += ("high_priority",)
        return BriefingItem(
            key=f"{kind.value}:{source_id}", kind=kind, title=_clean(title, 80) or "(untitled)", when=when, ends=ends, all_day=all_day, level=assessment.level,
            score=assessment.score, explicit_priority=explicit, reasons=assessment.reasons, source=source, detail=detail, flags=extra,
        )

    def _task_item(self, task, now: datetime) -> BriefingItem:
        flags = ("overdue",) if task.due_at is not None and task.due_at < now else ()
        return self._item(ItemKind.TASK, _TASKS, task.task_id, task.title, now, when=task.due_at, explicit=task.priority, flags=flags)

    # ---- tasks ------------------------------------------------------------------------------------------------------------------------

    def _collect_tasks(self, ctx: ProductivityContext, now, start, end, horizon, statuses) -> list[BriefingItem]:
        opened: list[BriefingItem] = []

        def work() -> None:
            for task in self._tasks.list_tasks(statuses=set(OPEN_STATUSES), limit=TASK_SCAN, now=now):
                item = self._task_item(task, now)
                opened.append(item)
                due = task.due_at
                if due is not None and due < now:
                    ctx.tasks_overdue.append(item)
                elif due is not None and start <= due < end:
                    ctx.tasks_due.append(item)
                elif due is not None and end <= due < horizon:
                    ctx.tasks_upcoming.append(item)
                if task.priority >= TaskPriority.HIGH:
                    ctx.tasks_high.append(item)
            if ctx.window is BriefingWindow.TODAY:
                today = local_day_bounds(0, now, self._zone)
                done = self._tasks.list_tasks(statuses={TaskStatus.COMPLETED}, limit=TASK_SCAN, now=now)
                ctx.completed_today = sum(1 for t in done if t.completed_at is not None and today[0] <= t.completed_at < today[1])

        self._guard(SourceName.TASKS, statuses, work, self._tasks)
        return opened

    # ---- reminders ---------------------------------------------------------------------------------------------------------------------

    def _collect_reminders(self, ctx: ProductivityContext, now, start, end, statuses) -> None:
        def work() -> None:
            since = max(start, now)  # a reminder that already fired is not "still to come"
            for r in self._reminders.list_reminders(statuses={ReminderStatus.SCHEDULED}, scheduled_from=since, scheduled_before=end, limit=50):
                ctx.reminders.append(self._item(ItemKind.REMINDER, _REMINDERS, r.reminder_id, r.message, now, when=r.scheduled_at))

        self._guard(SourceName.REMINDERS, statuses, work, self._reminders)

    # ---- calendar -------------------------------------------------------------------------------------------------------------------------

    def _calendar_item(self, e, now: datetime) -> BriefingItem:
        ref = SourceRef(source=SourceName.CALENDAR, source_id=f"{e.calendar_id}/{e.event_id}"[-128:], label="your Google Calendar")
        flags = tuple(f for f, on in (("tentative", e.status == "tentative"), ("repeats", bool(e.recurring_event_id or e.recurrence))) if on)
        return self._item(ItemKind.CALENDAR_EVENT, ref, ref.source_id, e.summary or "(no title)", now, when=e.start, ends=e.end, all_day=e.all_day, flags=flags)

    def _collect_calendar(self, ctx: ProductivityContext, now, start, end, horizon, statuses) -> list:
        kept: list = []
        kept_upcoming: list = []  # also considered for preparation matching (an interview tomorrow while briefing today)

        def work() -> None:
            listing = self._calendar.events_between(start, end, limit=CALENDAR_SCAN)
            ctx.calendar_truncated = bool(listing.truncated)
            for e in listing.events:
                if e.is_cancelled or e.declined_by_me:
                    continue
                if ctx.window is BriefingWindow.TODAY and not e.all_day and e.end <= now:
                    continue  # already over
                kept.append(e)
                ctx.events.append(self._calendar_item(e, now))
            if end < horizon:
                soon = self._calendar.events_between(end, min(horizon, end + timedelta(days=self._lookahead)), limit=UPCOMING_CAP)
                for e in soon.events[:UPCOMING_CAP]:
                    if not e.is_cancelled and not e.declined_by_me:
                        ctx.events_upcoming.append(self._calendar_item(e, now))
                        kept_upcoming.append(e)
            timed = [e for e in kept if not e.all_day]
            for c in self._calendar.conflicts_among(timed):
                ctx.conflicts.append(ConflictInfo(
                    kind=ConflictKind.OVERLAP, first=c.first.metadata.get("calendar_event_id", c.first.event_id), second=c.second.metadata.get("calendar_event_id", c.second.event_id),
                    first_title=_clean(c.first.title, 60), second_title=_clean(c.second.title, 60), when=max(c.first.start_at, c.second.start_at), source=SourceName.CALENDAR))

        self._guard(SourceName.CALENDAR, statuses, work, self._calendar, integration=True)
        return [*kept, *kept_upcoming]

    def _past_events(self, ctx: ProductivityContext, start, end) -> None:
        ctx.past_event_count = len([e for e in self._calendar.events_between(start, end, limit=CALENDAR_SCAN).events if not e.is_cancelled and not e.declined_by_me])

    # ---- Phase 11 events and deadlines ---------------------------------------------------------------------------------------------------

    _SCOPES = {BriefingWindow.TODAY: EventScope.TODAY, BriefingWindow.TOMORROW: EventScope.TOMORROW, BriefingWindow.THIS_WEEK: EventScope.THIS_WEEK,
               BriefingWindow.NEXT_7_DAYS: EventScope.NEXT_7_DAYS}

    def _event_item(self, e, now: datetime) -> BriefingItem:
        ref = SourceRef(source=SourceName.EVENTS, source_id=e.event_id, label="your events and deadlines")
        kind = ItemKind.DEADLINE if (e.is_deadline or e.event_type in DUE_TYPES) else ItemKind.EVENT
        flags = ("overdue",) if e.is_deadline and e.due_at is not None and e.due_at < now else ()
        return self._item(kind, ref, e.event_id, e.title, now, when=e.anchor, ends=e.end_at, all_day=e.all_day, explicit=e.priority, event_type=e.event_type, flags=flags)

    def _collect_events(self, ctx: ProductivityContext, now, window, end, horizon, statuses, *, calendar_ok: bool) -> list:
        rows: list = []

        def hidden(e) -> bool:
            if e.status is EventStatus.UNKNOWN:  # unconfirmed guesses are never presented as commitments
                return True
            return calendar_ok and e.source.source_type is SourceType.GOOGLE_CALENDAR  # the calendar itself covers its mirror

        def work() -> None:
            seen: set[str] = set()

            def take(e) -> BriefingItem | None:
                """Rows feed preparation matching; an event linked to a task is not shown (the task's own item covers it)."""
                if hidden(e) or e.event_id in seen:
                    return None
                seen.add(e.event_id)
                rows.append(e)
                return None if e.task_id is not None else self._event_item(e, now)

            for e in self._events.list_scope(self._SCOPES[window], limit=EVENT_SCAN).events:
                item = take(e)
                if item is None:
                    continue
                if item.kind is ItemKind.DEADLINE:
                    (ctx.deadlines_overdue if e.due_at is not None and e.due_at < now else ctx.deadlines_due).append(item)
                else:
                    ctx.events.append(item)
            for e in self._events.list_scope(EventScope.OVERDUE, limit=EVENT_SCAN).events:
                if e.is_deadline:
                    item = take(e)
                    if item is not None:
                        ctx.deadlines_overdue.append(item)
            for e in self._events.list_scope(EventScope.UPCOMING, limit=EVENT_SCAN).events:
                if not (end <= e.anchor < horizon):
                    continue
                item = take(e)
                if item is not None:
                    (ctx.deadlines_upcoming if item.kind is ItemKind.DEADLINE else ctx.events_upcoming).append(item)

        self._guard(SourceName.EVENTS, statuses, work, self._events)
        return rows

    # ---- e-mail and messages -------------------------------------------------------------------------------------------------------------

    def _collect_email(self, ctx: ProductivityContext, now, statuses, *, start, end) -> None:
        if self._gmail is None or self._email_limit == 0:
            statuses.setdefault(SourceName.GMAIL, SourceState.NOT_CONFIGURED if self._gmail is None else SourceState.DISABLED)
            return

        def work() -> None:
            ref_label = "your Gmail inbox"
            for m in self._gmail.search(EMAIL_QUERY, self._email_limit * 2).messages:
                if not m.is_unread:
                    continue
                if start is not None and m.timestamp is not None and not (start <= m.timestamp < end):
                    continue
                category = self._gmail.classify(m).category
                if category not in (EmailCategory.ACTION_REQUIRED, EmailCategory.IMPORTANT):
                    continue  # ordinary mail is never summarized here
                action = category is EmailCategory.ACTION_REQUIRED
                ref = SourceRef(source=SourceName.GMAIL, source_id=m.message_id, label=ref_label)
                sender = _sender_name(m.sender.display if m.sender else "")
                item = self._item(ItemKind.EMAIL, ref, m.message_id, m.subject or "(no subject)", now, when=m.timestamp, action_email=action, detail=sender,
                                  flags=("unread", "action_required" if action else "important"))
                target = ctx.emails_action if action else ctx.emails_important
                if len(ctx.emails_action) + len(ctx.emails_important) < self._email_limit:
                    target.append(item)
                    if start is not None:
                        ctx.missed.append(item)

        self._guard(SourceName.GMAIL, statuses, work, self._gmail, integration=True)

    def _collect_messages(self, ctx: ProductivityContext, now, statuses) -> None:
        if self._messaging is None:
            statuses.setdefault(SourceName.MESSAGING, SourceState.NOT_CONFIGURED)
            return

        def work() -> None:
            if not self._messaging.is_configured():
                statuses[SourceName.MESSAGING] = SourceState.NOT_CONFIGURED  # no provider set up: never an error, never polled
                return
            page = self._messaging.messages(limit=MESSAGE_SCAN)
            for m in page.messages:
                if len(ctx.messages) >= MAX_MESSAGE_ITEMS:
                    break
                if self._messaging.classify(m).category is not MessageCategory.ACTION_REQUIRED:
                    continue
                ref = SourceRef(source=SourceName.MESSAGING, source_id=m.message_id, label=f"your {m.provider.capitalize()} messages")
                sender = _sender_name(m.sender.display if m.sender else "")
                ctx.messages.append(self._item(ItemKind.MESSAGE, ref, m.message_id, f"a message from {sender}", now, when=m.timestamp, detail=sender, flags=("action_required",)))

        self._guard(SourceName.MESSAGING, statuses, work, self._messaging)

    # ---- analysis that needs the whole context -------------------------------------------------------------------------------------------

    def _cluster_deadlines(self, ctx: ProductivityContext) -> None:
        """Several important deadlines close together (within 24 hours of each other, all at HIGH or above)."""
        pool = [i for i in (*ctx.tasks_due, *ctx.tasks_upcoming, *ctx.deadlines_due, *ctx.deadlines_upcoming) if i.level >= PriorityLevel.HIGH and i.when is not None]
        pool.sort(key=lambda i: (i.when, i.key))
        for a, b in zip(pool, pool[1:]):
            if b.when - a.when <= timedelta(hours=24) and (a.when - ctx.now) <= timedelta(days=3):
                ctx.conflicts.append(ConflictInfo(kind=ConflictKind.DEADLINE_CLUSTER, first=a.key, second=b.key, first_title=a.title, second_title=b.title, when=a.when, source=SourceName.TASKS))
                break  # one factual mention is enough

    def _prepare(self, ctx: ProductivityContext, now, open_tasks: list[BriefingItem], event_rows: list, calendar_events: list) -> None:
        """Existing pending tasks that look like preparation for an upcoming interview, exam, presentation, ... (never invented)."""
        horizon = now + timedelta(days=2)
        targets: list[tuple[str, str, datetime, set[str], str | None]] = []
        for e in event_rows:
            if e.event_type in (EventType.INTERVIEW, EventType.EXAM, EventType.APPOINTMENT, EventType.MEETING) and e.start_at is not None and now <= e.start_at < horizon:
                targets.append((f"event:{e.event_id}", e.title, e.start_at, _words(e.title), e.task_id))
        for c in calendar_events:
            if not c.all_day and now <= c.start < horizon and _PREP_EVENT.search(c.summary or ""):
                targets.append((f"calendar_event:{c.calendar_id}/{c.event_id}"[:200], c.summary or "", c.start, _words(c.summary or ""), None))
        used: set[str] = set()
        for key, title, when, words, linked_task in sorted(targets, key=lambda t: (t[2], t[0])):
            found = 0
            for task in sorted(open_tasks, key=lambda t: (t.when is None, t.when, t.key)):
                if task.key in used or found >= 2 or len(ctx.preparation) >= 3:
                    continue
                if task.when is not None and task.when > when:
                    continue  # due after the event: not preparation for it
                basis = None
                if linked_task is not None and task.key == f"task:{linked_task}":
                    basis = "linked"
                elif words & _words(task.title):
                    basis = "named"
                elif _PREP_VERB.match(task.title):
                    basis = "pending"
                if basis:
                    used.add(task.key)
                    found += 1
                    ctx.preparation.append(PreparationLink(
                        event_key=key, event_title=_clean(title, 80), event_when=when, task_key=task.key, task_title=task.title, basis=basis))
        # the linked/prep tasks must be findable from the context so the briefing can name them
        by_key = {t.key: t for t in open_tasks}
        for link in ctx.preparation:
            task = by_key.get(link.task_key)
            if task is not None and task.key not in ctx.all_items():
                ctx.tasks_upcoming.append(task)

    def _sort(self, ctx: ProductivityContext) -> None:
        def order(items: list[BriefingItem]) -> None:
            items.sort(key=lambda i: (-i.score, i.when is None, i.when, i.key))

        def by_time(items: list[BriefingItem]) -> None:
            items.sort(key=lambda i: (i.when is None, i.when, i.key))

        for attr in ("tasks_overdue", "tasks_due", "tasks_upcoming", "tasks_high", "deadlines_overdue", "deadlines_due", "deadlines_upcoming", "emails_action", "emails_important", "messages"):
            order(getattr(ctx, attr))
        for attr in ("events", "events_upcoming", "reminders"):
            by_time(getattr(ctx, attr))

    # ---- what was missed --------------------------------------------------------------------------------------------------------------------

    def _missed_tasks(self, ctx: ProductivityContext, now, start, end) -> None:
        for task in self._tasks.list_tasks(statuses=set(OPEN_STATUSES), limit=TASK_SCAN, now=now):
            if task.due_at is not None and start <= task.due_at < min(end, now):
                ctx.missed.append(self._task_item(task, now))

    def _missed_reminders(self, ctx: ProductivityContext, now, start, end) -> None:
        for r in self._reminders.list_reminders(statuses={ReminderStatus.EXPIRED}, scheduled_from=start, scheduled_before=min(end, now), limit=50):
            ctx.missed.append(self._item(ItemKind.REMINDER, _REMINDERS, r.reminder_id, r.message, now, when=r.scheduled_at, flags=("missed",)))

    def _missed_deadlines(self, ctx: ProductivityContext, now, start, end) -> None:
        for e in self._events.list_scope(EventScope.OVERDUE, limit=EVENT_SCAN).events:
            if e.is_deadline and e.due_at is not None and start <= e.due_at < min(end, now):
                ctx.missed.append(self._event_item(e, now))
