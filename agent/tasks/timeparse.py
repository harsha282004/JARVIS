"""Natural-language date/time and recurrence parsing.

`dateparser` (a maintained library) handles relative offsets ("in 30 minutes")
and explicit dates ("March 5 at 3 pm"). It is unreliable for weekday names and
bare times of day ("next Monday at 8 AM" returns nothing, "at 5 pm" can land on
tomorrow), so a small deterministic layer handles today/tomorrow, weekdays and
times of day. Everything returns an explicit timezone-aware datetime in the
user's timezone. Nothing is guessed: an unclear phrase returns None, and an
ambiguous one (a bare "at 8", a recurrence without a time) raises
TimeParseError with a question to ask the user.
"""

import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from agent.tasks.models import Frequency, Recurrence
from backend.core.logging import get_logger

logger = get_logger(__name__)

WEEKDAYS = {
    "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3, "friday": 4, "saturday": 5, "sunday": 6,
}
_ABBREVIATIONS = {
    "mon": 0, "tue": 1, "tues": 1, "wed": 2, "thu": 3, "thur": 3, "thurs": 3, "fri": 4, "sat": 5, "sun": 6,
}
_ORDINAL_WORDS = {
    "first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5, "sixth": 6, "seventh": 7, "eighth": 8,
    "ninth": 9, "tenth": 10, "fifteenth": 15, "twentieth": 20, "last": 31,
}

_WEEKDAY_NAMES = "|".join(sorted(WEEKDAYS, key=len, reverse=True))
_TIME_12H = re.compile(r"\b(\d{1,2})(?::(\d{2}))?\s*([ap])\.?\s?m\b\.?")
_TIME_24H = re.compile(r"\b([01]?\d|2[0-3]):([0-5]\d)\b")
_NOON = re.compile(r"\b(noon|midnight)\b")
_BARE_AT = re.compile(r"(?:\bat|@)\s*(\d{1,2})\b(?!\s*(?:st|nd|rd|th|minutes?|mins?|hours?|hrs?|days?|weeks?|months?))")
_RELATIVE = re.compile(
    r"\b(?:in|after)\s+(?:an?|\d+|half an?|one|two|three|four|five|ten|fifteen|twenty|thirty)\s*"
    r"(?:seconds?|secs?|minutes?|mins?|hours?|hrs?|days?|weeks?)\b|\bfrom now\b"
)
_RELATIVE_TIMED = re.compile(r"\b(?:seconds?|secs?|minutes?|mins?|hours?|hrs?)\b")
_FILLER = re.compile(r"\b(at|around|by|on|the|of|@)\b")


class TimeParseError(Exception):
    """A time phrase that needs a follow-up question. The message is safe to say to the user."""


@dataclass(frozen=True)
class ParsedTime:
    value: datetime  # aware, in the user's timezone
    has_time: bool  # False when only a date was given (time is 00:00 and must be chosen by the caller)


def _normalize(text: str) -> str:
    return " ".join(text.lower().replace(",", " ").split()).strip(" .!?")


def _find_time(text: str) -> tuple[tuple[int, int] | None, str]:
    """Extract one time of day from `text`. Returns ((hour, minute) | None, text without the time).

    Raises TimeParseError for a bare hour ("at 8") whose AM/PM is unknown.
    """
    if (m := _TIME_12H.search(text)) is not None:
        hour, minute = int(m.group(1)), int(m.group(2) or 0)
        if not 1 <= hour <= 12 or minute > 59:
            raise TimeParseError("That time doesn't look right. What time did you mean?")
        hour = hour % 12 + (12 if m.group(3) == "p" else 0)
        return (hour, minute), text[: m.start()] + " " + text[m.end():]
    if (m := _TIME_24H.search(text)) is not None:
        return (int(m.group(1)), int(m.group(2))), text[: m.start()] + " " + text[m.end():]
    if (m := _NOON.search(text)) is not None:
        return ((12, 0) if m.group(1) == "noon" else (0, 0)), text[: m.start()] + " " + text[m.end():]
    if (m := _BARE_AT.search(text)) is not None:
        hour = int(m.group(1))
        if hour == 0 or 13 <= hour <= 23:
            return (hour, 0), text[: m.start()] + " " + text[m.end():]
        raise TimeParseError(f"Did you mean {hour} AM or {hour} PM?")
    return None, text


class TimeParser:
    def __init__(self, zone: ZoneInfo):
        self._zone = zone

    @property
    def zone(self) -> ZoneInfo:
        return self._zone

    # ---- one-off times ---------------------------------------------------

    def parse(self, text: str, now: datetime) -> ParsedTime | None:
        """Parse a one-off date/time relative to `now`. None if the phrase is not understood."""
        phrase = _normalize(text)
        if not phrase or len(phrase) > 100:
            return None
        local_now = now.astimezone(self._zone)

        if _RELATIVE.search(phrase):
            return self._with_dateparser(phrase, local_now)

        if (m := re.match(r"^(?:on\s+)?(day after tomorrow|today|tomorrow|tonight)\b(.*)$", phrase)) is not None:
            offset = {"today": 0, "tonight": 0, "tomorrow": 1, "day after tomorrow": 2}[m.group(1)]
            body = m.group(2)
            if m.group(1) == "tonight":  # "tonight at 8" can only mean the evening
                body = re.sub(r"(\bat\s*(?:[1-9]|1[0-2]))\b(?!\s*[:ap])", r"\1 pm", body)
            time_of_day, rest = _find_time(body)
            if _FILLER.sub(" ", rest).strip():
                return None
            return self._build(local_now.date() + timedelta(days=offset), time_of_day)

        if (m := re.match(rf"^(?:on\s+)?(?:(next|this|coming)\s+)?({_WEEKDAY_NAMES})\b(.*)$", phrase)) is not None:
            time_of_day, rest = _find_time(m.group(3))
            if _FILLER.sub(" ", rest).strip():
                return None
            return self._weekday(WEEKDAYS[m.group(2)], m.group(1), time_of_day, local_now)

        time_of_day, rest = _find_time(phrase)
        if time_of_day is not None and not _FILLER.sub(" ", rest).strip():
            candidate = self._build(local_now.date(), time_of_day)
            if candidate.value <= local_now:
                candidate = self._build(local_now.date() + timedelta(days=1), time_of_day)
            return candidate

        return self._with_dateparser(phrase, local_now)

    def _build(self, day: date, time_of_day: tuple[int, int] | None) -> ParsedTime:
        hour, minute = time_of_day or (0, 0)
        return ParsedTime(datetime.combine(day, time(hour, minute), tzinfo=self._zone), time_of_day is not None)

    def _weekday(
        self, target: int, qualifier: str | None, time_of_day: tuple[int, int] | None, local_now: datetime
    ) -> ParsedTime:
        delta = (target - local_now.weekday()) % 7
        if qualifier == "next":
            delta = delta or 7  # "next Monday" is never today
        elif delta == 0 and time_of_day is not None and self._build(local_now.date(), time_of_day).value <= local_now:
            delta = 7  # this Monday's time has already passed today
        return self._build(local_now.date() + timedelta(days=delta), time_of_day)

    def _with_dateparser(self, phrase: str, local_now: datetime) -> ParsedTime | None:
        import dateparser  # imported lazily: it is slow to import and only needed here

        try:
            result = dateparser.parse(
                phrase,
                languages=["en"],
                settings={
                    "TIMEZONE": self._zone.key,
                    "RETURN_AS_TIMEZONE_AWARE": True,
                    "PREFER_DATES_FROM": "future",
                    "RELATIVE_BASE": local_now.replace(tzinfo=None),
                },
            )
        except Exception as exc:  # noqa: BLE001 - dateparser raises assorted errors for odd input
            logger.warning("Date parsing failed (%s)", type(exc).__name__)
            return None
        if result is None:
            return None
        result = result.astimezone(self._zone).replace(microsecond=0)
        relative = _RELATIVE.search(phrase) is not None
        has_time = (
            (relative and _RELATIVE_TIMED.search(phrase) is not None)
            or _TIME_12H.search(phrase) is not None
            or _TIME_24H.search(phrase) is not None
            or _NOON.search(phrase) is not None
        )
        if relative and not has_time:  # "in 2 days": keep the current time of day
            return ParsedTime(result, True)
        if not has_time:
            result = result.replace(hour=0, minute=0, second=0)
        elif not relative:
            result = result.replace(second=0)
        return ParsedTime(result, has_time)

    # ---- recurrence --------------------------------------------------------

    def parse_recurrence(self, text: str) -> Recurrence | None:
        """Parse "every day at 8 AM", "every Monday at 9", "every month on the first day at 10 AM".

        None if the phrase is not a recurrence. Raises TimeParseError when it is one
        but lacks a time (or a day of the month): a schedule is never invented.
        """
        phrase = _normalize(text)
        if not phrase or len(phrase) > 150:
            return None
        daily = re.search(r"\b(every\s*day|each day|daily)\b", phrase) is not None
        weekday_words = self._recurrence_weekdays(phrase)
        monthly = re.search(r"\b(every|each) month\b|\bmonthly\b", phrase) is not None
        weekly = re.search(r"\b(every|each) week\b|\bweekly\b", phrase) is not None

        if not (daily or weekday_words or monthly or weekly):
            return None
        time_of_day, rest = _find_time(phrase)
        if time_of_day is None:
            raise TimeParseError("What time should it repeat?")
        hour, minute = time_of_day

        if monthly:
            day = self._day_of_month(rest)
            if day is None:
                raise TimeParseError("Which day of the month should it repeat on?")
            return Recurrence(frequency=Frequency.MONTHLY, hour=hour, minute=minute, day_of_month=day)
        if weekday_words:
            return Recurrence(frequency=Frequency.WEEKLY, hour=hour, minute=minute, weekdays=tuple(sorted(weekday_words)))
        if weekly:
            raise TimeParseError("Which day of the week should it repeat on?")
        return Recurrence(frequency=Frequency.DAILY, hour=hour, minute=minute)

    @staticmethod
    def _recurrence_weekdays(phrase: str) -> set[int]:
        if re.search(r"\bweekdays?\b", phrase):
            return {0, 1, 2, 3, 4}
        if re.search(r"\bweekends?\b", phrase):
            return {5, 6}
        found = {WEEKDAYS[m.group(1)] for m in re.finditer(rf"\b({_WEEKDAY_NAMES})s?\b", phrase)}  # "monday", "mondays"
        if re.search(r"\b(every|each)\b", phrase):
            for m in re.finditer(r"\b(mon|tues?|wed|thu(?:rs?)?|fri|sat|sun)\b", phrase):
                found.add(_ABBREVIATIONS[m.group(1)])
        return found

    @staticmethod
    def _day_of_month(text: str) -> int | None:
        if (m := re.search(r"\b(\d{1,2})(?:st|nd|rd|th)?\b", text)) is not None and 1 <= int(m.group(1)) <= 31:
            return int(m.group(1))
        for word, value in _ORDINAL_WORDS.items():
            if re.search(rf"\b{word}\b", text):
                return value
        return None


def extract_time_of_day(text: str) -> tuple[tuple[int, int] | None, str]:
    """Pull a clock time ("9 AM", "17:30") out of free text. Returns ((hour, minute) | None, text without it).
    An ambiguous bare hour ("at 8") is left in place and reported as no time."""
    try:
        return _find_time(_normalize(text))
    except TimeParseError:
        return None, text
