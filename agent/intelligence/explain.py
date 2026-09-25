"""Explanations: "why are you telling me this?", "why did you schedule that?", "where did you get that?".

JARVIS keeps the evidence behind the last things it said (a finding, a plan block, an answer). An explanation is those recorded
statements in plain words, with their sources. It is never a reconstruction of hidden reasoning: if there is no recorded evidence, JARVIS
says so instead of inventing a reason.
"""

from collections import deque
from dataclasses import dataclass, field
from datetime import datetime

from agent.intelligence.models import Certainty, Provenance, Statement, utcnow
from agent.intelligence.phrasing import join_and
from agent.intelligence.textnorm import tokens

NOTHING_TO_EXPLAIN = "I don't have anything recent to explain. Ask me again about what you mean and I'll tell you where it came from."


@dataclass(frozen=True)
class Explained:
    kind: str  # "finding" | "plan_block" | "answer" | "proposal"
    subject: str  # what was said, in a few words
    evidence: tuple[Statement, ...]
    created_at: datetime = field(default_factory=utcnow)
    lead: str = "I told you that"  # "I suggested this"


class ExplanationLog:
    def __init__(self, capacity: int = 40):
        self._items: deque[Explained] = deque(maxlen=capacity)

    def record(self, kind: str, subject: str, evidence: tuple[Statement, ...], lead: str = "I told you that") -> None:
        if evidence:
            self._items.append(Explained(kind, subject[:120], evidence, lead=lead))

    def recent(self) -> list[Explained]:
        return list(self._items)

    def find(self, query: str | None = None, kinds: tuple[str, ...] | None = None) -> Explained | None:
        """The most relevant recent item: the newest whose subject shares words with `query`, otherwise simply the newest."""
        pool = [i for i in reversed(self._items) if kinds is None or i.kind in kinds]
        if not pool:
            return None
        if query:
            q = tokens(query)
            best = max(pool, key=lambda i: (len(q & tokens(i.subject)), 0), default=None)
            if best is not None and q & tokens(best.subject):
                return best
        return pool[0]

    # ---- rendering -----------------------------------------------------------------------------------------------------

    @staticmethod
    def why(item: Explained | None) -> str:
        if item is None:
            return NOTHING_TO_EXPLAIN
        reasons = [s.text.rstrip(".") for s in item.evidence if s.kind in (Certainty.FACT, Certainty.EXTRACTED, Certainty.INFERENCE)]
        if not reasons:
            return NOTHING_TO_EXPLAIN
        text = f"{item.lead} because " + _because(reasons)
        inferred = [s for s in item.evidence if s.kind is Certainty.INFERENCE]
        if inferred:
            text += " Part of that is my inference from the evidence, not something a source stated."
        return text + " " + ExplanationLog.sources(item)

    @staticmethod
    def sources(item: Explained | None) -> str:
        if item is None:
            return NOTHING_TO_EXPLAIN
        seen: list[str] = []
        for s in item.evidence:
            for p in s.provenance:
                d = p.describe()
                if d not in seen:
                    seen.append(d)
        return ("That came from " + join_and(seen[:5]) + ".") if seen else "I don't have a recorded source for that."


def _because(reasons: list[str]) -> str:
    """Joins evidence sentences into one sentence, only lower-casing a leading ordinary word (never a name or acronym)."""
    parts = [r[:1].lower() + r[1:] if r[:1].isupper() and not r[:2].isupper() and not r.startswith("I ") else r for r in reasons[:4]]
    return join_and(parts) + "."
