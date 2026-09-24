"""CalendarService: bounded reads, calendar resolution and conflict checks over a CalendarClient.

Google Calendar stays the source of truth: nothing is mirrored into PostgreSQL. Every retrieval is bounded
(a per-call event limit, at most MAX_CALENDARS calendars, a time window). Conflict detection reuses the Phase 11
rules (`agent.events.conflicts`), applied to temporary, never-stored `Event` objects built from calendar events.
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from agent.events.conflicts import Conflict, conflict_between, find_conflicts
from agent.events.models import Event, EventStatus, EventType
from agent.tasks.models import utcnow
from backend.core.logging import get_logger
from integrations.calendar.base import CalendarClient
from integrations.calendar.models import (
    CalendarError,
    CalendarEvent,
    CalendarEventDraft,
    CalendarEventPatch,
    CalendarInfo,
    CalendarNotFound,
)

logger = get_logger(__name__)

MAX_CALENDARS = 10  # calendars read per request
CALENDAR_CACHE_SECONDS = 300
DEFAULT_MAX_RESULTS = 20
ABSOLUTE_MAX_RESULTS = 100
CONFLICT_SCAN_LIMIT = 50


@dataclass
class EventsListing:
    events: list[CalendarEvent] = field(default_factory=list)  # start order, at most the limit
    total_seen: int = 0
    truncated: bool = False  # more events exist in the window than were returned
    calendars: list[CalendarInfo] = field(default_factory=list)  # the calendars that were read


class CalendarService:
    def __init__(self, client: CalendarClient, *, zone: ZoneInfo, max_results: int = DEFAULT_MAX_RESULTS, clock=utcnow, is_ready=None):
        self._client = client
        self._zone = zone
        self._max = max(1, min(max_results, ABSOLUTE_MAX_RESULTS))
        self._clock = clock
        self._is_ready = is_ready
        self._calendars: list[CalendarInfo] | None = None
        self._calendars_at: datetime | None = None

    @property
    def zone(self) -> ZoneInfo:
        return self._zone

    @property
    def max_results(self) -> int:
        return self._max

    def is_configured(self) -> bool:
        return bool(self._is_ready()) if self._is_ready else True

    def clamp(self, requested: int | None = None) -> int:
        return max(1, min(requested or self._max, self._max))

    # ---- calendars ------------------------------------------------------------------------------------------

    def calendars(self, refresh: bool = False) -> list[CalendarInfo]:
        now = self._clock()
        fresh = self._calendars_at is not None and (now - self._calendars_at).total_seconds() < CALENDAR_CACHE_SECONDS
        if refresh or self._calendars is None or not fresh:
            self._calendars, self._calendars_at = self._client.list_calendars(), now
        return list(self._calendars)

    def selected_calendars(self) -> list[CalendarInfo]:
        """The calendars shown in the user's list, primary first, at most MAX_CALENDARS (holiday/birthday clutter is
        whatever the user has switched on in Google Calendar)."""
        chosen = [c for c in self.calendars() if c.selected]
        chosen.sort(key=lambda c: (not c.primary, c.summary.lower()))
        return chosen[:MAX_CALENDARS]

    def find_calendar(self, name: str | None, *, writable: bool = False) -> list[CalendarInfo]:
        """Calendars matching a spoken name (whole words of the name, case-insensitive). No name: the primary calendar.
        Several results mean the caller must ask; none means no such calendar."""
        pool = [c for c in self.calendars() if not writable or c.writable]
        if not name or name.strip().lower() in ("primary", "main", "my calendar", "default"):
            return [c for c in pool if c.primary][:1]
        words = [w for w in name.lower().replace("calendar", " ").split() if w]
        exact = [c for c in pool if c.summary.lower() == name.strip().lower()]
        if exact:
            return exact
        return [c for c in pool if words and all(w in c.summary.lower() for w in words)]

    def calendar_by_id(self, calendar_id: str) -> CalendarInfo | None:
        return next((c for c in self.calendars() if c.calendar_id == calendar_id), None)

    # ---- reading --------------------------------------------------------------------------------------------

    def events_between(
        self, start: datetime, end: datetime, *, calendars: list[CalendarInfo] | None = None,
        query: str | None = None, limit: int | None = None,
    ) -> EventsListing:
        """Events in [start, end) merged from the given calendars (default: the selected ones), in start order."""
        cap = ABSOLUTE_MAX_RESULTS if limit == ABSOLUTE_MAX_RESULTS else self.clamp(limit)  # identification may look wider
        chosen = (calendars if calendars is not None else self.selected_calendars())[:MAX_CALENDARS]
        merged: list[CalendarEvent] = []
        truncated = False
        for calendar in chosen:
            result = self._client.list_events(calendar.calendar_id, start, end, cap, query)
            merged.extend(result.events)
            truncated = truncated or result.truncated
        merged.sort(key=lambda e: (e.start, e.end, e.event_id))
        total = len(merged)
        return EventsListing(events=merged[:cap], total_seen=total, truncated=truncated or total > cap, calendars=chosen)

    def get_event(self, calendar_id: str, event_id: str) -> CalendarEvent:
        return self._client.get_event(calendar_id, event_id)

    # ---- changing (only ever called after the PermissionManager authorized exactly these parameters) --------

    def create_event(self, calendar_id: str, draft: CalendarEventDraft) -> CalendarEvent:
        created = self._client.create_event(calendar_id, draft)
        logger.info("Calendar event created (calendar=%s, event=%s)", calendar_id, created.event_id)
        return created

    def update_event(self, calendar_id: str, event_id: str, patch: CalendarEventPatch, etag: str | None = None) -> CalendarEvent:
        updated = self._client.update_event(calendar_id, event_id, patch, etag)
        logger.info("Calendar event updated (calendar=%s, event=%s)", calendar_id, event_id)
        return updated

    def delete_event(self, calendar_id: str, event_id: str, etag: str | None = None) -> None:
        self._client.delete_event(calendar_id, event_id, etag)
        logger.info("Calendar event deleted (calendar=%s, event=%s)", calendar_id, event_id)

    # ---- conflicts (Phase 11 rules; informational only) -------------------------------------------------------

    def to_internal(self, event: CalendarEvent) -> Event:
        """A temporary Phase 11 `Event` for conflict maths. It is never stored."""
        now = self._clock()
        return Event(
            title=event.summary or "(no title)", event_type=EventType.EVENT, status=EventStatus.UPCOMING,
            start_at=event.start, end_at=event.end if event.end > event.start else None, timezone=self._zone.key,
            all_day=event.all_day, created_at=now, updated_at=now, metadata={"calendar_event_id": event.event_id},
        )

    def conflicts_among(self, events: list[CalendarEvent]) -> list[Conflict]:
        return find_conflicts([self.to_internal(e) for e in events if e.blocks_time], self._zone)

    def conflicting_events(
        self, start: datetime, end: datetime, all_day: bool, *, exclude: tuple[str, str] | None = None
    ) -> list[CalendarEvent]:
        """Existing events (on every selected calendar) that clash with a proposed time. `exclude` is the
        (calendar_id, event_id) being moved. Nothing is changed."""
        probe = Event(
            title="proposed", event_type=EventType.EVENT, status=EventStatus.UPCOMING, start_at=start,
            end_at=end if end > start else None, timezone=self._zone.key, all_day=all_day,
            created_at=self._clock(), updated_at=self._clock(),
        )
        window_start, window_end = start - timedelta(hours=12), end + timedelta(hours=12)
        found: list[CalendarEvent] = []
        for existing in self.events_between(window_start, window_end, limit=CONFLICT_SCAN_LIMIT).events:
            if not existing.blocks_time or (exclude and (existing.calendar_id, existing.event_id) == exclude):
                continue
            if conflict_between(probe, self.to_internal(existing), self._zone) is not None:
                found.append(existing)
        return found
