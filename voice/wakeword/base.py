"""Wake-word provider abstraction.

The rest of the voice pipeline depends only on this interface, not on any
specific wake-word engine, so the engine can be swapped later.
"""

from abc import abstractmethod

import numpy as np

from voice.base import VoiceProvider


class WakeWordProvider(VoiceProvider):
    """Base interface for a streaming wake-word detector."""

    @property
    @abstractmethod
    def frame_samples(self) -> int:
        """Number of int16 samples expected per process() call."""
        raise NotImplementedError

    @abstractmethod
    def process(self, frame: np.ndarray) -> bool:
        """Feed one audio frame; return True if the wake word was just detected."""
        raise NotImplementedError
