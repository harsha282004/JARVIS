"""Turning a signal into a short, factual, non-manipulative notification sentence. Deterministic templates: no language
model is involved, so nothing in a title (which can be a stranger's email subject or invitation title) can steer the
wording beyond being quoted, sanitized, inside quotation marks.
"""

import re
from datetime import datetime
from zoneinfo import ZoneInfo

from agent.proactive.models import MAX_MESSAGE_CHARS, NotificationCandidate, ProactiveSignal, SignalType, SourceKind
from agent.tasks.formatting import format_clock, format_when

_UNSAFE = re.compile(r"[\x00-\x1f\x7f<>]")
_MONTHS = ("January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December")
GENERIC_EMAIL_MESSAGE = "An email may need your attention (details are in Gmail)."  # what the history keeps for email signals


def safe_text(text: str, limit: int) -> str:
    """Single line, no control characters or angle brackets, bounded. External text is only ever quoted."""
    return " ".join(_UNSAFE.sub(" ", text).split())[:limit]


def relative_when(target: datetime, now: datetime, zone: ZoneInfo, *, all_day: bool = False) -> str:
    minutes = (target - now).total_seconds() / 60
    if not all_day and -1 < minutes < 120:
        if minutes < 1:
            return "now"
        rounded = round(minutes)
        return f"in about {rounded} minute{'s' if rounded != 1 else ''}"
    return format_when(target, now, zone, with_time=not all_day)


def absolute_when(target: datetime, zone: ZoneInfo, *, all_day: bool = False) -> str:
    """A time that does not change as the day goes on (used in the stored reason)."""
    local = target.astimezone(zone)
    day = f"{_MONTHS[local.month - 1]} {local.day}"
    return day if all_day else f"{day} at {format_clock(local.hour, local.minute)}"


def build_candidate(signal: ProactiveSignal, now: datetime, zone: ZoneInfo) -> NotificationCandidate:
    title = safe_text(signal.title, 80)
    all_day = signal.metadata.get("all_day") == "1"
    target = signal.relevant_at
    when = relative_when(target, now, zone, all_day=all_day) if target is not None else ""
    absolute = absolute_when(target, zone, all_day=all_day) if target is not None else ""
    label = safe_text(signal.metadata.get("label", "event"), 30) or "event"
    sender = safe_text(signal.metadata.get("sender", "someone"), 60) or "someone"
    other = safe_text(signal.metadata.get("other", ""), 80)
    kind = signal.signal_type

    if kind is SignalType.TASK_DUE:
        message, reason = f"Your task '{title}' is due {when}.", f"your task '{title}' is due {absolute}"
    elif kind is SignalType.TASK_OVERDUE:
        message, reason = f"Your task '{title}' is overdue. It was due {when}.", f"your task '{title}' was due {absolute} and is still open"
    elif kind is SignalType.EVENT_APPROACHING and signal.source_type is SourceKind.CALENDAR:
        message, reason = f"Your calendar event '{title}' starts {when}.", f"the Google Calendar event '{title}' starts {absolute}"
    elif kind is SignalType.EVENT_APPROACHING:
        message, reason = f"Your {label} '{title}' starts {when}.", f"your {label} '{title}' starts {absolute}"
    elif kind is SignalType.DEADLINE_APPROACHING:
        lead = "Your deadline" if label in ("deadline", "other", "event") else f"Your {label}"
        message, reason = (
            (f"{lead} '{title}' is {when}." if lead == "Your deadline" else f"{lead} '{title}' is due {when}."),
            f"your {label} '{title}' is due {absolute}",
        )
    elif kind is SignalType.DEADLINE_OVERDUE:
        message, reason = f"Your deadline '{title}' has passed. It was due {when}.", f"your deadline '{title}' was due {absolute} and is not marked done"
    elif kind is SignalType.CALENDAR_CONFLICT:
        message, reason = f"Two calendar events overlap {when}: '{title}' and '{other}'.", f"the calendar events '{title}' and '{other}' overlap around {absolute}"
    elif kind is SignalType.ACTION_REQUIRED_EMAIL:
        subject = safe_text(signal.description, 80)
        message = f"An email from {sender} may need your attention: '{subject}'."
        reason = "an unread email was classified as needing action by JARVIS's own rules (a guess, not a fact)"
    else:  # IMPORTANT_EMAIL
        subject = safe_text(signal.description, 80)
        message = f"An email from {sender} looks important: '{subject}'."
        reason = "an unread email is marked important in Gmail"
    message = " ".join(message.split())[:MAX_MESSAGE_CHARS]
    return NotificationCandidate(
        signal_id=signal.signal_id, message=message, reason=reason[:300], priority=signal.priority, urgency=signal.urgency,
        created_at=now, expires_at=signal.expires_at,
    )
