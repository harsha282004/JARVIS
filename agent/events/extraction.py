"""Finding events and deadlines in text: an email, a document chunk, a saved memory or the user's own words.

Deterministic and rule-based (no LLM): a sentence becomes a candidate only if it has BOTH an event cue
("interview", "deadline", "due", "submit", "meeting", ...) AND a date phrase. The date is resolved by the Phase 9
`TimeParser` through `resolve_when`, relative to a reference time (for an email, the day it was sent).
The text is untrusted DATA: it is only pattern-matched. Nothing in it is executed, followed or interpreted as an
instruction, and only a short evidence sentence is kept.

Not guessed: a vague date ("next week", "sometime") or an ambiguous time ("at 8") is returned as `unresolved` with
the question to ask; nothing is stored for it. Dates already in the past are counted as `expired` and skipped.
"""

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from agent.events.dates import ResolvedWhen, WhenKind, event_times, resolve_when
from agent.events.models import DUE_TYPES, EventType, SourceType
from agent.memory.models import Confidence
from agent.tasks.timeparse import TimeParser

MAX_TEXT_CHARS = 20_000
MAX_SENTENCES = 300
MAX_EVIDENCE_CHARS = 300
MAX_CANDIDATES = 10
_EXPIRED_GRACE = timedelta(days=1)

_MONTH = (
    r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|aug(?:ust)?|sep(?:t(?:ember)?)?|"
    r"oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)"
)
_WEEKDAY = r"(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday)"
_DATE_PATTERNS = [
    re.compile(rf"\b{_MONTH}\.?\s+\d{{1,2}}(?:st|nd|rd|th)?(?:,?\s+\d{{4}})?\b", re.I),
    re.compile(rf"\b\d{{1,2}}(?:st|nd|rd|th)?\s+(?:of\s+)?{_MONTH}\.?(?:,?\s+\d{{4}})?\b", re.I),
    re.compile(r"\b\d{4}-\d{2}-\d{2}\b"),
    re.compile(rf"\b(?:(?:next|this|coming)\s+)?{_WEEKDAY}\b", re.I),
    re.compile(r"\b(?:day after tomorrow|today|tomorrow|tonight)\b", re.I),
    re.compile(r"\bin\s+(?:\d+|a|an|one|two|three|four|five|six|seven|ten|fourteen)\s+(?:days?|weeks?)\b", re.I),
    re.compile(r"\b(?:this|next)\s+(?:week|month|weekend)\b|\bsometime\b|\bsoon\b", re.I),
]
_ABSOLUTE = (_DATE_PATTERNS[0], _DATE_PATTERNS[1], _DATE_PATTERNS[2])
_TIME = re.compile(
    r"(?:\bat\s+|@\s*)?(?:\d{1,2}(?::\d{2})?\s*[ap]\.?m\.?|\d{1,2}:\d{2}|\bnoon\b|\bmidnight\b)"
    r"|\bat\s+\d{1,2}\b(?!\s*(?:st|nd|rd|th|days?|weeks?|hours?|minutes?|%))",
    re.I,
)
_DAY_PART = re.compile(r"\b(?:morning|afternoon|evening|night)\b", re.I)
_BOUND_BEFORE = re.compile(r"\b(by|before|until|no later than|on or before)\s*$", re.I)
_SENTENCE = re.compile(r"(?<=[.!?])\s+|\n+")
_PAST = re.compile(r"\b(?:was|were|had been|took place|has passed|ended|happened|sent on|posted on|received on)\b", re.I)

# (type, pattern). First match wins, most specific first.
_TYPE_CUES: list[tuple[EventType, re.Pattern[str]]] = [
    (EventType.INTERVIEW, re.compile(r"\binterview", re.I)),
    (EventType.EXAM, re.compile(r"\b(?:exam|examination|midterm|mid-term|final exam|quiz|viva)\b", re.I)),
    (EventType.ASSIGNMENT, re.compile(r"\b(?:assignment|homework|coursework|lab report)\b", re.I)),
    (EventType.APPLICATION, re.compile(r"\bapplication\b", re.I)),
    (EventType.APPOINTMENT, re.compile(r"\bappointment\b", re.I)),
    (EventType.MEETING, re.compile(r"\b(?:meeting|call|sync|catch[- ]?up|stand-?up|session)\b", re.I)),
    (EventType.EVENT, re.compile(r"\b(?:webinar|workshop|conference|seminar|ceremony|hackathon|orientation|presentation|demo|event)\b", re.I)),
    (EventType.DEADLINE, re.compile(
        r"\b(?:deadline|due|submit|submission|submitted|closes?|closing|last date|registration|must be (?:received|completed)|expires?)\b", re.I)),
]
_DEADLINE_WORDS = _TYPE_CUES[-1][1]
_TRAILING = re.compile(r"\s*\b(?:by|before|on|at|until|is|are|was|be|will|scheduled|for|from|the|a|an|and|due)\s*$", re.I)
_LEADING_ARTICLE = re.compile(r"^(?:your|the|my|our|a|an)\s+", re.I)
_LEADING = re.compile(r"^(?:reminder|note|please note(?: that)?|fyi|important|dear \w+)[:,\-\s]+", re.I)

_TYPE_LABELS = {
    EventType.INTERVIEW: "Interview", EventType.EXAM: "Exam", EventType.ASSIGNMENT: "Assignment deadline",
    EventType.APPLICATION: "Application deadline", EventType.APPOINTMENT: "Appointment", EventType.MEETING: "Meeting",
    EventType.EVENT: "Event", EventType.DEADLINE: "Deadline", EventType.REMINDER: "Reminder", EventType.OTHER: "Event",
}


@dataclass(frozen=True)
class Candidate:
    title: str
    event_type: EventType
    when: ResolvedWhen
    confidence: Confidence
    evidence: str
    phrase: str

    @property
    def is_deadline(self) -> bool:
        return self.event_type in DUE_TYPES


@dataclass(frozen=True)
class Unresolved:
    phrase: str
    question: str
    evidence: str


@dataclass
class ExtractionResult:
    candidates: list[Candidate] = field(default_factory=list)
    unresolved: list[Unresolved] = field(default_factory=list)
    expired: int = 0  # dates already in the past: not imported
    truncated: bool = False


def _clean(text: str) -> str:
    """Untrusted text: control characters and angle brackets removed, whitespace collapsed."""
    return " ".join(re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", " ", text).replace("<", "(").replace(">", ")").split())


def infer_type(text: str) -> EventType | None:
    for event_type, pattern in _TYPE_CUES:
        if pattern.search(text):
            return event_type
    return None


def _title(sentence: str, event_type: EventType, subject: str | None) -> str:
    if subject and _clean(subject):
        return _clean(subject)[:120]
    body = _LEADING.sub("", sentence)
    for pattern in _DATE_PATTERNS[:6]:
        body = pattern.sub(" ", body)
    body = _TIME.sub(" ", body)
    body = " ".join(body.split()).strip(" .:;,-–")
    previous = None
    while previous != body:  # drop trailing connectors left behind by the removed date ("Exam is on" -> "Exam")
        previous = body
        body = _TRAILING.sub("", body).strip(" .:;,-–")
    body = _LEADING_ARTICLE.sub("", body)
    return (body[:1].upper() + body[1:])[:120] if len(body) >= 4 else _TYPE_LABELS[event_type]


def _confidence(*, absolute: bool, has_time: bool, user_stated: bool) -> Confidence:
    """Extraction certainty, not factual certainty: an event cue is always present (1), plus an absolute calendar date,
    a stated time, and the user having said it themselves."""
    score = 1 + int(absolute) + int(has_time) + int(user_stated)
    return Confidence.HIGH if score >= 3 else Confidence.MEDIUM if score == 2 else Confidence.LOW


def extract_events(
    text: str,
    *,
    reference: datetime,
    parser: TimeParser,
    now: datetime | None = None,
    subject: str | None = None,
    user_stated: bool = False,
    max_candidates: int = MAX_CANDIDATES,
) -> ExtractionResult:
    """Candidates found in `text`. `reference` is the moment relative dates are counted from (an email's send time,
    or now); `now` (default: reference) decides what is already in the past."""
    result = ExtractionResult()
    moment = now or reference
    body = _clean(text[:MAX_TEXT_CHARS]) if "\n" not in text else "\n".join(_clean(line) for line in text[:MAX_TEXT_CHARS].split("\n"))
    sentences = [s.strip() for s in _SENTENCE.split(body) if s and s.strip()]
    if len(sentences) > MAX_SENTENCES:
        sentences, result.truncated = sentences[:MAX_SENTENCES], True
    seen: set[tuple[str, str]] = set()

    for sentence in sentences:
        if not 8 <= len(sentence) <= 400 or (_PAST.search(sentence) and not re.search(r"\bwill\b|\bis scheduled\b", sentence, re.I)):
            continue
        event_type = infer_type(sentence)
        if event_type is None:
            continue
        date_match = next((m for p in _DATE_PATTERNS if (m := p.search(sentence)) is not None), None)
        if date_match is None:
            continue
        # A due-by cue makes an interview-less application/assignment/deadline a deadline; keep the specific type.
        time_match = _TIME.search(sentence)
        part_match = _DAY_PART.search(sentence)
        phrase = " ".join(t for t in (
            date_match.group(0), time_match.group(0) if time_match else "", part_match.group(0) if part_match else "") if t)
        bound = _BOUND_BEFORE.search(sentence[: date_match.start()])
        resolved = resolve_when(parser, f"{bound.group(1)} {phrase}" if bound else phrase, reference)
        evidence = sentence[:MAX_EVIDENCE_CHARS]
        if resolved.kind is WhenKind.AMBIGUOUS:
            result.unresolved.append(Unresolved(phrase, resolved.question or "Which date?", evidence))
            continue
        if not resolved.is_resolved or resolved.value is None:
            continue
        times = event_times(resolved, event_type)
        anchor = times.due_at or times.start_at
        assert anchor is not None
        if anchor < moment - _EXPIRED_GRACE:
            result.expired += 1
            continue
        key = (event_type.value, anchor.isoformat())
        if key in seen:
            continue
        seen.add(key)
        absolute = any(p.search(date_match.group(0)) for p in _ABSOLUTE)
        result.candidates.append(Candidate(
            title=_title(sentence, event_type, subject), event_type=event_type, when=resolved,
            confidence=_confidence(absolute=absolute, has_time=resolved.has_time, user_stated=user_stated),
            evidence=evidence, phrase=phrase,
        ))
        if len(result.candidates) >= max_candidates:
            result.truncated = True
            break
    return result
