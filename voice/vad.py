"""Voice activity detection: end-of-utterance by silence instead of a fixed recording window.

An energy detector with an adaptive noise floor (no model, no dependencies, runs per 80 ms frame in microseconds).
`UtteranceDetector` is the state machine around it: it waits for speech to start, follows it, and finishes when the speaker
has been quiet for `silence_seconds`, when the utterance reaches `max_utterance_seconds`, or when nobody starts speaking
within `no_speech_seconds`. Utterances shorter than `min_utterance_seconds` are noise (a click, a cough) and are dropped.

Audio only lives in memory for the duration of one utterance and is never written anywhere.
"""

from dataclasses import dataclass
from enum import StrEnum

import numpy as np


class UtteranceStatus(StrEnum):
    WAITING = "waiting"          # no speech yet
    SPEAKING = "speaking"
    COMPLETE = "complete"        # speech followed by enough silence
    TOO_LONG = "too_long"        # cut at max_utterance_seconds
    NO_SPEECH = "no_speech"      # nobody spoke within the wait window
    TOO_SHORT = "too_short"      # a blip below the minimum length


def frame_level(frame: np.ndarray) -> float:
    """RMS of one int16/float frame as a fraction of full scale (0..1)."""
    if frame.size == 0:
        return 0.0
    x = frame.astype(np.float32)
    if frame.dtype == np.int16:
        x = x / 32768.0
    return float(np.sqrt(np.mean(x * x)))


class EnergyVAD:
    """Speech if the frame is clearly louder than the noise floor, which follows quiet frames slowly."""

    def __init__(self, threshold: float = 0.015, ratio: float = 3.0, floor_decay: float = 0.05, initial_floor: float = 0.002):
        self.threshold = threshold      # absolute minimum RMS that can count as speech
        self._ratio = ratio             # ...and it must exceed floor * ratio
        self._decay = floor_decay
        self.noise_floor = initial_floor

    def is_speech(self, frame: np.ndarray) -> bool:
        level = frame_level(frame)
        speech = level >= max(self.threshold, self.noise_floor * self._ratio)
        if not speech:  # only learn the background from non-speech, so a long utterance cannot raise the floor
            self.noise_floor += self._decay * (level - self.noise_floor)
        return speech


@dataclass
class UtteranceResult:
    status: UtteranceStatus
    audio: np.ndarray
    speech_seconds: float
    total_seconds: float
    speech_started_after: float | None  # seconds from start until speech began (None = never)


class UtteranceDetector:
    """Feed frames with `push()` until it returns a finished status; `result()` then gives the captured audio."""

    def __init__(
        self,
        vad: EnergyVAD,
        sample_rate: int,
        silence_seconds: float = 1.0,
        max_utterance_seconds: float = 15.0,
        min_utterance_seconds: float = 0.15,
        no_speech_seconds: float = 6.0,
        preroll_frames: int = 3,
    ):
        self._vad = vad
        self._rate = sample_rate
        self._silence = silence_seconds
        self._max = max_utterance_seconds
        self._min = min_utterance_seconds
        self._no_speech = no_speech_seconds
        self._preroll_limit = preroll_frames
        self._preroll: list[np.ndarray] = []
        self._frames: list[np.ndarray] = []
        self._elapsed = 0.0
        self._speech = 0.0
        self._quiet_run = 0.0
        self._started_at: float | None = None
        self.status = UtteranceStatus.WAITING

    @property
    def finished(self) -> bool:
        return self.status not in (UtteranceStatus.WAITING, UtteranceStatus.SPEAKING)

    def push(self, frame: np.ndarray) -> UtteranceStatus:
        if self.finished:
            return self.status
        seconds = frame.size / self._rate
        self._elapsed += seconds
        speech = self._vad.is_speech(frame)
        if self.status is UtteranceStatus.WAITING:
            if speech:
                self.status = UtteranceStatus.SPEAKING
                self._started_at = self._elapsed - seconds
                self._frames = [*self._preroll, frame]  # keep the syllable before the trigger
                self._speech = seconds
                self._quiet_run = 0.0
            else:
                self._preroll.append(frame)
                del self._preroll[: -self._preroll_limit]
                if self._elapsed >= self._no_speech:
                    self.status = UtteranceStatus.NO_SPEECH
            return self.status
        self._frames.append(frame)
        if speech:
            self._speech += seconds
            self._quiet_run = 0.0
        else:
            self._quiet_run += seconds
        spoken_span = self._elapsed - (self._started_at or 0.0)
        if self._quiet_run >= self._silence:
            self.status = UtteranceStatus.COMPLETE if self._speech >= self._min else UtteranceStatus.TOO_SHORT
        elif spoken_span >= self._max:
            self.status = UtteranceStatus.TOO_LONG
        return self.status

    def result(self) -> UtteranceResult:
        audio = np.concatenate(self._frames) if self._frames else np.array([], dtype=np.int16)
        return UtteranceResult(self.status, audio, self._speech, self._elapsed, self._started_at)
