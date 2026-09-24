"""Resolving the date/time phrases found in events, on top of the Phase 9 `TimeParser` (not a second parser).

`resolve_when` adds only what event text needs and the reminder parser deliberately does not accept:
  - leading bounds ("by Friday", "before September 30", "until Monday");
  - part-of-day words ("tomorrow afternoon", and "tomorrow afternoon at 3" means 3 PM);
  - vagueness is reported, never guessed ("next week", "sometime", "October", "the 15th").
Result kinds: EXACT (date and time), DATE_ONLY (a day, time unknown), AMBIGUOUS (a question to ask),
UNRESOLVED (not understood). Every value is an aware datetime in the user's timezone.
"""

import re
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from enum import StrEnum

from agent.events.models import DUE_TYPES, EventType
from agent.tasks.timeparse import TimeParseError, TimeParser

_BOUND = re.compile(r"^(?:no later than|on or before|before|by|until|till|due on|due by|due)\s+", re.I)
_DAY_PART = re.compile(r"\b(morning|afternoon|evening|night)\b", re.I)
_VAGUE = re.compile(
    r"\b(?:this|next|last|the coming|the following)\s+(?:week|month|year|weekend|quarter|semester)\b"
    r"|\b(?:soon|sometime|someday|later|eventually|shortly|asap|end of (?:the )?(?:week|month|year))\b|\bweekends?\b",
    re.I,
)
_MONTHS = re.compile(
    r"\b(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|aug(?:ust)?|sep(?:t(?:ember)?)?|"
    r"oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\b",
    re.I,
)
_ORDINAL_ONLY = re.compile(r"^(?:the\s+)?\d{1,2}(?:st|nd|rd|th)$", re.I)
_BARE_HOUR = re.compile(r"(\bat\s*)(\d{1,2})(:\d{2})?\b(?!\s*[:ap.])(?!\s*(?:st|nd|rd|th|days?|weeks?|hours?|minutes?))", re.I)
_PM_PARTS = {"afternoon", "evening", "night"}
_DAYS_AHEAD = re.compile(r"^in\s+\S+\s+(?:days?|weeks?)$", re.I)


class WhenKind(StrEnum):
    EXACT = "exact"
    DATE_ONLY = "date_only"
    AMBIGUOUS = "ambiguous"
    UNRESOLVED = "unresolved"


@dataclass(frozen=True)
class ResolvedWhen:
    kind: WhenKind
    value: datetime | None = None  # aware, user's timezone; for DATE_ONLY the local midnight of that day
    day_part: str | None = None  # "afternoon" etc. when only that was said; the time itself stays unknown
    bound: str | None = None  # "by" / "before" / "until" when the phrase was a limit rather than an instant
    question: str | None = None  # what to ask the user when AMBIGUOUS

    @property
    def is_resolved(self) -> bool:
        return self.kind in (WhenKind.EXACT, WhenKind.DATE_ONLY)

    @property
    def has_time(self) -> bool:
        return self.kind is WhenKind.EXACT


def _ambiguous(question: str, bound: str | None = None) -> ResolvedWhen:
    return ResolvedWhen(WhenKind.AMBIGUOUS, bound=bound, question=question)


def resolve_when(parser: TimeParser, phrase: str, reference: datetime) -> ResolvedWhen:
    """Resolve `phrase` relative to `reference` (now, or the date of the email it came from)."""
    text = " ".join((phrase or "").replace(",", " ").split()).strip(" .!?")
    if not text or len(text) > 120:
        return ResolvedWhen(WhenKind.UNRESOLVED)

    bound: str | None = None
    if (m := _BOUND.match(text)) is not None:
        bound = m.group(0).strip().split()[0].lower()
        text = text[m.end():].strip()
    if _VAGUE.search(text):
        return _ambiguous("Which day do you mean? I need an exact date.", bound)
    if _ORDINAL_ONLY.match(text):
        return _ambiguous("Which month is that?", bound)
    if _MONTHS.search(text) and not re.search(r"\d", text):
        return _ambiguous("Which day of the month?", bound)

    day_part_match = _DAY_PART.search(text)
    day_part = day_part_match.group(1).lower() if day_part_match else None
    if day_part_match:
        text = " ".join((text[: day_part_match.start()] + " " + text[day_part_match.end():]).split())
        if day_part in _PM_PARTS or day_part == "morning":  # "tomorrow afternoon at 3" is 3 PM, "morning at 9" is 9 AM
            suffix = " pm" if day_part in _PM_PARTS else " am"
            text = _BARE_HOUR.sub(lambda m: f"{m.group(1)}{m.group(2)}{m.group(3) or ''}{suffix}", text)
    text = text.strip()
    if not text:
        return ResolvedWhen(WhenKind.UNRESOLVED)

    try:
        parsed = parser.parse(text, reference)
    except TimeParseError as exc:
        return _ambiguous(str(exc), bound)
    if parsed is None:
        return ResolvedWhen(WhenKind.UNRESOLVED, bound=bound)
    if _DAYS_AHEAD.match(text):  # "in two days" names a day, not a time of day
        return ResolvedWhen(WhenKind.DATE_ONLY, parsed.value.replace(hour=0, minute=0, second=0, microsecond=0),
                            day_part=day_part, bound=bound)
    if parsed.has_time:
        return ResolvedWhen(WhenKind.EXACT, parsed.value, bound=bound)
    return ResolvedWhen(WhenKind.DATE_ONLY, parsed.value, day_part=day_part, bound=bound)


@dataclass(frozen=True)
class EventTimes:
    start_at: datetime | None
    end_at: datetime | None
    due_at: datetime | None
    all_day: bool


def event_times(when: ResolvedWhen, event_type: EventType, duration_minutes: int | None = None) -> EventTimes:
    """The stored timestamps for a resolved time. Deadlines use `due_at` (a date without a time means the END of
    that local day); other events use `start_at` (a date without a time is an all-day event: that whole local day)."""
    value = when.value
    assert value is not None and when.is_resolved
    if event_type in DUE_TYPES:
        if when.has_time:
            return EventTimes(None, None, value, False)
        return EventTimes(None, None, datetime.combine(value.date(), time(23, 59), tzinfo=value.tzinfo), True)
    if when.has_time:
        end = value + timedelta(minutes=duration_minutes) if duration_minutes else None
        return EventTimes(value, end, None, False)
    start = datetime.combine(value.date(), time(0, 0), tzinfo=value.tzinfo)
    return EventTimes(start, datetime.combine(value.date() + timedelta(days=1), time(0, 0), tzinfo=value.tzinfo), None, True)
