"""Text-to-speech provider abstraction."""

from abc import abstractmethod

import numpy as np

from voice.base import VoiceProvider


class TTSProvider(VoiceProvider):
    """Base interface for converting text into audio."""

    @abstractmethod
    def synthesize(self, text: str) -> tuple[np.ndarray, int]:
        """Return (mono audio samples, sample_rate) for the given text.

        Raises on a backend failure — never returns silence or fabricated
        audio pretending speech was generated.
        """
        raise NotImplementedError
