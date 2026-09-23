"""Speech-to-text provider abstraction."""

from abc import abstractmethod

import numpy as np

from voice.base import VoiceProvider


class STTProvider(VoiceProvider):
    """Base interface for converting captured audio into text."""

    @abstractmethod
    def transcribe(self, audio: np.ndarray, sample_rate: int) -> str:
        """Transcribe mono audio samples to text. Never fabricates a transcript
        — returns an empty string if nothing intelligible was heard, and
        raises on a backend failure."""
        raise NotImplementedError
