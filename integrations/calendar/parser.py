"""Google Calendar API JSON -> JARVIS models. Tolerant of missing and malformed fields.

Only what the feature needs is kept; unknown fields are dropped. Text from the calendar is untrusted and is
cleaned (control characters removed, angle brackets defused, length capped).
"""

import re
from datetime import date, datetime, time, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from integrations.calendar.models import (
    CalendarEvent,
    CalendarEventAttendee,
    CalendarEventReminder,
    CalendarInfo,
    CalendarResponseError,
)

MAX_ATTENDEES = 200
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


def clean(value: Any, limit: int) -> str:
    if not isinstance(value, str):
        return ""
    return " ".join(_CONTROL.sub(" ", value).replace("<", "(").replace(">", ")").split())[:limit]


def _zone(name: Any, fallback: ZoneInfo) -> ZoneInfo:
    if isinstance(name, str) and name:
        try:
            return ZoneInfo(name)
        except (ZoneInfoNotFoundError, ValueError, OSError):
            pass
    return fallback


def _datetime(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(timezone.utc) if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _when(part: Any, fallback: ZoneInfo) -> tuple[datetime, bool, str | None]:
    """(start-or-end as aware UTC, is_all_day, the event's own zone name)."""
    if not isinstance(part, dict):
        raise CalendarResponseError("event without a time")
    zone_name = part.get("timeZone") if isinstance(part.get("timeZone"), str) else None
    if isinstance(part.get("date"), str):  # all-day: a date in the event's (or the calendar's) zone
        try:
            day = date.fromisoformat(part["date"])
        except ValueError:
            raise CalendarResponseError("bad date") from None
        local = datetime.combine(day, time(0, 0), tzinfo=_zone(zone_name, fallback))
        return local.astimezone(timezone.utc), True, zone_name
    moment = _datetime(part.get("dateTime"))
    if moment is None:
        raise CalendarResponseError("bad time")
    return moment, False, zone_name


def _attendee(raw: Any) -> CalendarEventAttendee | None:
    if not isinstance(raw, dict) or not isinstance(raw.get("email"), str):
        return None
    return CalendarEventAttendee(
        email=clean(raw["email"], 254), name=clean(raw.get("displayName"), 100),
        response_status=raw.get("responseStatus") if isinstance(raw.get("responseStatus"), str) else "needsAction",
        organizer=bool(raw.get("organizer")), self_=bool(raw.get("self")), optional=bool(raw.get("optional")),
    )


def _meeting_link(raw: dict[str, Any]) -> str | None:
    candidates = [raw.get("hangoutLink")]
    conference = raw.get("conferenceData")
    if isinstance(conference, dict):
        for entry in conference.get("entryPoints") or []:
            if isinstance(entry, dict) and entry.get("entryPointType") == "video":
                candidates.append(entry.get("uri"))
    for link in candidates:
        if isinstance(link, str) and re.match(r"^https://[^\s<>\"']{4,480}$", link):
            return link
    return None  # never report a non-https or odd link


def parse_calendar(raw: Any) -> CalendarInfo:
    if not isinstance(raw, dict) or not isinstance(raw.get("id"), str) or not raw["id"]:
        raise CalendarResponseError("calendar without an id")
    return CalendarInfo(
        calendar_id=raw["id"], summary=clean(raw.get("summaryOverride") or raw.get("summary"), 200),
        description=clean(raw.get("description"), 500),
        timezone=raw.get("timeZone") if isinstance(raw.get("timeZone"), str) else None,
        primary=bool(raw.get("primary")), selected=bool(raw.get("selected", True)),
        access_role=raw.get("accessRole") if isinstance(raw.get("accessRole"), str) else "reader",
    )


def parse_event(raw: Any, calendar_id: str, fallback_zone: ZoneInfo) -> CalendarEvent:
    """Normalize one `events` resource. Raises CalendarResponseError if it has no id or no usable time."""
    if not isinstance(raw, dict) or not isinstance(raw.get("id"), str) or not raw["id"]:
        raise CalendarResponseError("event without an id")
    start, all_day, zone_name = _when(raw.get("start"), fallback_zone)
    end, end_all_day, _ = _when(raw.get("end"), fallback_zone)
    if end < start:  # malformed: never invent a duration, keep it as an instant
        end = start
    attendees = [a for a in (_attendee(x) for x in (raw.get("attendees") or [])[:MAX_ATTENDEES]) if a is not None]
    reminders = []
    overrides = (raw.get("reminders") or {}).get("overrides") if isinstance(raw.get("reminders"), dict) else None
    for item in overrides or []:
        if isinstance(item, dict) and isinstance(item.get("minutes"), int) and item["minutes"] >= 0:
            reminders.append(CalendarEventReminder(method=str(item.get("method") or "popup")[:10], minutes=item["minutes"]))
    organizer = _attendee({**raw["organizer"], "organizer": True}) if isinstance(raw.get("organizer"), dict) else None
    recurrence = [clean(r, 300) for r in (raw.get("recurrence") or []) if isinstance(r, str)][:5]
    return CalendarEvent(
        event_id=raw["id"], calendar_id=calendar_id, etag=raw.get("etag") if isinstance(raw.get("etag"), str) else None,
        summary=clean(raw.get("summary"), 300), description=clean(raw.get("description"), 2000),
        location=clean(raw.get("location"), 300), start=start, end=end, timezone=zone_name, all_day=all_day and end_all_day,
        organizer=organizer, attendees=attendees, reminders=reminders, meeting_link=_meeting_link(raw), recurrence=recurrence,
        recurring_event_id=raw.get("recurringEventId") if isinstance(raw.get("recurringEventId"), str) else None,
        status=raw.get("status") if isinstance(raw.get("status"), str) else "confirmed",
        busy=raw.get("transparency") != "transparent",
        created=_datetime(raw.get("created")), updated=_datetime(raw.get("updated")),
        html_link=raw.get("htmlLink") if isinstance(raw.get("htmlLink"), str) and raw["htmlLink"].startswith("https://") else None,
    )
