"""SnapshotCollector: reads what the existing services already know and normalizes it into one `Snapshot`.

READ ONLY. It calls only the public read methods of TaskService, ReminderService, EventService, CalendarService, GmailService,
MemoryService and RagService, creates or changes nothing, and keeps only short, bounded copies for the analysis.

Every source is isolated:
  * not set up            -> NOT_CONFIGURED (it is simply not a source; nothing is claimed about it);
  * set up but failing    -> UNAVAILABLE (JARVIS then says it could not check it, and never assumes "nothing there");
  * offline mode          -> cloud sources (Gmail, Calendar) are UNAVAILABLE without any network call.
One failing source never stops the others. Failures are logged by exception type only.
"""

from collections.abc import Callable
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from agent.intelligence.models import (
    CalendarItem,
    DocumentItem,
    EmailItem,
    EventRecordItem,
    MemoryItem,
    ReminderItem,
    Snapshot,
    SourceState,
    TaskItem,
    utcnow,
)
from agent.events.models import EventStatus
from agent.tasks.models import OPEN_STATUSES, ReminderStatus, TaskStatus
from backend.core.logging import get_logger
from backend.core.security.trust import sanitize_external
from integrations.calendar.models import CalendarNotConfigured
from integrations.gmail.models import GmailNotConfigured

logger = get_logger(__name__)

TASK_SCAN = 200
REMINDER_SCAN = 100
EVENT_SCAN = 100
CALENDAR_SCAN = 100
MEMORY_SCAN = 100
DOC_SCAN = 15
MAX_BODY_CHARS = 4000
MAX_DOC_CHARS = 12_000
EMAIL_QUERY = "in:inbox newer_than:7d"


class SnapshotCollector:
    def __init__(
        self,
        *,
        zone: ZoneInfo,
        clock: Callable[[], datetime] = utcnow,
        tasks=None,
        reminders=None,
        events=None,
        calendar=None,
        gmail=None,
        memory=None,
        rag=None,
        hub=None,
        offline: Callable[[], bool] = lambda: False,
        past_days: int = 7,
        lookahead_days: int = 14,
        email_limit: int = 10,
    ):
        self._zone = zone
        self._clock = clock
        self._tasks, self._reminders, self._events = tasks, reminders, events
        self._calendar, self._gmail, self._memory, self._rag = calendar, gmail, memory, rag
        self._hub = hub  # the Integration Hub (Phase 18): GitHub activity and dates found in messages, from what was synchronized
        self._offline = offline
        self._past = past_days
        self._ahead = lookahead_days
        self._email_limit = email_limit
        self._doc_cache: dict[tuple[str, str], str] = {}

    def collect(self) -> Snapshot:
        now = self._clock()
        snap = Snapshot(now=now, zone=self._zone)
        self._guard(snap, "tasks", self._tasks, lambda: self._load_tasks(snap, now))
        self._guard(snap, "reminders", self._reminders, lambda: self._load_reminders(snap, now))
        self._guard(snap, "events", self._events, lambda: self._load_event_records(snap, now))
        self._guard(snap, "memory", self._memory, lambda: self._load_memory(snap))
        self._guard(snap, "documents", self._rag, lambda: self._load_documents(snap))
        self._guard(snap, "calendar", self._calendar, lambda: self._load_calendar(snap, now), cloud=True)
        self._guard(snap, "gmail", self._gmail, lambda: self._load_email(snap), cloud=True)
        self._guard(snap, "hub", self._hub, lambda: snap.external.extend(self._hub.external_items(now)))
        return snap

    # ---- isolation ---------------------------------------------------------------------------------------------------

    def _guard(self, snap: Snapshot, name: str, service, work: Callable[[], None], *, cloud: bool = False) -> None:
        if service is None:
            snap.states[name] = SourceState.NOT_CONFIGURED
            return
        if hasattr(service, "is_configured") and not service.is_configured():
            snap.states[name] = SourceState.NOT_CONFIGURED
            return
        if cloud and self._offline():
            snap.states[name] = SourceState.UNAVAILABLE  # offline mode: no network call is made
            return
        try:
            work()
            snap.states[name] = SourceState.OK
        except (CalendarNotConfigured, GmailNotConfigured):
            snap.states[name] = SourceState.NOT_CONFIGURED
        except Exception as exc:  # noqa: BLE001 - one broken source must never break the analysis
            snap.states[name] = SourceState.UNAVAILABLE
            logger.warning("Intelligence source %s unavailable (%s)", name, type(exc).__name__)

    # ---- sources -----------------------------------------------------------------------------------------------------

    def _load_tasks(self, snap: Snapshot, now: datetime) -> None:
        open_tasks = self._tasks.list_tasks(statuses=set(OPEN_STATUSES), limit=TASK_SCAN, now=now)
        recent = self._tasks.list_tasks(statuses={TaskStatus.COMPLETED}, limit=50, now=now)
        for t in [*open_tasks, *recent]:
            estimate = t.metadata.get("estimate_minutes") if isinstance(t.metadata, dict) else None
            snap.tasks.append(TaskItem(
                task_id=t.task_id, title=sanitize_external(t.title, 200), status=t.status.value, priority=int(t.priority),
                due_at=t.due_at, created_at=t.created_at, completed_at=t.completed_at,
                estimate_minutes=int(estimate) if isinstance(estimate, (int, float)) and 5 <= estimate <= 960 else None,
                notes=sanitize_external(t.notes or "", 300),
            ))

    def _load_reminders(self, snap: Snapshot, now: datetime) -> None:
        reminders = self._reminders.list_reminders(statuses={ReminderStatus.SCHEDULED, ReminderStatus.TRIGGERED}, limit=REMINDER_SCAN)
        for r in reminders:
            snap.reminders.append(ReminderItem(
                reminder_id=r.reminder_id, message=sanitize_external(r.message, 200), scheduled_at=r.scheduled_at,
                status=r.status.value, task_id=r.task_id, missed=bool(r.metadata.get("missed")) if isinstance(r.metadata, dict) else False,
            ))

    def _load_event_records(self, snap: Snapshot, now: datetime) -> None:
        from agent.events.temporal import EventScope

        listing = self._events.list_scope(EventScope.ALL, limit=EVENT_SCAN)
        for e in listing.events:
            if e.status in (EventStatus.CANCELLED, EventStatus.COMPLETED):
                continue
            snap.event_records.append(EventRecordItem(
                event_id=e.event_id, title=sanitize_external(e.title, 200), event_type=e.event_type.value, start_at=e.start_at,
                due_at=e.due_at, status=e.status.value, confidence=e.confidence, task_id=e.task_id,
            ))

    def _load_calendar(self, snap: Snapshot, now: datetime) -> None:
        start = now - timedelta(days=self._past)
        end = now + timedelta(days=self._ahead)
        listing = self._calendar.events_between(start, end, limit=CALENDAR_SCAN)
        for e in listing.events:
            if e.is_cancelled:
                continue
            snap.calendar.append(CalendarItem(
                event_id=e.event_id, calendar_id=e.calendar_id, title=sanitize_external(e.summary or "(untitled)", 200),
                start=e.start, end=e.end, all_day=e.all_day, blocks_time=e.blocks_time,
            ))

    def _load_email(self, snap: Snapshot) -> None:
        if self._email_limit <= 0:
            return
        result = self._gmail.search(EMAIL_QUERY, self._email_limit)
        for m in result.messages:
            snap.emails.append(EmailItem(
                message_id=m.message_id, subject=sanitize_external(m.subject, 200),
                sender=sanitize_external(m.sender.name or "a sender", 60) if m.sender and "@" not in (m.sender.name or "@") else "a sender",
                received_at=m.timestamp, body=sanitize_external(m.plain_text_body or m.snippet, MAX_BODY_CHARS),
                action_requested=bool(self._gmail.action_requests(m)) if hasattr(self._gmail, "action_requests") else False,
            ))

    def _load_memory(self, snap: Snapshot) -> None:
        for m in self._memory.search(None, limit=MEMORY_SCAN):  # active memories only (the service's default)
            snap.memories.append(MemoryItem(
                memory_id=m.memory_id, content=sanitize_external(m.content, 300), created_at=m.created_at, confidence=m.confidence,
                explicit=str(getattr(m.basis, "value", m.basis)) == "explicit", kind=str(getattr(m.type, "value", m.type)),
            ))

    def _load_documents(self, snap: Snapshot) -> None:
        from agent.rag.models import DocumentStatus

        for doc in self._rag.list_documents([DocumentStatus.INDEXED])[:DOC_SCAN]:
            key = (doc.document_id, doc.indexed_at.isoformat() if doc.indexed_at else "")
            text = self._doc_cache.get(key)
            if text is None:
                chunks = self._rag.get_chunks(doc.document_id)
                text = "\n".join(c.text for c in chunks)[:MAX_DOC_CHARS]
                self._doc_cache[key] = text
            snap.documents.append(DocumentItem(doc.document_id, sanitize_external(doc.title or doc.filename, 200), doc.indexed_at, sanitize_external(text, MAX_DOC_CHARS)))

