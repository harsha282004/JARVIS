"""Deterministic email analysis for the hub: topic, importance, and the events/deadlines/registrations an email states.

Rule-based and explainable (each result lists the rules that fired); no language model. The email is UNTRUSTED text: it is pattern-matched only, the
injection scanner runs over it, and a flagged email is never trusted more than LOW confidence for what it "asks".

Topics: college, work, internship, hackathon, competition, conference, interview, finance, personal, newsletter, other.
Importance: CRITICAL (interview or deadline/event within 24 h, explicitly urgent), IMPORTANT (needs action, has a date within a week, interview/hackathon/
internship/college with a date), NORMAL, LOW (promotional/newsletter). Notification policy still decides what is announced: most emails are not.
"""

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import IntEnum, StrEnum

from agent.intelligence.extraction import Commitment, CommitmentKind, TextExtractor
from agent.intelligence.models import SourceKind
from backend.core.security.trust import scan_for_injection
from integrations.gmail.intelligence import classify
from integrations.gmail.models import EmailCategory, GmailMessage
from integrations.gmail.text import strip_quoted_reply


class EmailTopic(StrEnum):
    COLLEGE = "college"
    WORK = "work"
    INTERNSHIP = "internship"
    HACKATHON = "hackathon"
    COMPETITION = "competition"
    CONFERENCE = "conference"
    INTERVIEW = "interview"
    FINANCE = "finance"
    PERSONAL = "personal"
    NEWSLETTER = "newsletter"
    OTHER = "other"


class Importance(IntEnum):
    LOW = 1
    NORMAL = 2
    IMPORTANT = 3
    CRITICAL = 4


# Most specific first: the first topic whose pattern matches (subject counts double) wins.
_TOPIC_RULES: list[tuple[EmailTopic, re.Pattern[str]]] = [
    (EmailTopic.INTERVIEW, re.compile(r"\binterview\b", re.I)),
    (EmailTopic.HACKATHON, re.compile(r"\bhackathon|hack ?fest|\bdevfest\b", re.I)),
    (EmailTopic.INTERNSHIP, re.compile(r"\binternship|\bintern\b|summer of code|\bapprentice", re.I)),
    (EmailTopic.COMPETITION, re.compile(r"\bcompetition|\bcontest\b|\bchallenge\b|\bolympiad|\bcapture the flag|\bctf\b", re.I)),
    (EmailTopic.CONFERENCE, re.compile(r"\bconference|\bsummit\b|\bsymposium|\bworkshop\b|\bwebinar|\bmeetup", re.I)),
    (EmailTopic.FINANCE, re.compile(r"\binvoice|\bpayment|\bbank\b|\bstatement\b|\breceipt|\btransaction|\btax\b|\bcredit card|\brefund", re.I)),
    (EmailTopic.COLLEGE, re.compile(r"\bcollege|\buniversity|\bsemester|\bprofessor|\bcourse\b|\bassignment|\bexam\b|\bcampus|\bfaculty|\bsyllabus|\bproject (?:review|submission)|\bviva\b", re.I)),
    (EmailTopic.WORK, re.compile(r"\bjob\b|\bhiring|\brecruit|\boffer letter|\bposition\b|\bjob application|\bpayroll|\bstandup|\bsprint\b|\bclient\b", re.I)),
]
_EDU_SENDER = re.compile(r"\.(?:edu|ac\.[a-z]{2})\b", re.I)
_PROMO = re.compile(r"\b\d{1,3}% off\b|\bunsubscribe\b|\bnewsletter\b|\bdiscount\b|\b(?:big |flash |mega )?sale\b|\bcoupon\b", re.I)
_URGENT = re.compile(r"\b(?:urgent|asap|immediately|time[- ]sensitive|final (?:notice|reminder))\b", re.I)

_NAME = r"(?P<name>(?-i:[A-Z0-9][\w&'’\-]*(?:\s+[A-Z0-9][\w&'’\-]*){0,5}))"
_REGISTRATION = re.compile(r"(?i:registration|registered|registering|sign[- ]?up|enrol(?:l)?ment|application)\s+(?i:for|to|in|at)\s+(?i:the\s+)?" + _NAME
                           + r"(?:\s+(?i:is|has been|was|have been)?\s*(?i:now\s+)?(?P<state>(?i:confirmed|complete|completed|successful|received|approved|accepted)))?")
_REGISTERED_FOR = re.compile(r"(?i:you(?:'ve| have)? (?:are |been )?(?:successfully )?(?:registered|signed up|enrolled)(?: successfully)?\s+(?:for|to|in)\s+(?:the\s+)?)" + _NAME)
_LOCATION = re.compile(r"(?:\b(?i:location|venue|where)\s*[:\-]\s*|\b(?i:will be held|takes place|is held|being held)\s+(?i:at|in)\s+)(?P<loc>[A-Z0-9][^.\n;]{2,70})")
_CONFIRMED = re.compile(r"\b(?:confirmed|complete|completed|successful|successfully|received|approved|accepted)\b", re.I)


@dataclass(frozen=True)
class Registration:
    event_name: str
    state: str  # "completed" | "mentioned"
    evidence: str


@dataclass
class EmailAnalysis:
    topic: EmailTopic
    importance: Importance
    reasons: list[str] = field(default_factory=list)
    category: EmailCategory = EmailCategory.UNKNOWN
    commitments: list[Commitment] = field(default_factory=list)
    registration: Registration | None = None
    location: str | None = None
    flagged: bool = False
    injection_reasons: tuple[str, ...] = ()


def classify_topic(message: GmailMessage, category: EmailCategory | None = None) -> tuple[EmailTopic, str]:
    """(topic, rule). The subject is searched first, then the (unquoted) start of the body."""
    body = strip_quoted_reply(message.plain_text_body or message.snippet)[:2500]
    sender = (message.sender.email if message.sender else "").lower()
    for scope, text in (("subject", message.subject), ("body", body)):
        for topic, pattern in _TOPIC_RULES:
            if pattern.search(text):
                if topic is EmailTopic.COLLEGE and scope == "body" and not (_EDU_SENDER.search(sender) or re.search(r"\b(?:college|university)\b", body, re.I)):
                    continue
                return topic, f"{topic.value} wording in the {scope}"
    if _EDU_SENDER.search(sender):
        return EmailTopic.COLLEGE, "sender is an education domain"
    category = category or classify(message).category
    if category is EmailCategory.PROMOTIONAL:
        return EmailTopic.NEWSLETTER, "promotional/bulk message"
    if category is EmailCategory.PERSONAL:
        return EmailTopic.PERSONAL, "looks like a personal message"
    return EmailTopic.OTHER, "no topic rule matched"


def _registration(text: str) -> Registration | None:
    for pattern in (_REGISTERED_FOR, _REGISTRATION):
        for m in pattern.finditer(text):
            name = " ".join(m.group("name").split()).strip(" .,!")
            if len(name) < 3 or name.lower() in {"the", "your", "our", "this", "that", "a", "an"}:
                continue
            state_word = m.groupdict().get("state")
            sentence_start = max(text.rfind(".", 0, m.start()), text.rfind("\n", 0, m.start())) + 1
            sentence_end = text.find(".", m.end())
            sentence = text[sentence_start: sentence_end if sentence_end != -1 else len(text)][:240].strip()
            completed = bool(state_word) or bool(_CONFIRMED.search(sentence)) or pattern is _REGISTERED_FOR
            return Registration(name, "completed" if completed else "mentioned", sentence)
    return None


def analyze_email(message: GmailMessage, extractor: TextExtractor, now: datetime) -> EmailAnalysis:
    cls = classify(message)
    topic, topic_rule = classify_topic(message, cls.category)
    body = strip_quoted_reply(message.plain_text_body or message.snippet)
    scan = scan_for_injection(f"{message.subject}\n{body[:20_000]}")
    outcome = extractor.extract(
        body, source_type=SourceKind.EMAIL, source_id=message.message_id, label=f"email '{message.subject[:60]}'", source_timestamp=message.timestamp,
        subject=message.subject,
    )
    registration = _registration(f"{message.subject}. {body[:4000]}") if topic in (EmailTopic.HACKATHON, EmailTopic.COMPETITION, EmailTopic.CONFERENCE, EmailTopic.INTERNSHIP, EmailTopic.COLLEGE, EmailTopic.OTHER, EmailTopic.WORK) else None
    loc = _LOCATION.search(body[:4000])
    analysis = EmailAnalysis(topic, Importance.NORMAL, [topic_rule], cls.category, list(outcome.commitments), registration,
                             " ".join(loc.group("loc").split())[:80] if loc else None, scan.flagged or outcome.flagged, scan.reasons)
    analysis.importance, why = _importance(message, analysis, now)
    analysis.reasons.extend(why)
    return analysis


def _importance(message: GmailMessage, a: EmailAnalysis, now: datetime) -> tuple[Importance, list[str]]:
    reasons: list[str] = []
    soonest = min((c.when for c in a.commitments if c.when is not None and c.when >= now - timedelta(hours=2)), default=None)
    within = lambda hours: soonest is not None and soonest - now <= timedelta(hours=hours)  # noqa: E731
    text = f"{message.subject} {message.snippet}"
    if (a.category is EmailCategory.PROMOTIONAL or _PROMO.search(text)) and a.topic in (EmailTopic.OTHER, EmailTopic.NEWSLETTER, EmailTopic.FINANCE) and not a.commitments:
        return Importance.LOW, ["promotional or bulk mail"]
    if a.topic is EmailTopic.NEWSLETTER:
        return Importance.LOW, ["newsletter"]
    level = Importance.NORMAL
    if a.category in (EmailCategory.IMPORTANT, EmailCategory.ACTION_REQUIRED):
        level, reasons = Importance.IMPORTANT, [f"looks like {a.category.value.replace('_', ' ')}"]
    if a.topic is EmailTopic.INTERVIEW:
        level = max(level, Importance.IMPORTANT)
        reasons.append("interview")
    if a.commitments and within(24 * 7):
        level = max(level, Importance.IMPORTANT)
        reasons.append("states a date within a week")
    if a.topic in (EmailTopic.HACKATHON, EmailTopic.INTERNSHIP, EmailTopic.COLLEGE) and a.commitments:
        level = max(level, Importance.IMPORTANT)
        reasons.append(f"{a.topic.value} with a date")
    if a.registration and a.registration.state == "completed":
        level = max(level, Importance.IMPORTANT)
        reasons.append("registration confirmed")
    if (a.topic is EmailTopic.INTERVIEW and within(48)) or (within(24) and any(c.kind in (CommitmentKind.EVENT, CommitmentKind.TASK, CommitmentKind.DEADLINE) for c in a.commitments)) \
            or (_URGENT.search(text) and within(72)):
        level = Importance.CRITICAL
        reasons.append("something is due or happens within 24 hours" if within(24) else "an interview within 48 hours" if a.topic is EmailTopic.INTERVIEW else "marked urgent and dated within 72 hours")
    if a.flagged and level > Importance.NORMAL:
        level = Importance.NORMAL  # a suspicious email never earns a high priority from its own words
        reasons.append("contains instructions aimed at JARVIS, so its priority is capped")
    return level, reasons
