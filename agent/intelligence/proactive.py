"""Proactive intelligence: turn findings into notifications, through the NotificationCenter and nothing else.

    findings (deterministic, evidence-backed)  ->  level  ->  NotificationCenter (dedupe, cooldown, quiet hours, privacy, preferences)

Only what is worth interrupting for is delivered: IMPORTANT and CRITICAL findings become notifications, lesser ones are stored so
they show up in the next briefing. A notification says what JARVIS found (facts, then an optional suggestion) and never acts; the
evidence is recorded so "why are you telling me this?" has a real answer. The same finding is never announced twice (its key is the
dedupe key), and a finding that changed (its time moved) is announced again.
"""

from agent.intelligence.findings import Finding, FindingKind, Urgency
from agent.intelligence.explain import ExplanationLog
from backend.core.notifications import Level, Notification, NotificationCenter

_LEVEL = {Urgency.CRITICAL: Level.CRITICAL, Urgency.IMPORTANT: Level.IMPORTANT, Urgency.NOTICE: Level.NORMAL, Urgency.INFO: Level.LOW}
# Findings that are answers to a question, not something worth pushing on their own.
_ON_REQUEST_ONLY = frozenset({FindingKind.REMINDER_COLLISION, FindingKind.SUSPICIOUS_CONTENT})


class IntelligenceNotifier:
    def __init__(self, center: NotificationCenter, explanations: ExplanationLog, skip_kinds: frozenset[FindingKind] = frozenset()):
        self._center = center
        self._explanations = explanations
        self._skip = skip_kinds  # kinds another notifier (Phase 14) already covers, so the user is not told twice

    def publish(self, findings: list[Finding]) -> list[Notification]:
        made: list[Notification] = []
        for f in findings:
            if f.kind in self._skip or (f.kind in _ON_REQUEST_ONLY and f.urgency < Urgency.IMPORTANT):
                continue
            if f.kind is FindingKind.TASK_PROPOSAL and f.offer is None:
                continue
            note = self._center.submit(
                dedupe_key=f.key, level=_LEVEL[f.urgency], title=f.title, body=f.spoken(), category=f.category, source="intelligence",
                refs={"finding": f.kind.value, "entities": list(f.entity_ids)[:4]},
            )
            if note is None:
                continue  # exact duplicate: nothing new was said
            made.append(note)
            self._explanations.record("finding", f.title, tuple(f.facts) + ((f.suggestion,) if f.suggestion else ()), lead="I told you that")
        return made
