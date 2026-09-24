"""PriorityAnalyzer: transparent, deterministic ordering of briefing items. No language model takes part.

Each item gets an internal integer score from three explainable parts, and the score maps to a PriorityLevel:

  what the source says   an explicit priority (task/event: low 0, medium 10, high 20, critical 30); with none, a fixed default
                         by kind (interview, exam, deadline, assignment, application 20; meeting, appointment, calendar
                         event, reminder 10; email needing action 12, important email 10; message needing action 8)
  how late or close      overdue +25; within three hours +20; today +15; tomorrow +10; within three days +5
  (nothing else)         wording, sender, or anything a model says never adds a point

  score >= 45 CRITICAL, >= 30 HIGH, >= 10 NORMAL, otherwise LOW.

So an overdue CRITICAL task outranks a LOW task due next week, and a HIGH task due tomorrow reaches HIGH. The score is used only
to order items and choose a focus; it is never shown to the user (only the factual reasons are), and it never changes the
underlying task, event or email. The `reasons` returned are what "why is this a priority?" is answered from.
"""

from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

from agent.briefing.models import ItemKind, PriorityLevel
from agent.events.models import EventType
from agent.tasks.models import TaskPriority

_EXPLICIT_POINTS = {TaskPriority.LOW: 0, TaskPriority.MEDIUM: 10, TaskPriority.HIGH: 20, TaskPriority.CRITICAL: 30}
_EXPLICIT_WORDS = {TaskPriority.LOW: "low", TaskPriority.MEDIUM: "medium", TaskPriority.HIGH: "high", TaskPriority.CRITICAL: "critical"}
_KIND_DEFAULT = {
    ItemKind.DEADLINE: 20, ItemKind.EVENT: 10, ItemKind.CALENDAR_EVENT: 10, ItemKind.REMINDER: 10, ItemKind.TASK: 10,
    ItemKind.EMAIL: 10, ItemKind.MESSAGE: 8,
}
_EVENT_TYPE_DEFAULT = {
    EventType.INTERVIEW: 20, EventType.EXAM: 20, EventType.DEADLINE: 20, EventType.ASSIGNMENT: 20, EventType.APPLICATION: 20,
    EventType.MEETING: 10, EventType.APPOINTMENT: 10,
}
CRITICAL_AT, HIGH_AT, NORMAL_AT = 45, 30, 10


@dataclass(frozen=True)
class Facts:
    kind: ItemKind
    when: datetime | None = None
    explicit: TaskPriority | None = None
    event_type: EventType | None = None
    action_email: bool = False  # an email the deterministic classifier says needs action
    all_day: bool = False


@dataclass(frozen=True)
class Assessment:
    level: PriorityLevel
    score: int
    reasons: tuple[str, ...]


def level_for(score: int) -> PriorityLevel:
    if score >= CRITICAL_AT:
        return PriorityLevel.CRITICAL
    if score >= HIGH_AT:
        return PriorityLevel.HIGH
    if score >= NORMAL_AT:
        return PriorityLevel.NORMAL
    return PriorityLevel.LOW


class PriorityAnalyzer:
    def __init__(self, zone: ZoneInfo):
        self._zone = zone

    def assess(self, facts: Facts, now: datetime) -> Assessment:
        reasons: list[str] = []
        if facts.explicit is not None:
            score = _EXPLICIT_POINTS[facts.explicit]
            reasons.append(f"it is marked {_EXPLICIT_WORDS[facts.explicit]} priority")
        elif facts.event_type is not None and facts.event_type in _EVENT_TYPE_DEFAULT:
            score = _EVENT_TYPE_DEFAULT[facts.event_type]
            reasons.append(f"it is {'an' if facts.event_type.value[0] in 'aeiou' else 'a'} {facts.event_type.value}")
        elif facts.kind is ItemKind.EMAIL and facts.action_email:
            score = 12
            reasons.append("an email that JARVIS's rules classify as needing action (a guess, not a fact)")
        else:
            score = _KIND_DEFAULT.get(facts.kind, 10)
        if facts.when is not None:
            score += self._timing(facts, now, reasons)
        return Assessment(level=level_for(score), score=score, reasons=tuple(reasons))

    def _timing(self, facts: Facts, now: datetime, reasons: list[str]) -> int:
        when = facts.when
        assert when is not None
        verb = "starts" if facts.kind in (ItemKind.EVENT, ItemKind.CALENDAR_EVENT) else "is due"
        if facts.kind is ItemKind.REMINDER:
            verb = "is scheduled"
        if when < now and not facts.all_day:
            if facts.kind in (ItemKind.TASK, ItemKind.DEADLINE):
                reasons.append("it is overdue")
                return 25
            return 0  # something that already started is not "more urgent"
        minutes = (when - now).total_seconds() / 60
        days = (when.astimezone(self._zone).date() - now.astimezone(self._zone).date()).days
        if not facts.all_day and minutes <= 180:
            reasons.append(f"it {verb} within three hours")
            return 20
        if days == 0:
            reasons.append(f"it {verb} today")
            return 15
        if days == 1:
            reasons.append(f"it {verb} tomorrow")
            return 10
        if days <= 3:
            reasons.append(f"it {verb} within three days")
            return 5
        return 0
