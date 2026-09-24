"""Natural-language time and recurrence parsing, and timezone handling. No database."""

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from agent.tasks.formatting import format_recurrence, format_when, local_day_bounds
from agent.tasks.models import Frequency, Recurrence
from agent.tasks.timeparse import TimeParseError, TimeParser, extract_time_of_day
from agent.tasks.zone import resolve_timezone
from tests.task_helpers import IST, ist

NOW = ist(2030, 3, 4, 14, 30)  # Monday
parser = TimeParser(IST)


@pytest.mark.parametrize(
    "phrase, expected",
    [
        ("tomorrow at 9 AM", ist(2030, 3, 5, 9)),
        ("Tomorrow at 9 a.m.", ist(2030, 3, 5, 9)),
        ("in 30 minutes", ist(2030, 3, 4, 15, 0)),
        ("in 2 hours", ist(2030, 3, 4, 16, 30)),
        ("today at 6 PM", ist(2030, 3, 4, 18)),
        ("next Monday at 8 AM", ist(2030, 3, 11, 8)),   # today is Monday: "next" is never today
        ("Monday at 8 am", ist(2030, 3, 11, 8)),         # this Monday 8 AM already passed today
        ("Monday at 5 pm", ist(2030, 3, 4, 17)),         # ... but 5 PM has not
        ("friday at 3pm", ist(2030, 3, 8, 15)),
        ("on Sunday at 10:15 am", ist(2030, 3, 10, 10, 15)),
        ("at 5 pm", ist(2030, 3, 4, 17)),                # a bare time is the next such time
        ("at 9 am", ist(2030, 3, 5, 9)),                 # 9 AM has passed today: tomorrow
        ("17:30", ist(2030, 3, 4, 17, 30)),
        ("day after tomorrow at noon", ist(2030, 3, 6, 12)),
        ("tonight at 8", ist(2030, 3, 4, 20)),
        ("March 10 at 3 pm", ist(2030, 3, 10, 15)),
        ("2030-03-10 15:00", ist(2030, 3, 10, 15)),
    ],
)
def test_natural_language_times(phrase, expected):
    parsed = parser.parse(phrase, NOW)
    assert parsed is not None and parsed.has_time
    assert parsed.value == expected
    assert parsed.value.tzinfo is not None  # always an explicit timezone-aware datetime


def test_date_only_phrases_report_that_no_time_was_given():
    parsed = parser.parse("tomorrow", NOW)
    assert parsed.has_time is False and parsed.value.date() == ist(2030, 3, 5).date()


@pytest.mark.parametrize("phrase", ["", "   ", "submit my assignment", "whenever", "tomorrow morning", "x" * 500])
def test_unparseable_phrases_return_none_and_never_invent_a_time(phrase):
    assert parser.parse(phrase, NOW) is None


@pytest.mark.parametrize("phrase", ["at 8", "tomorrow at 9", "Monday at 7"])
def test_ambiguous_am_pm_asks_instead_of_guessing(phrase):
    with pytest.raises(TimeParseError, match="AM or"):
        parser.parse(phrase, NOW)


def test_parsing_respects_the_configured_timezone_not_utc():
    new_york = TimeParser(ZoneInfo("America/New_York"))
    now = datetime(2030, 3, 4, 14, 0, tzinfo=timezone.utc)  # 09:00 in New York (EST)
    parsed = new_york.parse("today at 6 PM", now)
    assert parsed.value.utcoffset().total_seconds() == -5 * 3600
    assert parsed.value.astimezone(timezone.utc) == datetime(2030, 3, 4, 23, 0, tzinfo=timezone.utc)
    same_instant_in_ist = parser.parse("today at 6 PM", now)  # 19:30 in Kolkata: still "today"
    assert same_instant_in_ist.value.astimezone(timezone.utc) == datetime(2030, 3, 4, 12, 30, tzinfo=timezone.utc)


def test_relative_times_are_measured_from_now_regardless_of_zone():
    for zone in (IST, ZoneInfo("America/New_York"), ZoneInfo("UTC")):
        parsed = TimeParser(zone).parse("in 30 minutes", NOW)
        assert (parsed.value - NOW).total_seconds() == 1800


def test_wall_clock_times_follow_daylight_saving():
    new_york = TimeParser(ZoneInfo("America/New_York"))
    now = datetime(2030, 3, 9, 15, 0, tzinfo=timezone.utc)  # the day before clocks go forward
    parsed = new_york.parse("tomorrow at 9 AM", now)
    assert parsed.value.astimezone(timezone.utc) == datetime(2030, 3, 10, 13, 0, tzinfo=timezone.utc)  # EDT, not EST


@pytest.mark.parametrize(
    "phrase, expected",
    [
        ("every day at 8 AM", Recurrence(frequency=Frequency.DAILY, hour=8)),
        ("daily at 6:45 pm", Recurrence(frequency=Frequency.DAILY, hour=18, minute=45)),
        ("Every Monday at 8 AM", Recurrence(frequency=Frequency.WEEKLY, hour=8, weekdays=(0,))),
        ("every monday and friday at 6 pm", Recurrence(frequency=Frequency.WEEKLY, hour=18, weekdays=(0, 4))),
        ("every weekday at 7:30 am", Recurrence(frequency=Frequency.WEEKLY, hour=7, minute=30, weekdays=(0, 1, 2, 3, 4))),
        ("every sun at 10 am", Recurrence(frequency=Frequency.WEEKLY, hour=10, weekdays=(6,))),
        ("every month on the first day at 10 AM", Recurrence(frequency=Frequency.MONTHLY, hour=10, day_of_month=1)),
        ("monthly on the 15th at 9 am", Recurrence(frequency=Frequency.MONTHLY, hour=9, day_of_month=15)),
        ("every month on the last day at 5 pm", Recurrence(frequency=Frequency.MONTHLY, hour=17, day_of_month=31)),
    ],
)
def test_recurrence_phrases_become_structured_recurrences(phrase, expected):
    assert parser.parse_recurrence(phrase) == expected


@pytest.mark.parametrize(
    "phrase, question",
    [
        ("every monday", "What time"),
        ("every day", "What time"),
        ("daily at 8", "AM or 8 PM"),
        ("every month at 9 am", "Which day of the month"),
        ("weekly at 9 am", "Which day of the week"),
    ],
)
def test_incomplete_recurrences_ask_a_question(phrase, question):
    with pytest.raises(TimeParseError, match=question):
        parser.parse_recurrence(phrase)


@pytest.mark.parametrize("phrase", ["tomorrow at 9 am", "call mom", "", "in 30 minutes"])
def test_non_recurring_phrases_are_not_recurrences(phrase):
    assert parser.parse_recurrence(phrase) is None


def test_extract_time_of_day():
    clock_time, rest = extract_time_of_day("9 AM assignment")
    assert clock_time == (9, 0) and rest.strip() == "assignment"
    assert extract_time_of_day("assignment")[0] is None
    assert extract_time_of_day("at 8")[0] is None  # ambiguous: not guessed


def test_formatting_is_speakable():
    assert format_when(ist(2030, 3, 4, 20), NOW, IST) == "today at 8:00 PM"
    assert format_when(ist(2030, 3, 5, 9), NOW, IST) == "tomorrow at 9:00 AM"
    assert format_when(ist(2030, 3, 8, 15, 5), NOW, IST) == "Friday at 3:05 PM"
    assert format_when(ist(2030, 4, 2, 0, 0), NOW, IST) == "April 2nd at 12:00 AM"
    assert format_recurrence(Recurrence(frequency=Frequency.WEEKLY, hour=8, weekdays=(0,))) == "every Monday at 8:00 AM"
    assert format_recurrence(Recurrence(frequency=Frequency.MONTHLY, hour=10, day_of_month=1)) == "every month on the 1st at 10:00 AM"
    start, end = local_day_bounds(0, NOW, IST)
    assert (start, end) == (ist(2030, 3, 4), ist(2030, 3, 5))


def test_timezone_setting_resolution(monkeypatch):
    assert resolve_timezone("Europe/London").key == "Europe/London"
    with pytest.raises(Exception):
        resolve_timezone("Not/AZone")

    import tzlocal

    monkeypatch.setattr(tzlocal, "get_localzone_name", lambda: "Asia/Tokyo")
    assert resolve_timezone("").key == "Asia/Tokyo"  # empty = the system zone, detected once and made explicit

    def broken():
        raise RuntimeError("no zone")

    monkeypatch.setattr(tzlocal, "get_localzone_name", broken)
    assert resolve_timezone("").key == "UTC"  # documented fallback


def test_settings_validate_the_timezone_and_document_defaults():
    from backend.core.config import Settings

    base = {"DATABASE_URL": "postgresql+psycopg2://u:p@localhost/x"}
    defaults = Settings(_env_file=None, **base)
    assert defaults.JARVIS_TIMEZONE == "" and defaults.JARVIS_REMINDER_POLL_SECONDS == 15.0
    assert defaults.JARVIS_MISSED_REMINDER_POLICY == "notify" and defaults.JARVIS_DEFAULT_TASK_PRIORITY == "medium"
    assert defaults.JARVIS_TASKS_ENABLED and defaults.JARVIS_REMINDERS_ENABLED
    assert Settings(_env_file=None, JARVIS_TIMEZONE="Asia/Kolkata", **base).JARVIS_TIMEZONE == "Asia/Kolkata"
    for bad in ({"JARVIS_TIMEZONE": "Mars/Base"}, {"JARVIS_MISSED_REMINDER_POLICY": "ignore"},
                {"JARVIS_REMINDER_POLL_SECONDS": 0}, {"JARVIS_DEFAULT_TASK_PRIORITY": "urgent"}):
        with pytest.raises(ValueError):
            Settings(_env_file=None, **base, **bad)
