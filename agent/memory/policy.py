"""When may a memory candidate be saved? Deterministic, replaceable policy.

  REJECT    secrets, oversized/unusable content
  CONFIRM   sensitive content, inferred (guessed) memories, low confidence,
            or everything when auto-save is switched off
  AUTO_SAVE explicit, sufficiently confident, non-sensitive statements
The LLM has no say: this runs on code-produced candidates only.
"""

from dataclasses import dataclass
from enum import StrEnum

from agent.memory.models import Confidence, MemoryBasis, MemoryCandidate
from agent.memory.safety import screen


class MemoryDecision(StrEnum):
    AUTO_SAVE = "auto_save"
    CONFIRM = "confirm"
    REJECT = "reject"


@dataclass(frozen=True)
class PolicyResult:
    decision: MemoryDecision
    reason: str  # a category/code, never the candidate's content


class MemoryPolicy:
    def __init__(self, auto_save: bool = True, min_confidence: Confidence = Confidence.MEDIUM):
        self._auto_save = auto_save
        self._min_confidence = min_confidence

    def evaluate(self, candidate: MemoryCandidate) -> PolicyResult:
        screening = screen(f"{candidate.content} {candidate.slot or ''}")
        if screening.secret:
            return PolicyResult(MemoryDecision.REJECT, f"secret:{screening.secret}")
        if screening.sensitive:
            return PolicyResult(MemoryDecision.CONFIRM, f"sensitive:{screening.sensitive}")
        if candidate.basis is MemoryBasis.INFERRED:
            return PolicyResult(MemoryDecision.CONFIRM, "inferred")
        if candidate.confidence < self._min_confidence:
            return PolicyResult(MemoryDecision.CONFIRM, "low_confidence")
        if not self._auto_save:
            return PolicyResult(MemoryDecision.CONFIRM, "auto_save_disabled")
        return PolicyResult(MemoryDecision.AUTO_SAVE, "explicit_low_risk")
