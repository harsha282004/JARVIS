"""Wake policy: what counts as an intentional "Hey JARVIS" / "JARVIS", debounce, and the sleep command. Pure logic (no audio, no models).

    frame score (openWakeWord)  ->  WakeGate.observe()
        strong  (>= direct threshold on >= min_frames consecutive frames)        -> ACCEPT
        candidate (>= threshold, or >= 2 frames above the floor within 5)         -> CANDIDATE: a short local STT check of the last ~2 s must normalise to EXACTLY
                                                                                    "hey jarvis" or "jarvis" -> ACCEPT, anything else -> rejected (nothing is spoken)
        below the candidate floor                                                 -> nothing

Debounce/refractory windows (after an activation, after JARVIS spoke, after a rejection) make one utterance produce at most one activation, and JARVIS's own voice
cannot re-wake it. Audio used for the STT check lives in memory only for that check; nothing is stored, logged or sent (the log records the normalised phrase of a
successful wake and only the length of a rejected transcript).
"""

import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field

VALID_WAKE_PHRASES = frozenset({"hey jarvis", "jarvis"})

_PUNCT = re.compile(r"[^a-z0-9\s]")
_SLEEP_CORE = re.compile(r"^(?:go to sleep|sleep|go to sleep now|sleep now|goodnight|good night)$")


def _norm(text: str) -> str:
    """Lowercase, punctuation removed, whitespace collapsed. "Hey, Jarvis!" -> "hey jarvis"."""
    return re.sub(r"\s+", " ", _PUNCT.sub(" ", (text or "").lower())).strip()


def normalize_wake_phrase(text: str) -> str | None:
    """The canonical wake phrase if `text` is exactly one, else None. No fuzzy matching: "jarvis" and "hey jarvis" only (case, punctuation and spacing ignored)."""
    n = _norm(text)
    return n if n in VALID_WAKE_PHRASES else None


def is_sleep_command(text: str) -> bool:
    """"sleep", "go to sleep", "JARVIS sleep", "Hey JARVIS, sleep", "go to sleep JARVIS" (and nothing longer: "sleep well tonight" is not a command)."""
    n = _norm(text)
    n = re.sub(r"^(?:hey )?jarvis ", "", n)
    n = re.sub(r" jarvis$", "", n)
    return bool(_SLEEP_CORE.match(n))


def wake_plus_sleep(text: str) -> bool:
    """"Hey JARVIS sleep" heard by the wake confirmation: it is not a wake (JARVIS is already waiting), and nothing is spoken."""
    n = _norm(text)
    return bool(re.match(r"^(?:hey )?jarvis (?:go to sleep|sleep)(?: now)?$", n))


@dataclass
class WakeConfig:
    debounce_seconds: float = 1.5            # after an activation, no second one
    direct_threshold: float = 0.85           # sustained scores at/above this count as a strong wake...
    direct_accept: bool = False              # ...but are still checked by STT (False, the strict default): the model also fires on "Okay Jarvis"/"Yes Jarvis"; True = accept at once
    min_frames: int = 2                      # ...for at least this many consecutive 80 ms frames
    candidate_floor: float = 0.3             # weaker sustained scores become STT-checked candidates
    stt_confirm: bool = True                 # candidates need the exact phrase from STT (False: candidates are ignored, only strong wakes count)
    candidate_cooldown_seconds: float = 2.0  # at most one STT check per this window
    post_tts_block_seconds: float = 1.0      # JARVIS's own voice/echo cannot wake it
    confirm_window_seconds: float = 2.4      # audio kept in memory for the STT check
    confirm_tail_seconds: float = 0.8        # extra audio captured after a weak candidate before checking
    strong_tail_seconds: float = 0.3         # ...after a strong wake (the phrase is already complete)
    session_timeout_seconds: float = 120.0   # inactivity that ends a conversation
    sleep_command_enabled: bool = True
    manual_ttl_seconds: float = 5.0          # a tray/API "talk to JARVIS" request older than this is dropped, never replayed later


@dataclass
class WakeDecision:
    action: str                    # none | candidate | accept
    source: str = ""               # model | model_persisted | stt_confirmed
    score: float = 0.0
    reason: str = ""
    strong: bool = False           # a sustained strong score: the phrase is complete, so the STT check needs only a short tail


@dataclass
class WakeEvent:
    """A validated wake. The activation acknowledgement may only follow one of these."""

    source: str                    # model | stt_confirmed | manual | legacy_model
    score: float | None
    threshold: float
    phrase: str
    state_before: str
    stt_confirmed: bool
    debounced_ms: float
    at: float = field(default_factory=time.time)


class WakeGate:
    def __init__(self, config: WakeConfig | None = None, clock: Callable[[], float] = time.monotonic):
        self.cfg = config or WakeConfig()
        self._clock = clock
        self._strong_run = 0
        self._run_frames = 0
        self._run_peak = 0.0
        self._blocked_until = 0.0
        self._candidate_block_until = 0.0
        self.last_reason = ""

    # ---- windows ------------------------------------------------------------------------------------------------------------------

    def block(self, seconds: float) -> None:
        self._blocked_until = max(self._blocked_until, self._clock() + seconds)

    def blocked(self) -> bool:
        return self._clock() < self._blocked_until

    def remaining_block(self) -> float:
        return max(0.0, self._blocked_until - self._clock())

    def note_activation(self) -> None:
        self.block(self.cfg.debounce_seconds)
        self._clear()

    def note_rejection(self) -> None:
        self._candidate_block_until = self._clock() + self.cfg.candidate_cooldown_seconds
        self._clear()

    # ---- per-frame decision -------------------------------------------------------------------------------------------------------

    MAX_RUN_FRAMES = 8   # ~0.6 s: a run of raised scores that never ends is decided anyway

    def observe(self, score: float, threshold: float) -> WakeDecision:
        """One frame's score. A rising score is watched until it either becomes a sustained strong wake (accepted at once) or the run of raised scores ends
        (then it is a candidate for the phrase check if it reached the threshold or lasted two frames). A candidate is never decided on the first raised frame:
        that would pre-empt the direct path with an STT check on a word still being spoken."""
        if self.blocked():
            self._clear()
            return WakeDecision("none", score=score, reason="blocked")
        self._strong_run = self._strong_run + 1 if score >= self.cfg.direct_threshold else 0
        if self._strong_run >= max(1, self.cfg.min_frames):
            self._clear()
            if self.cfg.direct_accept or not self.cfg.stt_confirm:
                return WakeDecision("accept", "model", score, "sustained strong score")
            if self._clock() < self._candidate_block_until:
                return WakeDecision("none", score=score, reason="candidate cooldown")
            return WakeDecision("candidate", "stt_confirmed", score, "strong score: exact phrase check required", strong=True)
        raised = score >= self.cfg.candidate_floor
        if raised:
            self._run_frames += 1
            self._run_peak = max(self._run_peak, score)
            if self._run_frames < self.MAX_RUN_FRAMES:
                return WakeDecision("none", score=score, reason="score rising")
        elif self._run_frames == 0:
            return WakeDecision("none", score=score, reason="below threshold")
        frames, peak = self._run_frames, self._run_peak
        self._clear()
        if not (peak >= threshold or frames >= 2):
            return WakeDecision("none", score=score, reason="not persistent")
        if not self.cfg.stt_confirm:
            return WakeDecision("accept", "model_persisted", peak, "persisted above threshold") if peak >= threshold and frames >= max(1, self.cfg.min_frames) else WakeDecision("none", score=score, reason="not persistent")
        if self._clock() < self._candidate_block_until:
            return WakeDecision("none", score=score, reason="candidate cooldown")
        return WakeDecision("candidate", "stt_confirmed", peak, "needs phrase confirmation")

    def _clear(self) -> None:
        self._run_frames = 0
        self._run_peak = 0.0
        self._strong_run = 0


def confirm_phrase(text: str) -> tuple[bool, str, str]:
    """(ok, normalised phrase, why). ok only for exactly "hey jarvis" / "jarvis"."""
    phrase = normalize_wake_phrase(text)
    if phrase is not None:
        return True, phrase, "phrase matched"
    if wake_plus_sleep(text):
        return False, "", "wake plus sleep: ignored"
    return False, "", "not a wake phrase"
