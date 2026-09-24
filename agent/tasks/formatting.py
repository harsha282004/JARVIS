"""Human-readable (speakable) descriptions of times and recurrences. Pure functions."""

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from agent.tasks.models import Frequency, Recurrence

_DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
_MONTHS = [
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
]


def format_clock(hour: int, minute: int) -> str:
    suffix = "AM" if hour < 12 else "PM"
    return f"{hour % 12 or 12}:{minute:02d} {suffix}"


def _ordinal(n: int) -> str:
    if 10 <= n % 100 <= 20:
        return f"{n}th"
    return f"{n}{ {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th') }"


def format_when(value: datetime, now: datetime, zone: ZoneInfo, with_time: bool = True) -> str:
    """"today at 9:00 AM", "tomorrow at 9:00 AM", "Monday at 8:00 AM", "March 5 at 3:00 PM"."""
    local, local_now = value.astimezone(zone), now.astimezone(zone)
    days = (local.date() - local_now.date()).days
    if days == 0:
        day = "today"
    elif days == 1:
        day = "tomorrow"
    elif days == -1:
        day = "yesterday"
    elif 1 < days < 7:
        day = _DAYS[local.weekday()]
    else:
        day = f"{_MONTHS[local.month - 1]} {_ordinal(local.day)}"
        if local.year != local_now.year:
            day += f", {local.year}"
    return f"{day} at {format_clock(local.hour, local.minute)}" if with_time else day


def format_recurrence(rec: Recurrence) -> str:
    clock = format_clock(rec.hour, rec.minute)
    if rec.frequency is Frequency.DAILY:
        return f"every day at {clock}"
    if rec.frequency is Frequency.WEEKLY:
        if rec.weekdays == (0, 1, 2, 3, 4):
            return f"every weekday at {clock}"
        names = [_DAYS[d] for d in rec.weekdays]
        return f"every {' and '.join(names) if len(names) < 3 else ', '.join(names)} at {clock}"
    return f"every month on the {_ordinal(rec.day_of_month or 1)} at {clock}"


def local_day_bounds(day_offset: int, now: datetime, zone: ZoneInfo) -> tuple[datetime, datetime]:
    """[start, end) of the user's local day `day_offset` days from today, as aware datetimes."""
    local = now.astimezone(zone)
    start = datetime.combine(local.date() + timedelta(days=day_offset), datetime.min.time(), tzinfo=zone)
    end = datetime.combine(local.date() + timedelta(days=day_offset + 1), datetime.min.time(), tzinfo=zone)
    return start, end
