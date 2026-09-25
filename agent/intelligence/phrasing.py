"""Small, deterministic wording helpers shared by findings, briefings and plans, so dates and lists are said the same way everywhere."""

from datetime import date, datetime
from zoneinfo import ZoneInfo


def local(moment: datetime, zone: ZoneInfo) -> datetime:
    return moment.astimezone(zone)


def clock(moment: datetime, zone: ZoneInfo) -> str:
    m = moment.astimezone(zone)
    hour = m.hour % 12 or 12
    suffix = "AM" if m.hour < 12 else "PM"
    return f"{hour} {suffix}" if m.minute == 0 else f"{hour}:{m.minute:02d} {suffix}"


def day_word(moment: datetime | date, now: datetime, zone: ZoneInfo) -> str:
    """today / tomorrow / yesterday / Friday (within the coming week) / Friday, Sep 26 (further away or past)."""
    target = moment.astimezone(zone).date() if isinstance(moment, datetime) else moment
    today = now.astimezone(zone).date()
    delta = (target - today).days
    if delta == 0:
        return "today"
    if delta == 1:
        return "tomorrow"
    if delta == -1:
        return "yesterday"
    if 1 < delta <= 6:
        return target.strftime("%A")
    return f"{target.strftime('%A')}, {target.strftime('%b')} {target.day}"


def when_phrase(moment: datetime, now: datetime, zone: ZoneInfo, all_day: bool = False) -> str:
    """'tomorrow at 11 AM', 'Friday' (all-day), 'Monday, Sep 28 at 10 AM'."""
    day = day_word(moment, now, zone)
    return day if all_day else f"{day} at {clock(moment, zone)}"


def join_and(items: list[str]) -> str:
    items = [i for i in items if i]
    if not items:
        return ""
    if len(items) == 1:
        return items[0]
    return ", ".join(items[:-1]) + f" and {items[-1]}"


def quoted(title: str, limit: int = 70) -> str:
    title = " ".join(title.split()).rstrip(".")
    return "'" + (title if len(title) <= limit else title[: limit - 1] + "…") + "'"


STATUS_WORDS = {"pending": "pending", "in_progress": "in progress", "overdue": "overdue", "completed": "completed", "cancelled": "cancelled"}


def status_word(status: str) -> str:
    return STATUS_WORDS.get(status, status.replace("_", " "))
