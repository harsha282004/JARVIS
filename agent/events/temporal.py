"""Bounded temporal reasoning: scopes ("this week"), countdowns ("in 3 days") and status timing.

All arithmetic is on the user's local calendar (their configured timezone), never on UTC days. Nothing here
guesses: an event without a usable time simply has no countdown.
"""

from datetime import datetime, timedelta
from enum import StrEnum
from zoneinfo import ZoneInfo

from agent.events.models import Event
from agent.tasks.formatting import local_day_bounds

DEFAULT_DURATION = timedelta(hours=1)  # only for deciding "is it still going on"; never stored


class EventScope(StrEnum):
    UPCOMING = "upcoming"  # from today for the configured lookahead
    TODAY = "today"
    TOMORROW = "tomorrow"
    THIS_WEEK = "this_week"  # today until the end of Sunday
    NEXT_WEEK = "next_week"  # the following Monday to Sunday
    NEXT_7_DAYS = "next_7_days"
    OVERDUE = "overdue"
    ALL = "all"


def scope_window(scope: EventScope, now: datetime, zone: ZoneInfo, lookahead_days: int) -> tuple[datetime, datetime] | None:
    """[start, end) of the scope on the user's local calendar; None for OVERDUE and ALL (no window)."""
    today_start, _ = local_day_bounds(0, now, zone)
    if scope is EventScope.TODAY:
        return local_day_bounds(0, now, zone)
    if scope is EventScope.TOMORROW:
        return local_day_bounds(1, now, zone)
    if scope is EventScope.THIS_WEEK:
        days_left = 6 - now.astimezone(zone).weekday()  # days until Sunday
        return today_start, local_day_bounds(days_left, now, zone)[1]
    if scope is EventScope.NEXT_WEEK:
        to_monday = 7 - now.astimezone(zone).weekday()
        return local_day_bounds(to_monday, now, zone)[0], local_day_bounds(to_monday + 6, now, zone)[1]
    if scope is EventScope.NEXT_7_DAYS:
        return today_start, local_day_bounds(6, now, zone)[1]
    if scope is EventScope.UPCOMING:
        return today_start, local_day_bounds(max(lookahead_days, 1) - 1, now, zone)[1]
    return None


def effective_end(event: Event, zone: ZoneInfo) -> datetime:
    """When the event is over: its end, its due time, the end of its day (all-day), or start + one hour."""
    if event.due_at is not None and event.start_at is None:
        return event.due_at
    if event.end_at is not None:
        return event.end_at
    start = event.start_at
    assert start is not None
    if event.all_day:
        return local_day_bounds(0, start.astimezone(zone), zone)[1]
    return start + DEFAULT_DURATION


def in_window(event: Event, window: tuple[datetime, datetime], zone: ZoneInfo) -> bool:
    """True if the event starts, is due, or is still going on inside [start, end)."""
    start, end = window
    return event.anchor < end and effective_end(event, zone) >= start


def days_until(target: datetime, now: datetime, zone: ZoneInfo) -> int:
    """Whole local calendar days from today to `target` (0 = today, 1 = tomorrow, negative = past)."""
    return (target.astimezone(zone).date() - now.astimezone(zone).date()).days


def countdown(event: Event, now: datetime, zone: ZoneInfo) -> str:
    """"today", "tomorrow", "in 5 days", "3 days ago", or "in about 2 hours" for something later today."""
    target = event.anchor
    days = days_until(target, now, zone)
    if days == 0:
        delta = target - now
        if abs(delta) < timedelta(minutes=1):
            return "right now"
        hours = round(abs(delta).total_seconds() / 3600)
        if delta > timedelta(0):
            return f"in about {hours} hour{'s' if hours != 1 else ''}" if hours >= 1 else "in less than an hour"
        return "earlier today"
    if days == 1:
        return "tomorrow"
    if days == -1:
        return "yesterday"
    return f"in {days} days" if days > 1 else f"{-days} days ago"
