"""Speech-to-text provider abstraction."""

from abc import abstractmethod

from dataclasses import dataclass

import numpy as np

from voice.base import VoiceProvider


@dataclass(frozen=True)
class Transcription:
    """One recognition result. `confidence` is 0..1 (1 - no_speech probability, blended with the average word probability);
    None means the engine did not report one. Never contains audio."""

    text: str
    confidence: float | None = None
    audio_seconds: float = 0.0
    language: str | None = None


class STTProvider(VoiceProvider):
    """Base interface for converting captured audio into text."""

    @abstractmethod
    def transcribe(self, audio: np.ndarray, sample_rate: int) -> str:
        """Transcribe mono audio samples to text. Never fabricates a transcript
        — returns an empty string if nothing intelligible was heard, and
        raises on a backend failure."""
        raise NotImplementedError

    def transcribe_detailed(self, audio: np.ndarray, sample_rate: int) -> Transcription:
        """Like `transcribe`, plus confidence and duration. The default wraps `transcribe` (no confidence available)."""
        return Transcription(self.transcribe(audio, sample_rate), None, len(audio) / sample_rate if sample_rate else 0.0)
