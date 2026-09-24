"""Bounded read-through mapping between Google Calendar events and Phase 11 event intelligence.

Google Calendar is the source of truth for calendar events. Nothing is mirrored wholesale:
- Reading the calendar stores nothing.
- An event JARVIS itself creates or changes gets ONE Phase 11 record, marked `GOOGLE_CALENDAR`, whose
  `source_id` is the stable `calendar_id/event_id`. That lets JARVIS's deadline and conflict intelligence know
  about it, and lets a later change or cancellation find the same record (no duplicates: the source + time key is
  unique, and the mapping is looked up by source id first). Recurring events are not mapped (one record cannot
  represent a series).
- `reconcile()` checks the mapped records against the calendar, at most every `interval` seconds and at most
  `max_checks` events per run: an event deleted (or cancelled) on Google Calendar cancels its Phase 11 record,
  and a moved or renamed one is updated, so no misleading active record lingers. It is NOT a continuous sync.
Everything here is best-effort: a failure is logged (exception type only) and never affects the calendar operation.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta

from agent.events.extraction import infer_type
from agent.events.models import EventSource, EventStatus, EventType, SourceType
from agent.events.service import EventService
from backend.core.logging import get_logger
from integrations.calendar.models import CalendarError, CalendarEvent, CalendarNotFound
from integrations.calendar.service import CalendarService

logger = get_logger(__name__)

MAX_SOURCE_ID = 128
RECONCILE_INTERVAL_SECONDS = 600
RECONCILE_MAX_CHECKS = 20


@dataclass
class ReconcileResult:
    checked: int = 0
    cancelled: int = 0
    updated: int = 0
    skipped: bool = False  # throttled or nothing mapped


class CalendarEventSync:
    def __init__(
        self, calendar: CalendarService, events: EventService | None, clock=None,
        interval_seconds: float = RECONCILE_INTERVAL_SECONDS, max_checks: int = RECONCILE_MAX_CHECKS,
    ):
        self._calendar = calendar
        self._events = events
        self._clock = clock or calendar._clock
        self._interval = timedelta(seconds=interval_seconds)
        self._max_checks = max_checks
        self._last: datetime | None = None

    @staticmethod
    def mapping_id(calendar_id: str, event_id: str) -> str:
        return f"{calendar_id}/{event_id}"

    def _source(self, event: CalendarEvent) -> EventSource | None:
        mapping = self.mapping_id(event.calendar_id, event.event_id)
        if len(mapping) > MAX_SOURCE_ID:
            return None
        return EventSource(source_type=SourceType.GOOGLE_CALENDAR, source_id=mapping, reference="your Google Calendar")

    def record_created(self, event: CalendarEvent) -> None:
        """One Phase 11 record for an event JARVIS just created (idempotent). Skips recurring events."""
        if self._events is None or event.is_recurring:
            return
        source = self._source(event)
        if source is None:
            return
        try:
            self._events.create_event(
                event.summary or "(no title)", infer_type(event.summary) or EventType.EVENT, start_at=event.start,
                end_at=event.end if event.end > event.start else None, all_day=event.all_day, source=source,
                metadata={"calendar_id": event.calendar_id, "event_id": event.event_id},
            )
        except Exception as exc:  # noqa: BLE001 - never affects the calendar operation
            logger.warning("Calendar event mapping failed (%s)", type(exc).__name__)

    def record_updated(self, event: CalendarEvent) -> None:
        if self._events is None or event.is_recurring:
            return
        try:
            mapped = self._events.find_by_source(SourceType.GOOGLE_CALENDAR, self.mapping_id(event.calendar_id, event.event_id))
            if mapped is None:
                self.record_created(event)
            elif mapped.is_open or mapped.status is EventStatus.MISSED:
                self._events.update_event(
                    mapped.event_id, title=event.summary or "(no title)", start_at=event.start,
                    end_at=event.end if event.end > event.start else None, all_day=event.all_day,
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Calendar event mapping update failed (%s)", type(exc).__name__)

    def record_cancelled(self, calendar_id: str, event_id: str) -> None:
        if self._events is None:
            return
        try:
            mapped = self._events.find_by_source(SourceType.GOOGLE_CALENDAR, self.mapping_id(calendar_id, event_id))
            if mapped is not None and mapped.status in (EventStatus.UPCOMING, EventStatus.ACTIVE, EventStatus.MISSED):
                self._events.cancel_event(mapped.event_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Calendar event mapping cancel failed (%s)", type(exc).__name__)

    def reconcile(self, force: bool = False) -> ReconcileResult:
        """Check mapped, still-open records against Google Calendar (bounded, throttled)."""
        result = ReconcileResult()
        now = self._clock()
        if self._events is None or (not force and self._last is not None and now - self._last < self._interval):
            result.skipped = True
            return result
        self._last = now
        try:
            mapped = self._events.list_by_source(SourceType.GOOGLE_CALENDAR, open_only=True, limit=self._max_checks)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Calendar reconcile could not read mapped events (%s)", type(exc).__name__)
            result.skipped = True
            return result
        for record in mapped:
            calendar_id, _, event_id = (record.source.source_id or "").partition("/")
            if not event_id:
                continue
            result.checked += 1
            try:
                current = self._calendar.get_event(calendar_id, event_id)
            except CalendarNotFound:
                self.record_cancelled(calendar_id, event_id)
                result.cancelled += 1
                continue
            except CalendarError as exc:  # unreachable or unauthorized: stop, leave everything as it is
                logger.warning("Calendar reconcile stopped (%s)", type(exc).__name__)
                break
            if current.is_cancelled:
                self.record_cancelled(calendar_id, event_id)
                result.cancelled += 1
            elif (current.start, current.summary) != (record.start_at, record.title):
                self.record_updated(current)
                result.updated += 1
        return result
