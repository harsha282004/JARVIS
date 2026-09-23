"""STTProvider implementation backed by Faster-Whisper.

Runs fully local (CTranslate2 inference, CPU by default). Larger model
sizes are more accurate but need more RAM/CPU (or a GPU) and take longer
per utterance — see docs/voice-system.md for the size/hardware tradeoffs.
"""

import numpy as np

from voice.exceptions import ProviderNotConfiguredError
from voice.stt.base import STTProvider


class FasterWhisperProvider(STTProvider):
    """Converts captured audio to text using a local Faster-Whisper model."""

    def __init__(self, model_size: str, language: str, device: str = "cpu"):
        try:
            from faster_whisper import WhisperModel
        except ImportError as exc:  # pragma: no cover - dependency is in requirements.txt
            raise ProviderNotConfiguredError(
                "faster-whisper is not installed. Run: pip install -r requirements.txt"
            ) from exc

        self._language = language
        compute_type = "int8" if device == "cpu" else "float16"
        try:
            self._model = WhisperModel(model_size, device=device, compute_type=compute_type)
        except Exception as exc:  # noqa: BLE001 - covers missing/corrupt model download
            raise ProviderNotConfiguredError(
                f"Failed to load Faster-Whisper model '{model_size}': {exc}. "
                "The model is downloaded automatically on first use and requires "
                "network access — see docs/voice-system.md."
            ) from exc

    def is_ready(self) -> bool:
        return self._model is not None

    def transcribe(self, audio: np.ndarray, sample_rate: int) -> str:
        if audio.dtype == np.int16:
            audio = audio.astype(np.float32) / 32768.0

        # vad_filter: silence must transcribe to "" (not hallucinated text),
        # since a silent follow-up window is how a conversation ends.
        segments, _info = self._model.transcribe(
            audio, language=self._language, beam_size=1, vad_filter=True
        )
        text = " ".join(segment.text.strip() for segment in segments).strip()
        return text
