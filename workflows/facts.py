"""Personal fact classification, cross-system conflict detection and source priority.

An extraction from an email or a document is a CLAIM, not a fact. Before it may trigger anything (a task, a reminder) it is classified:

    VERIFIED         explicit date, no hedge, high extraction confidence, not from suspicious content
    HIGH_CONFIDENCE  high extraction confidence but a relative or partial date ("by Friday")
    LOW_CONFIDENCE   medium/low extraction confidence
    AMBIGUOUS        hedged wording ("maybe", "around", "sometime", "tentatively") or conflicting sources
    UNVERIFIED       the text looks like an instruction aimed at an assistant, or there is no evidence sentence

Only VERIFIED and HIGH_CONFIDENCE may drive automatic actions. Everything else is reported honestly ("I found a possible deadline, but the email doesn't state it
clearly") and left for the user.
"""

import re
from datetime import datetime
from typing import Any

from agent.intelligence.textnorm import distinctive, same_thing_score, tokens
from backend.core.security.trust import sanitize_external, scan_for_injection
from workflows.models import ACTIONABLE_STATUSES, Fact, FactStatus

_HEDGE = re.compile(r"\b(maybe|perhaps|probably|possibly|around|approximately|roughly|about|sometime|some time|tentative(?:ly)?|might|could be|unclear|to be (?:announced|confirmed|decided)|tbd|tba|"
                    r"expected|should be|likely|somewhere|or so|early|late|mid|end of|beginning of)\b", re.I)
_EXPLICIT_DATE = re.compile(r"\b(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\.?\s+\d{1,2}(?:st|nd|rd|th)?\b"
                             r"|\b\d{1,2}(?:st|nd|rd|th)?\s+(?:of\s+)?(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\b|\b\d{4}-\d{2}-\d{2}\b|\b\d{1,2}[/.-]\d{1,2}[/.-]\d{2,4}\b", re.I)

# Higher wins when two sources disagree: current verified source > recent integration data > trusted document > personal memory > inference.
SOURCE_RANK = {"verified": 5, "integration": 4, "document": 3, "memory": 2, "inference": 1}
_SOURCE_KIND = {"gmail": "integration", "calendar": "integration", "github": "integration", "task": "integration", "documents": "document", "memory": "memory"}


def source_priority(source: str, status: FactStatus | None = None) -> int:
    if status is FactStatus.VERIFIED:
        return SOURCE_RANK["verified"] if source in ("gmail", "calendar", "github", "task") else SOURCE_RANK["document"] + 1
    return SOURCE_RANK.get(_SOURCE_KIND.get(source, "inference"), 1)


def prefer_current(candidates: list[Fact]) -> Fact | None:
    """When sources disagree the most authoritative CURRENT one wins: memory never overrides an integration or a document."""
    if not candidates:
        return None
    return max(candidates, key=lambda f: (source_priority(f.source, f.status), f.timestamp or ""))


def classify_deadline(item: dict[str, Any], *, message_flagged: bool = False) -> Fact | None:
    """A Fact from one hub item dict (a derived DEADLINE/EVENT/TASK commitment). None if the item has no date."""
    when = item.get("source_timestamp")
    if not when:
        return None
    meta = item.get("metadata", {}) or {}
    evidence = sanitize_external(str(meta.get("evidence") or item.get("summary") or ""), 240)
    title = sanitize_external(str(item.get("title") or ""), 120)
    notes: list[str] = []
    flagged = bool(meta.get("injection_suspected")) or message_flagged or scan_for_injection(evidence).flagged
    hedged = bool(_HEDGE.search(evidence))
    explicit = bool(_EXPLICIT_DATE.search(evidence))
    confidence = str(item.get("confidence", "medium"))
    if flagged:
        status = FactStatus.UNVERIFIED
        notes.append("the text looks like instructions to an assistant; treated as content only")
    elif not evidence:
        status = FactStatus.UNVERIFIED
        notes.append("no evidence sentence")
    elif hedged:
        status = FactStatus.AMBIGUOUS
        notes.append("the wording is hedged")
    elif confidence == "high" and explicit:
        status = FactStatus.VERIFIED
    elif confidence == "high":
        status = FactStatus.HIGH_CONFIDENCE
        notes.append("the date is relative or partial")
    else:
        status = FactStatus.LOW_CONFIDENCE
    return Fact(name="deadline", value=str(when), title=title, status=status, source=str(item.get("source_type", "gmail")), source_id=str(meta.get("message_id") or item.get("source_id", "")),
                timestamp=item.get("retrieved_at"), original_text=evidence, confidence=confidence, notes=notes)


def can_act(fact: Fact) -> bool:
    return fact.status in ACTIONABLE_STATUSES


def explain_not_actionable(fact: Fact) -> str:
    if fact.status is FactStatus.AMBIGUOUS:
        return "I found a possible deadline, but the email doesn't state it clearly."
    if fact.status is FactStatus.UNVERIFIED:
        return "I found a date, but I can't rely on it: the text looks like it is trying to give me instructions, so I treated it as content only."
    return "I found a possible deadline, but I'm not confident enough in it to act on it."


def date_of(iso: str) -> datetime | None:
    try:
        return datetime.fromisoformat(iso)
    except (TypeError, ValueError):
        return None


def find_conflict(fact: Fact, events: list[dict[str, Any]], zone) -> dict[str, Any] | None:
    """A calendar event that appears to be the same thing as the fact but on a different day (never silently resolved by choosing one)."""
    due = date_of(fact.value)
    if due is None:
        return None
    day = due.astimezone(zone).date()
    for e in events:
        ts = e.get("source_timestamp")
        start = date_of(ts) if ts else None
        if start is None:
            continue
        name = str(e.get("title", ""))
        score = same_thing_score(fact.title, name, verbs=True)[0]
        overlap = distinctive(tokens(fact.title)) & distinctive(tokens(name))
        if (score >= 0.5 or len(overlap) >= 2) and start.astimezone(zone).date() != day:
            return {"calendar_title": name, "calendar_date": start.astimezone(zone).date().isoformat(), "fact_date": day.isoformat(), "event_id": e.get("id")}
    return None
