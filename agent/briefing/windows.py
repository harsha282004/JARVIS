"""Briefing windows on the user's local calendar. Reuses the existing timezone helpers (`local_day_bounds`); there is no second
timezone implementation."""

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from agent.briefing.models import BriefingWindow
from agent.tasks.formatting import local_day_bounds


def window_bounds(window: BriefingWindow, now: datetime, zone: ZoneInfo) -> tuple[datetime, datetime]:
    """[start, end) of the window as aware datetimes."""
    if window is BriefingWindow.TODAY:
        return local_day_bounds(0, now, zone)
    if window is BriefingWindow.TOMORROW:
        return local_day_bounds(1, now, zone)
    if window is BriefingWindow.THIS_WEEK:
        days_left = 6 - now.astimezone(zone).weekday()  # until Sunday
        return local_day_bounds(0, now, zone)[0], local_day_bounds(days_left, now, zone)[1]
    if window is BriefingWindow.NEXT_7_DAYS:
        return local_day_bounds(0, now, zone)[0], local_day_bounds(6, now, zone)[1]
    if window is BriefingWindow.YESTERDAY:
        return local_day_bounds(-1, now, zone)
    return now - timedelta(hours=24), now  # LAST_24_HOURS


def horizon_end(end: datetime, now: datetime, zone: ZoneInfo, lookahead_days: int) -> datetime:
    """How far "upcoming" reaches: the later of the window's end and `lookahead_days` days from today."""
    return max(end, local_day_bounds(max(lookahead_days, 1) - 1, now, zone)[1])
