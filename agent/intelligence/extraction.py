"""Finding events, deadlines and tasks in text (an email, a stored memory, a document chunk), with provenance.

Rule-based, no language model. The text is UNTRUSTED DATA: it is only pattern-matched, never followed. Every result records the
sentence it came from (sanitized, short) and how sure the extraction is, and content that looks like an injection attempt is
flagged so its findings are held at lower confidence and are never turned into tasks automatically.

What is found:
  * EVENT     something that happens at a time ("Your JARVIS project review is scheduled for tomorrow at 11 AM")
  * TASK      something the reader is asked to do, with a date ("Please submit the final report by Friday")
  * DEADLINE  a date something closes or is due, with no actionable verb ("Registration closes October 3")

A sentence "before the review" with no date of its own is tied to the event of that name found in the same text, so the deadline
is the start of that event. Vague dates ("next week") are returned as unresolved with the question to ask; nothing is guessed.
"""

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from zoneinfo import ZoneInfo

from agent.events.dates import ResolvedWhen, WhenKind, event_times, resolve_when
from agent.events.extraction import _ABSOLUTE, _BOUND_BEFORE, _DATE_PATTERNS, _DAY_PART, _SENTENCE, _TIME
from agent.events.models import DUE_TYPES, EventType
from agent.intelligence.deadlines import DeadlineKind, classify_deadline_kind
from agent.intelligence.models import Provenance, SourceKind, utcnow
from agent.intelligence.textnorm import ACTION_VERBS, STOP
from agent.memory.models import Confidence
from agent.tasks.timeparse import TimeParser
from backend.core.security.trust import scan_for_injection, sanitize_external

MAX_TEXT_CHARS = 20_000
MAX_SENTENCES = 300
MAX_EVIDENCE = 240
MAX_RESULTS = 12
_EXPIRED_GRACE = timedelta(days=1)


class CommitmentKind(StrEnum):
    EVENT = "event"
    TASK = "task"
    DEADLINE = "deadline"


@dataclass(frozen=True)
class Commitment:
    kind: CommitmentKind
    title: str
    when: datetime | None  # aware, user's zone; None only while a relative deadline waits for its event
    all_day: bool
    confidence: Confidence
    evidence: str
    source: Provenance
    event_type: EventType | None = None
    deadline_kind: DeadlineKind | None = None
    project_hint: str | None = None
    is_bound: bool = False
    relative_to: str | None = None  # "review": the deadline is the start of the event with this noun
    flagged: bool = False  # the source text contained an injection attempt

    @property
    def key(self) -> str:
        day = self.when.date().isoformat() if self.when else (self.relative_to or "")
        return f"{self.kind.value}|{' '.join(sorted(_key_tokens(self.title)))}|{day}"


def _key_tokens(title: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", title.lower()) if w not in STOP}


@dataclass(frozen=True)
class Unresolved:
    phrase: str
    question: str
    evidence: str


@dataclass
class ExtractionOutcome:
    commitments: list[Commitment] = field(default_factory=list)
    unresolved: list[Unresolved] = field(default_factory=list)
    expired: int = 0
    flagged: bool = False
    injection_reasons: tuple[str, ...] = ()


# what happens at a time -> (EventType, canonical noun)
_EVENT_NOUNS: list[tuple[re.Pattern[str], EventType]] = [
    (re.compile(r"\binterview\b", re.I), EventType.INTERVIEW),
    (re.compile(r"\b(?:exam|examination|midterm|viva|quiz|test)\b", re.I), EventType.EXAM),
    (re.compile(r"\b(?:project review|design review|code review|review|defen[cs]e|presentation|demo|hackathon|workshop|webinar|conference|lecture|seminar|class)\b", re.I), EventType.EVENT),
    (re.compile(r"\b(?:meeting|call|sync|catch[- ]?up|stand-?up|session|appointment)\b", re.I), EventType.MEETING),
]
_EVENT_NOUN_WORDS = re.compile(
    r"\b(interview|exam|examination|midterm|viva|quiz|project review|design review|code review|review|defen[cs]e|presentation|demo|"
    r"hackathon|workshop|webinar|conference|lecture|seminar|class|meeting|call|sync|catch[- ]?up|stand-?up|session|appointment)\b", re.I)
_DEADLINE_CUE = re.compile(
    r"\b(?:submit|submission|deadline|due|hand[- ]?in|upload|register|registration|apply|application|closes?|closing|last date|"
    r"turn in|send (?:in|us|me)|complete|finish)\b", re.I)
_TASK_VERBS = (
    "submit", "send", "complete", "finish", "upload", "register", "apply", "prepare", "fill", "sign", "pay", "review", "reply",
    "respond", "confirm", "provide", "bring", "email", "update", "return", "book", "attend", "read", "write", "finalize", "finalise",
)
_TASK = re.compile(
    r"(?:\b(?:please|kindly|make sure (?:you |to )|remember to|don'?t forget to|you (?:need|have) to|you must|you should|"
    r"we need you to|need to|must|should)\s+)?"
    rf"\b(?P<verb>{'|'.join(_TASK_VERBS)})\s+(?P<obj>[^.,;!?\n]{{3,90}}?)\s*"
    r"(?=\b(?:by|before|until|on or before|no later than|on|at|this|next|tomorrow|today|tonight|in|within)\b|[.,;!?]|$)",
    re.I,
)
_BEFORE_EVENT = re.compile(r"\b(?:before|prior to|ahead of|by the time of)\s+(?:the|your|our|this)\s+(?P<noun>[a-z][a-z \-]{2,30}?)(?=\s*(?:[.,;!?]|$|\band\b|\bstarts?\b))", re.I)
_PROJECT_BEFORE = re.compile(r"\b((?:[A-Z][A-Za-z0-9\-]*)(?:\s+[A-Z][A-Za-z0-9\-]*){0,2})\s+[Pp]roject\b")
_PROJECT_AFTER = re.compile(r"\b[Pp]roject\s+([A-Z][A-Za-z0-9\-]{2,})\b")
_NAME_STOPWORDS = {"your", "the", "my", "our", "a", "an", "this", "that", "final", "new", "next", "last", "team", "class", "group", "term", "semester", "year"}
_TITLE_LEFT_STOP = STOP | {"is", "are", "was", "will", "be", "have", "has", "not", "no", "yes", "also", "still", "just", "only", "kindly", "reminder", "hi", "hello", "dear", "thanks", "thank"}
_PAST = re.compile(r"\b(?:was|were|had been|took place|has passed|ended|happened)\b", re.I)


def detect_project(text: str, known: tuple[str, ...] = ()) -> str | None:
    """A project name mentioned in `text`: a known project (from the user's own data) if one appears, otherwise the capitalized
    words directly before or after the word "project"."""
    for name in known:
        if re.search(rf"(?<![A-Za-z0-9]){re.escape(name)}(?![A-Za-z0-9])", text, re.I):
            return name
    for pattern in (_PROJECT_BEFORE, _PROJECT_AFTER):
        for match in pattern.finditer(text):
            words = [w for w in match.group(1).split() if w.lower() not in _NAME_STOPWORDS or w.isupper()]
            while words and (words[0].lower() in _NAME_STOPWORDS or words[0].lower() in ACTION_VERBS or words[0].lower() in STOP):
                words.pop(0)  # a sentence-initial verb ("Prepare project review") is not a project name
            name = " ".join(words)
            if len(name) >= 3 and name.lower() not in _NAME_STOPWORDS:
                return name
    return None


def event_title(sentence: str, noun_match: re.Match[str]) -> str:
    """The event's name: the event noun with the up-to-three name words directly before it ("JARVIS project review")."""
    left = sentence[: noun_match.start()].split()
    picked: list[str] = []
    for word in reversed(left):
        bare = re.sub(r"[^A-Za-z0-9\-]", "", word)
        if not bare or bare.lower() in _TITLE_LEFT_STOP or len(picked) >= 3 or word.endswith((".", ",", ";", ":")):
            break
        picked.append(bare)
    name = " ".join([*reversed(picked), noun_match.group(0)])
    return (name[:1].upper() + name[1:])[:120]


def _task_title(verb: str, obj: str) -> str:
    obj = re.sub(r"\s+", " ", obj).strip(" .,:;-")
    obj = re.sub(r"^(?:the|your|our|a|an)\s+(?=\S)", lambda m: m.group(0), obj, flags=re.I)
    title = f"{verb.capitalize()} {obj}"
    return title[:120]


class TextExtractor:
    def __init__(self, zone: ZoneInfo, clock=utcnow, known_projects: tuple[str, ...] = ()):
        self._zone = zone
        self._parser = TimeParser(zone)
        self._clock = clock
        self._known = known_projects

    def extract(
        self,
        text: str,
        *,
        source_type: SourceKind,
        source_id: str,
        label: str,
        source_timestamp: datetime | None,
        subject: str | None = None,
        user_stated: bool = False,
    ) -> ExtractionOutcome:
        outcome = ExtractionOutcome()
        scan = scan_for_injection(f"{subject or ''}\n{text[:MAX_TEXT_CHARS]}")
        outcome.flagged, outcome.injection_reasons = scan.flagged, scan.reasons
        now = self._clock()
        reference = (source_timestamp or now).astimezone(self._zone)
        body = "\n".join(sanitize_external(line, 600) for line in text[:MAX_TEXT_CHARS].split("\n"))
        sentences = [s.strip() for s in _SENTENCE.split(body) if s and s.strip()][:MAX_SENTENCES]
        project = detect_project(f"{subject or ''}. {body}", self._known)
        events: list[Commitment] = []
        pending_relative: list[tuple[str, re.Match[str], str, str | None]] = []
        seen: set[str] = set()

        def prov(evidence: str, confidence: Confidence) -> Provenance:
            return Provenance(source_type, source_id, label, source_timestamp, now, confidence, None, evidence)

        for sentence in sentences:
            if not 8 <= len(sentence) <= 400 or (_PAST.search(sentence) and not re.search(r"\bwill\b|\bis scheduled\b", sentence, re.I)):
                continue
            noun = _EVENT_NOUN_WORDS.search(sentence)
            cue = _DEADLINE_CUE.search(sentence)
            date_match = next((m for p in _DATE_PATTERNS if (m := p.search(sentence)) is not None), None)
            evidence = sentence[:MAX_EVIDENCE]
            task_match = _TASK.search(sentence) if cue or date_match or _BEFORE_EVENT.search(sentence) else None
            is_task = task_match is not None and self._asks_reader(sentence, task_match)

            if date_match is None:
                # "Please submit the documentation before the review": the date is the review's own start time.
                before = _BEFORE_EVENT.search(sentence)
                if before and is_task:
                    pending_relative.append((sentence, task_match, before.group("noun").strip().lower(), project))  # type: ignore[arg-type]
                continue

            time_match = _TIME.search(sentence)
            part_match = _DAY_PART.search(sentence)
            phrase = " ".join(t for t in (date_match.group(0), time_match.group(0) if time_match else "", part_match.group(0) if part_match else "") if t)
            bound = _BOUND_BEFORE.search(sentence[: date_match.start()])
            resolved = resolve_when(self._parser, f"{bound.group(1)} {phrase}" if bound else phrase, reference)
            if resolved.kind is WhenKind.AMBIGUOUS:
                outcome.unresolved.append(Unresolved(phrase, resolved.question or "Which date?", evidence))
                continue
            if not resolved.is_resolved or resolved.value is None:
                continue

            absolute = any(p.search(date_match.group(0)) for p in _ABSOLUTE)
            confidence = self._confidence(absolute, resolved, bool(cue or noun), user_stated, outcome.flagged)

            if is_task and cue:
                due = self._due(resolved)
                anchor = due
                if anchor < now - _EXPIRED_GRACE:
                    outcome.expired += 1
                    continue
                title = _task_title(task_match.group("verb"), task_match.group("obj"))  # type: ignore[union-attr]
                c = Commitment(CommitmentKind.TASK, title, due, resolved.kind is WhenKind.DATE_ONLY, confidence, evidence,
                               prov(evidence, confidence), None, classify_deadline_kind(sentence), project, bool(resolved.bound), None, outcome.flagged)
            elif noun and not (cue and re.search(r"\b(?:due|deadline|closes?|closing|last date)\b", sentence, re.I)):
                match = _EVENT_NOUN_WORDS.search(sentence)
                assert match is not None
                event_type = next(t for p, t in _EVENT_NOUNS if p.search(match.group(0)))
                times = event_times(resolved, event_type)
                anchor_dt = times.due_at or times.start_at
                assert anchor_dt is not None
                if anchor_dt < now - _EXPIRED_GRACE:
                    outcome.expired += 1
                    continue
                title = event_title(sentence, match)
                c = Commitment(CommitmentKind.EVENT, title, times.start_at or times.due_at, times.all_day, confidence, evidence,
                               prov(evidence, confidence), event_type, DeadlineKind.EVENT_DATE, project, False, None, outcome.flagged)
            elif cue:
                due = self._due(resolved)
                if due < now - _EXPIRED_GRACE:
                    outcome.expired += 1
                    continue
                title = self._deadline_title(sentence, date_match)
                kind = classify_deadline_kind(sentence)
                c = Commitment(CommitmentKind.DEADLINE, title, due, resolved.kind is WhenKind.DATE_ONLY, confidence, evidence,
                               prov(evidence, confidence), None, kind, project, bool(resolved.bound), None, outcome.flagged)
            else:
                continue
            if c.key in seen:
                continue
            seen.add(c.key)
            outcome.commitments.append(c)
            if c.kind is CommitmentKind.EVENT:
                events.append(c)
            if len(outcome.commitments) >= MAX_RESULTS:
                break

        for sentence, task_match, noun_word, proj in pending_relative:
            c = self._relative_task(sentence, task_match, noun_word, proj, events, prov, outcome.flagged)
            if c is not None and c.key not in seen:
                seen.add(c.key)
                outcome.commitments.append(c)
        return outcome

    # ---- helpers -----------------------------------------------------------------------------------------------------

    @staticmethod
    def _asks_reader(sentence: str, match: re.Match[str]) -> bool:
        """The sentence asks someone to do the thing (imperative or "you need to"), not "the report was submitted"."""
        lead = sentence[: match.start()].lower().strip()
        if lead.endswith(("was", "were", "been", "be", "is", "are", "has", "have", "had", "we", "i", "they", "he", "she", "will")) and not lead.endswith("you"):
            return False
        return True

    def _due(self, resolved: ResolvedWhen) -> datetime:
        value = resolved.value
        assert value is not None
        if resolved.kind is WhenKind.DATE_ONLY:
            return value.replace(hour=23, minute=59, second=0, microsecond=0)  # a date alone means the end of that day
        return value

    @staticmethod
    def _deadline_title(sentence: str, date_match: re.Match[str]) -> str:
        body = sentence[: date_match.start()] + " " + sentence[date_match.end():]
        body = _TIME.sub(" ", body)
        body = re.sub(r"\b(?:by|before|until|on|at|is|are|will|be)\b\s*$", "", " ".join(body.split()).strip(" .:;,-"), flags=re.I).strip(" .:;,-")
        body = re.sub(r"^(?:please|kindly|reminder|note)[:,\s]+", "", body, flags=re.I)
        return (body[:1].upper() + body[1:])[:120] if len(body) >= 4 else "Deadline"

    @staticmethod
    def _confidence(absolute: bool, resolved: ResolvedWhen, has_cue: bool, user_stated: bool, flagged: bool) -> Confidence:
        score = int(has_cue) + 1 + int(absolute) + int(resolved.has_time) + int(user_stated)  # the +1: the date resolved unambiguously
        level = Confidence.HIGH if score >= 3 else Confidence.MEDIUM if score == 2 else Confidence.LOW
        if flagged and level > Confidence.LOW:
            level = Confidence(level - 1)  # an injection-flagged source is never trusted as much
        return level

    def _relative_task(self, sentence, task_match, noun_word, project, events, prov, flagged) -> Commitment | None:
        """A task due 'before the review': resolved against the event of that name found in the same text."""
        noun_tokens = _key_tokens(noun_word)
        for event in events:
            if event.when is not None and noun_tokens & _key_tokens(event.title):
                title = _task_title(task_match.group("verb"), task_match.group("obj"))
                evidence = sentence[:MAX_EVIDENCE]
                confidence = Confidence.MEDIUM if flagged else Confidence.HIGH
                return Commitment(CommitmentKind.TASK, title, event.when, False, confidence, evidence,
                                  prov(evidence, confidence), None, classify_deadline_kind(sentence), project or event.project_hint, True, noun_word, flagged)
        return None
