"""Voice provider abstraction.

Defines the contract for wake-word detection, speech-to-text and
text-to-speech backends. No wake-word engine, Whisper/Faster-Whisper,
or TTS engine is implemented in Phase 0.
"""

from abc import ABC, abstractmethod


class VoiceProvider(ABC):
    """Base interface shared by voice-pipeline components."""

    @abstractmethod
    def is_ready(self) -> bool:
        """Report whether the underlying engine is initialized. Not implemented in Phase 0."""
        raise NotImplementedError
