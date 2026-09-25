"""CalendarAdapter: the hub's view of Google Calendar. Wraps the existing CalendarService (nothing is re-implemented).

* normalization -> EVENT items that keep the external event id (`external_id`) and `calendar_id/event_id` as the source id: that pair is what makes a later
                   update or delete address the right event.
* sync          -> a bounded window (a week back to two months ahead). The cursor remembers which events (and when they start) the last sync saw; an event that is
                   no longer returned, and whose start is still inside the window, is reported as removed. Unchanged events are skipped by content hash in the store.
* issues        -> overlaps, duplicates (same title and start) and task deadlines that fall inside an event: facts only, nothing is modified.
* verified writes -> create / update / delete return whether the calendar's own read-back confirms the result. They are only ever called after the user's
                   confirmation (ConfirmationEngine / the Phase 12 tools); this class never decides to write.
"""

import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from backend.core.security.trust import sanitize_external
from integrations.calendar.models import CalendarEvent, CalendarEventDraft, CalendarEventPatch, CalendarNotFound
from integrations.calendar.service import CalendarService
from integrations.hub.models import ItemKind, NormalizedItem, Permission, utcnow
from integrations.hub.registry import IntegrationAdapter, SyncBatch

PAST_DAYS = 7
AHEAD_DAYS = 60
WINDOW_LIMIT = 100
_TOLERANCE = timedelta(minutes=1)


def normalize_event(event: CalendarEvent, retrieved_at: datetime) -> NormalizedItem:
    people = [a.display for a in event.attendees if a.display and not a.self_][:10]
    return NormalizedItem(
        ItemKind.EVENT, "calendar", f"{event.calendar_id}/{event.event_id}", event.start, sanitize_external(event.summary or "(untitled)", 200),
        sanitize_external(event.description, 240),
        {
            "end": event.end.isoformat(), "all_day": event.all_day, "timezone": event.timezone, "location": sanitize_external(event.location, 120),
            "status": event.status, "calendar_id": event.calendar_id, "participants": [sanitize_external(p, 60) for p in people],
            "blocks_time": event.blocks_time, "recurring": event.is_recurring, "etag": event.etag,
        },
        "high", event.event_id, retrieved_at,
    )


@dataclass(frozen=True)
class CalendarIssue:
    kind: str  # overlap | duplicate | deadline_overlap
    text: str
    items: tuple[str, ...]  # source ids


def find_calendar_issues(events: list[CalendarEvent], now: datetime, zone, task_deadlines: list[tuple[str, datetime]] | None = None) -> list[CalendarIssue]:
    """Facts about the calendar: no ranking, no advice, nothing changed."""
    from agent.intelligence.phrasing import clock, day_word

    timed = sorted((e for e in events if e.blocks_time and not e.all_day), key=lambda e: e.start)
    issues: list[CalendarIssue] = []
    seen: set[frozenset[str]] = set()
    for i, a in enumerate(timed):
        for b in timed[i + 1:]:
            if b.start >= a.end:
                break
            pair = frozenset({a.event_id, b.event_id})
            if pair in seen:
                continue
            seen.add(pair)
            if a.summary.strip().lower() == b.summary.strip().lower() and a.start == b.start:
                issues.append(CalendarIssue("duplicate", f"'{a.summary}' appears twice {day_word(a.start, now, zone)} at {clock(a.start, zone)}.", (a.event_id, b.event_id)))
            elif a.start == b.start:
                issues.append(CalendarIssue("overlap", f"You have two events scheduled at {clock(a.start, zone)} {day_word(a.start, now, zone)}: '{a.summary}' and '{b.summary}'.", (a.event_id, b.event_id)))
            else:
                issues.append(CalendarIssue("overlap", f"'{a.summary}' ({clock(a.start, zone)} to {clock(a.end, zone)}) overlaps '{b.summary}' ({clock(b.start, zone)} to {clock(b.end, zone)}) {day_word(b.start, now, zone)}.", (a.event_id, b.event_id)))
    for title, due in task_deadlines or []:
        for e in timed:
            if e.start <= due < e.end:
                issues.append(CalendarIssue("deadline_overlap", f"'{title}' is due {day_word(due, now, zone)} at {clock(due, zone)}, during '{e.summary}'.", (e.event_id,)))
    return issues


class CalendarAdapter(IntegrationAdapter):
    name = "calendar"
    display_name = "Google Calendar"
    permissions = frozenset({Permission.READ_EVENTS, Permission.CREATE_EVENT, Permission.UPDATE_EVENT, Permission.DELETE_EVENT})
    sync_interval_seconds = 900.0
    manual_connect = True

    def __init__(self, service: CalendarService, authenticator, zone, clock: Callable[[], datetime] = utcnow):
        self._svc, self._auth, self._zone, self._clock = service, authenticator, zone, clock

    @property
    def service(self) -> CalendarService:
        return self._svc

    # ---- state / operations ------------------------------------------------------------------------------------------------
    def is_configured(self) -> bool:
        return self._svc.is_configured()

    def is_authenticated(self) -> bool:
        return self._auth.is_ready()

    def authenticate(self) -> None:
        self._auth.authorize()

    def health_check(self) -> str:
        self._svc.calendars(refresh=True)
        return "Calendar reachable"

    def disconnect(self) -> None:
        self._auth.forget()

    def revoke(self) -> bool:
        return self._auth.revoke_remote()

    def events(self, start: datetime, end: datetime, query: str | None = None, limit: int = WINDOW_LIMIT) -> list[CalendarEvent]:
        return self._svc.events_between(start, end, query=query, limit=limit).events

    def search(self, query: str, limit: int) -> list[NormalizedItem]:
        now = self._clock()
        found = self.events(now - timedelta(days=30), now + timedelta(days=180), query, limit)
        return [normalize_event(e, now) for e in found[:limit]]

    def schedule(self, start: datetime, end: datetime) -> list[NormalizedItem]:
        now = self._clock()
        return [normalize_event(e, now) for e in self.events(start, end)]

    def fetch(self, source_id: str) -> NormalizedItem:
        calendar_id, _, event_id = source_id.rpartition("/")
        return normalize_event(self._svc.get_event(calendar_id, event_id), self._clock())

    def sync(self, cursor: str | None, limit: int) -> SyncBatch:
        now = self._clock()
        start, end = now - timedelta(days=PAST_DAYS), now + timedelta(days=AHEAD_DAYS)
        events = self.events(start, end, None, WINDOW_LIMIT)
        old = json.loads(cursor).get("e", {}) if cursor else {}
        current = {f"{e.calendar_id}/{e.event_id}": int(e.start.timestamp()) for e in events}
        removed = [(ItemKind.EVENT, sid) for sid, ts in old.items() if sid not in current and ts >= start.timestamp()]
        return SyncBatch([normalize_event(e, now) for e in events], json.dumps({"e": current}), removed)

    # ---- verified writes (only ever called after the user confirmed) --------------------------------------------------------
    def create_verified(self, calendar_id: str, draft: CalendarEventDraft) -> tuple[CalendarEvent | None, bool]:
        created = self._svc.create_event(calendar_id, draft)
        try:
            back = self._svc.get_event(calendar_id, created.event_id)
        except Exception:  # noqa: BLE001 - unreadable after creating: reported as unverified, never as done
            return created, False
        ok = back.summary == draft.summary and abs(back.start - draft.start.astimezone(timezone.utc)) <= _TOLERANCE and abs(back.end - draft.end.astimezone(timezone.utc)) <= _TOLERANCE
        return back, ok

    def update_verified(self, calendar_id: str, event_id: str, patch: CalendarEventPatch, etag: str | None = None) -> tuple[CalendarEvent, bool]:
        updated = self._svc.update_event(calendar_id, event_id, patch, etag)
        try:
            back = self._svc.get_event(calendar_id, event_id)
        except Exception:  # noqa: BLE001
            return updated, False
        ok = (patch.summary is None or back.summary == patch.summary) and (patch.start is None or abs(back.start - patch.start.astimezone(timezone.utc)) <= _TOLERANCE) \
            and (patch.end is None or abs(back.end - patch.end.astimezone(timezone.utc)) <= _TOLERANCE)
        return back, ok

    def delete_verified(self, calendar_id: str, event_id: str, etag: str | None = None) -> bool:
        self._svc.delete_event(calendar_id, event_id, etag)
        try:
            back = self._svc.get_event(calendar_id, event_id)
        except CalendarNotFound:
            return True  # the calendar itself says it is gone
        except Exception:  # noqa: BLE001
            return False
        return back.is_cancelled  # Google keeps a deleted event as "cancelled": that is the calendar confirming the deletion
