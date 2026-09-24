"""Next-occurrence calculation for structured recurrences.

Occurrences are computed as wall-clock times in the reminder's timezone and
converted to UTC, so "every day at 8 AM" stays at 8 AM across daylight-saving
changes. A wall-clock time that does not exist on a DST-gap day resolves to the
equivalent instant just after the gap (zoneinfo's normal behaviour).
"""

import calendar
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from agent.tasks.models import Frequency, Recurrence

# A monthly recurrence is at most ~31 days apart, so a year of days always contains the next one.
_SEARCH_DAYS = 400


def _matches(recurrence: Recurrence, day: date) -> bool:
    if recurrence.frequency is Frequency.DAILY:
        return True
    if recurrence.frequency is Frequency.WEEKLY:
        return day.weekday() in recurrence.weekdays
    last_day = calendar.monthrange(day.year, day.month)[1]
    return day.day == min(recurrence.day_of_month or 1, last_day)


def next_occurrence(recurrence: Recurrence, after: datetime, zone: ZoneInfo) -> datetime:
    """The first occurrence strictly after `after`, as an aware UTC datetime."""
    if after.tzinfo is None:
        raise ValueError("after must be timezone-aware")
    local_after = after.astimezone(zone)
    day = local_after.date()
    for _ in range(_SEARCH_DAYS):
        if _matches(recurrence, day):
            candidate = datetime.combine(day, time(recurrence.hour, recurrence.minute), tzinfo=zone)
            if candidate.astimezone(timezone.utc) > after.astimezone(timezone.utc):
                return candidate.astimezone(timezone.utc)
        day += timedelta(days=1)
    raise ValueError("no next occurrence found")  # unreachable for valid recurrences
