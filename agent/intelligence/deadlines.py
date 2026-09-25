"""Deadline intelligence: what KIND of date it is, and everything needed to trust it.

An email that says "registration closes Friday" and one that says "the review is Friday" contain two different Fridays. The kind
decides how JARVIS talks about them and when it reminds: an event date is when something happens, a registration/application/
submission deadline is the last moment to act, a preparation deadline is when the user should be ready by, a reminder date is when
to be told. Classification is by transparent wording rules; when nothing decides, the kind is EVENT_DATE for a thing that happens at
a time and SUBMISSION for a thing to hand in, never a guess dressed up as certain (the confidence says so).
"""

import re
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from agent.intelligence.models import Provenance
from agent.memory.models import Confidence


class DeadlineKind(StrEnum):
    EVENT_DATE = "event_date"
    REGISTRATION = "registration_deadline"
    SUBMISSION = "submission_deadline"
    APPLICATION = "application_deadline"
    PREPARATION = "preparation_deadline"
    REMINDER_DATE = "reminder_date"
    RECURRING = "recurring_deadline"


class DeadlineStatus(StrEnum):
    OPEN = "open"
    OVERDUE = "overdue"
    DONE = "done"  # the associated task/event is completed
    UNCONFIRMED = "unconfirmed"  # low-confidence extraction the user has not confirmed


_RULES: list[tuple[DeadlineKind, re.Pattern[str]]] = [
    (DeadlineKind.RECURRING, re.compile(r"\b(?:every|each)\s+(?:day|week|month|monday|tuesday|wednesday|thursday|friday|saturday|sunday)|\b(?:daily|weekly|monthly|fortnightly)\b", re.I)),
    (DeadlineKind.REGISTRATION, re.compile(r"\b(?:regist(?:er|ration)|sign[- ]?up|enrol(?:l|ment)|rsvp)\b", re.I)),
    (DeadlineKind.APPLICATION, re.compile(r"\b(?:apply|application|applications)\b", re.I)),
    (DeadlineKind.SUBMISSION, re.compile(r"\b(?:submit|submission|hand[- ]?in|upload|turn in|send (?:in|us|me)|deliver|due)\b", re.I)),
    (DeadlineKind.PREPARATION, re.compile(r"\b(?:prepar(?:e|ation)|get ready|revise|rehears(?:e|al)|practi[cs]e|study for)\b", re.I)),
    (DeadlineKind.REMINDER_DATE, re.compile(r"\b(?:remind(?:er)?|don'?t forget|remember to)\b", re.I)),
]

_HAPPENS_AT = re.compile(r"\b(?:meeting|interview|exam|review|presentation|demo|viva|defen[cs]e|hackathon|appointment|call|webinar|workshop|conference|lecture|class)\b", re.I)


def classify_deadline_kind(sentence: str) -> DeadlineKind:
    """Most specific rule wins (recurring, registration, application, submission, preparation, reminder); a sentence about
    something that happens at a time, with none of those cues, is an event date."""
    for kind, pattern in _RULES:
        if pattern.search(sentence):
            return kind
    return DeadlineKind.EVENT_DATE if _HAPPENS_AT.search(sentence) else DeadlineKind.SUBMISSION


@dataclass(frozen=True)
class Deadline:
    """A deadline with its full provenance."""

    deadline_id: str
    kind: DeadlineKind
    due_at: datetime  # normalized: aware, in the user's timezone
    timezone: str
    all_day: bool  # only the day was stated; due_at is the end of that day
    original_text: str  # the phrase or sentence it was read from (sanitized, short)
    confidence: Confidence
    source: Provenance
    entity_id: str | None  # the task or event it applies to, when one is known
    status: DeadlineStatus
    is_bound: bool = False  # "by Friday" (last moment) rather than "at Friday 3 PM" (an instant)
    title: str = ""  # what is due ("Submit the documentation")

    def describe_when(self) -> str:
        return format_when(self.due_at, self.all_day)


def format_when(moment: datetime, all_day: bool = False) -> str:
    """'Friday, Sep 26' for a day, 'Friday, Sep 26 at 11:00 AM' for a time."""
    day = f"{moment.strftime('%A')}, {moment.strftime('%b')} {moment.day}"
    if all_day:
        return day
    hour = moment.hour % 12 or 12
    return f"{day} at {hour}:{moment.minute:02d} {'AM' if moment.hour < 12 else 'PM'}"


def status_for(due_at: datetime, now: datetime, *, done: bool = False, confidence: Confidence = Confidence.HIGH) -> DeadlineStatus:
    if done:
        return DeadlineStatus.DONE
    if confidence < Confidence.MEDIUM:
        return DeadlineStatus.UNCONFIRMED
    return DeadlineStatus.OVERDUE if due_at < now else DeadlineStatus.OPEN
