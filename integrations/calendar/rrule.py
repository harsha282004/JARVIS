"""Google Calendar recurrence (RRULE) built from the Phase 9 `Recurrence` model. No custom recurrence engine.

The user's words are understood by the Phase 9 `TimeParser.parse_recurrence` (daily, weekly on given weekdays,
monthly on a day); this module only writes that structured recurrence in the form Google Calendar stores it.
The model can never supply an RRULE: a rule exists only if code built it from a parsed recurrence.
A rule with neither COUNT nor UNTIL repeats forever; callers must say so to the user before creating it.
"""

from datetime import datetime, timezone

from agent.tasks.models import Frequency, Recurrence

_DAYS = ["MO", "TU", "WE", "TH", "FR", "SA", "SU"]
_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
MAX_COUNT = 730


def build_rrule(recurrence: Recurrence, *, count: int | None = None, until: datetime | None = None, all_day: bool = False) -> str:
    """`RRULE:FREQ=WEEKLY;BYDAY=MO` and so on. `count` and `until` are mutually exclusive; `until` is an aware datetime
    (the last moment the series may run); all-day series take a date."""
    if count is not None and until is not None:
        raise ValueError("use count or until, not both")
    parts = [f"FREQ={recurrence.frequency.value.upper()}"]
    if recurrence.frequency is Frequency.WEEKLY:
        parts.append("BYDAY=" + ",".join(_DAYS[d] for d in sorted(recurrence.weekdays)))
    elif recurrence.frequency is Frequency.MONTHLY:
        day = recurrence.day_of_month or 1
        parts.append(f"BYMONTHDAY={-1 if day == 31 else day}")  # "the last day" means the last day of every month
    if count is not None:
        if not 1 <= count <= MAX_COUNT:
            raise ValueError("count out of range")
        parts.append(f"COUNT={count}")
    elif until is not None:
        utc = until.astimezone(timezone.utc)
        parts.append("UNTIL=" + (utc.strftime("%Y%m%d") if all_day else utc.strftime("%Y%m%dT%H%M%SZ")))
    return "RRULE:" + ";".join(parts)


def is_endless(rule: str) -> bool:
    return "COUNT=" not in rule and "UNTIL=" not in rule


def describe_rrule(rule: str) -> str:
    """"weekly on Monday", "every day", "monthly on day 15" — for speaking; unknown rules are called "repeating"."""
    fields = dict(p.split("=", 1) for p in rule.removeprefix("RRULE:").split(";") if "=" in p)
    freq = fields.get("FREQ", "")
    if freq == "DAILY":
        text = "every day"
    elif freq == "WEEKLY":
        days = [_NAMES[_DAYS.index(d)] for d in fields.get("BYDAY", "").split(",") if d in _DAYS]
        text = "every " + " and ".join(days) if days else "every week"
    elif freq == "MONTHLY":
        day = fields.get("BYMONTHDAY", "")
        text = "every month on the last day" if day == "-1" else f"every month on day {day}" if day else "every month"
    else:
        return "repeating"
    if "COUNT" in fields:
        text += f", {fields['COUNT']} times"
    elif "UNTIL" in fields:
        u = fields["UNTIL"]
        text += f", until {u[:4]}-{u[4:6]}-{u[6:8]}"
    return text
