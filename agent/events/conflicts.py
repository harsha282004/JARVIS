"""Informational conflict detection. It only reports overlaps; it never moves, cancels or changes anything.

Which events can conflict: live events that occupy time, i.e. that have a start (deadlines do not, they
are a point of "due by"; unconfirmed events are ignored). An event with no end is a single instant.
    timed vs timed   OVERLAP  their time ranges intersect (or an instant falls inside a range, or two instants coincide)
    all-day involved ALL_DAY  an all-day event and anything else on that same local day (softer: "same day")
"""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from zoneinfo import ZoneInfo

from agent.events.models import Event, EventStatus
from agent.events.temporal import effective_end
from agent.tasks.formatting import local_day_bounds

_LIVE = frozenset({EventStatus.UPCOMING, EventStatus.ACTIVE})


class ConflictKind(StrEnum):
    OVERLAP = "overlap"
    ALL_DAY = "all_day"


@dataclass(frozen=True)
class Conflict:
    first: Event
    second: Event
    kind: ConflictKind


def _occupies(event: Event) -> bool:
    return event.start_at is not None and event.status in _LIVE


def _span(event: Event, zone: ZoneInfo) -> tuple[datetime, datetime]:
    """[start, end) the event occupies; an instant has end == start."""
    start = event.start_at
    assert start is not None
    if event.all_day:
        return local_day_bounds(0, start.astimezone(zone), zone)
    return start, event.end_at if event.end_at is not None else start


def _ranges_overlap(a: tuple[datetime, datetime], b: tuple[datetime, datetime]) -> bool:
    (s1, e1), (s2, e2) = a, b
    if s1 == e1 and s2 == e2:  # two instants
        return s1 == s2
    if s1 == e1:  # instant vs range
        return s2 <= s1 < e2
    if s2 == e2:
        return s1 <= s2 < e1
    return s1 < e2 and s2 < e1


def conflict_between(a: Event, b: Event, zone: ZoneInfo) -> Conflict | None:
    if a.event_id == b.event_id or not (_occupies(a) and _occupies(b)):
        return None
    first, second = sorted((a, b), key=lambda e: (e.anchor, e.event_id))
    if a.all_day or b.all_day:
        # A timed event counts from its start to its end (an instant: one hour, as elsewhere) against the whole day.
        span_a = _span(a, zone) if a.all_day else (a.start_at, effective_end(a, zone))
        span_b = _span(b, zone) if b.all_day else (b.start_at, effective_end(b, zone))
        return Conflict(first, second, ConflictKind.ALL_DAY) if _ranges_overlap(span_a, span_b) else None  # type: ignore[arg-type]
    return Conflict(first, second, ConflictKind.OVERLAP) if _ranges_overlap(_span(a, zone), _span(b, zone)) else None


def find_conflicts(events: list[Event], zone: ZoneInfo) -> list[Conflict]:
    """Every conflicting pair, in a deterministic order (by start, then id)."""
    candidates = sorted((e for e in events if _occupies(e)), key=lambda e: (e.anchor, e.event_id))
    found: list[Conflict] = []
    for i, a in enumerate(candidates):
        for b in candidates[i + 1 :]:
            conflict = conflict_between(a, b, zone)
            if conflict is not None:
                found.append(conflict)
    return found
