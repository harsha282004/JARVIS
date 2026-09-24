"""Shared helpers for Google Calendar tests: Google-API-shaped JSON builders and an in-memory CalendarClient double.

The HTTP client, parser and OAuth code are tested for real (httpx.MockTransport, google-auth's real Credentials);
FakeCalendarClient is only used to test the layers ABOVE the CalendarClient interface.
"""

from datetime import datetime, timedelta, timezone

from integrations.calendar.base import CalendarClient
from integrations.calendar.models import (
    CalendarConflictError,
    CalendarEvent,
    CalendarEventAttendee,
    CalendarEventDraft,
    CalendarEventPatch,
    CalendarEventsResult,
    CalendarInfo,
    CalendarNotFound,
)
from tests.task_helpers import IST, ist  # noqa: F401  (re-exported)


def g_event(id="ev1", summary="Project meeting", start=None, end=None, date=None, end_date=None, tz="Asia/Kolkata", **extra):
    """A Google `events` resource."""
    raw = {"id": id, "status": "confirmed", "summary": summary, "etag": '"e1"'}
    if date:
        raw["start"], raw["end"] = {"date": date}, {"date": end_date or date}
    else:
        raw["start"] = {"dateTime": start or "2030-03-05T15:00:00+05:30", "timeZone": tz}
        raw["end"] = {"dateTime": end or "2030-03-05T16:00:00+05:30", "timeZone": tz}
    raw.update(extra)
    return raw


def g_calendar(id="primary@example.com", summary="Personal", primary=False, role="owner", tz="Asia/Kolkata", selected=True):
    raw = {"id": id, "summary": summary, "timeZone": tz, "accessRole": role, "selected": selected}
    if primary:
        raw["primary"] = True
    return raw


PRIMARY = CalendarInfo(calendar_id="me@example.com", summary="Personal", primary=True, timezone="Asia/Kolkata", access_role="owner")
WORK = CalendarInfo(calendar_id="work@group.calendar.google.com", summary="Work", timezone="Asia/Kolkata", access_role="writer")
HOLIDAYS = CalendarInfo(calendar_id="holidays#holiday@group.v.calendar.google.com", summary="Holidays in India", access_role="reader")
HIDDEN = CalendarInfo(calendar_id="hidden@example.com", summary="Hidden", access_role="owner", selected=False)


def cal_event(id, summary, start, end=None, calendar=PRIMARY, all_day=False, **kw) -> CalendarEvent:
    """A CalendarEvent; `start`/`end` are aware datetimes (use ist(...))."""
    end = end or (start + timedelta(hours=1))
    return CalendarEvent(
        event_id=id, calendar_id=calendar.calendar_id, etag=kw.pop("etag", f'"{id}-1"'), summary=summary,
        start=start.astimezone(timezone.utc), end=end.astimezone(timezone.utc), all_day=all_day, **kw,
    )


def all_day_event(id, summary, day, days=1, calendar=PRIMARY, **kw) -> CalendarEvent:
    start = ist(*day)
    return cal_event(id, summary, start, start + timedelta(days=days), calendar, all_day=True, **kw)


class FakeCalendarClient(CalendarClient):
    """In-memory Google Calendar (events per calendar) implementing the CalendarClient interface."""

    def __init__(self, calendars=(PRIMARY, WORK), events=()):
        self.calendars = list(calendars)
        self.events: dict[tuple[str, str], CalendarEvent] = {(e.calendar_id, e.event_id): e for e in events}
        self.calls: list[tuple] = []

    def list_calendars(self):
        self.calls.append(("list_calendars",))
        return list(self.calendars)

    def list_events(self, calendar_id, time_min, time_max, max_results, query=None, page_token=None):
        self.calls.append(("list_events", calendar_id, time_min, time_max, max_results, query))
        hits = [e for (cid, _), e in self.events.items() if cid == calendar_id and e.start < time_max and e.end > time_min and not e.is_cancelled and not e.recurrence]  # singleEvents=true: masters are not listed
        if query:
            q = query.lower()
            hits = [e for e in hits if q in f"{e.summary} {e.description} {e.location}".lower()]
        hits.sort(key=lambda e: (e.start, e.event_id))
        return CalendarEventsResult(events=hits[:max_results], truncated=len(hits) > max_results)

    def get_event(self, calendar_id, event_id):
        self.calls.append(("get_event", calendar_id, event_id))
        try:
            return self.events[(calendar_id, event_id)]
        except KeyError:
            raise CalendarNotFound("missing") from None

    def create_event(self, calendar_id, draft: CalendarEventDraft):
        self.calls.append(("create_event", calendar_id, draft))
        event = CalendarEvent(
            event_id=draft.event_id, calendar_id=calendar_id, etag='"new-1"', summary=draft.summary,
            start=draft.start.astimezone(timezone.utc), end=draft.end.astimezone(timezone.utc), all_day=draft.all_day,
            timezone=draft.timezone, location=draft.location, description=draft.description,
            attendees=[CalendarEventAttendee(email=a) for a in draft.attendees], recurrence=list(draft.recurrence),
        )
        self.events[(calendar_id, event.event_id)] = event
        return event

    def update_event(self, calendar_id, event_id, patch: CalendarEventPatch, etag=None):
        self.calls.append(("update_event", calendar_id, event_id, patch, etag))
        current = self.get_event(calendar_id, event_id)
        if etag is not None and etag != current.etag:
            raise CalendarConflictError("etag mismatch")
        changes = {}
        if patch.summary is not None:
            changes["summary"] = patch.summary
        if patch.location is not None:
            changes["location"] = patch.location
        if patch.description is not None:
            changes["description"] = patch.description
        if patch.start is not None and patch.end is not None:
            changes["start"], changes["end"] = patch.start.astimezone(timezone.utc), patch.end.astimezone(timezone.utc)
        updated = current.model_copy(update={**changes, "etag": '"upd-1"'})
        self.events[(calendar_id, event_id)] = updated
        return updated

    def delete_event(self, calendar_id, event_id, etag=None):
        self.calls.append(("delete_event", calendar_id, event_id, etag))
        current = self.get_event(calendar_id, event_id)
        if etag is not None and etag != current.etag:
            raise CalendarConflictError("etag mismatch")
        del self.events[(calendar_id, event_id)]

    def mutations(self):
        return [c for c in self.calls if c[0] in ("create_event", "update_event", "delete_event")]
