"""TTSProvider implementation backed by Piper.

Runs fully local, no network access needed once the voice model is
downloaded. Requires a Piper voice model (.onnx, optionally with a
matching .onnx.json config) — see docs/voice-system.md for where to get one.
"""

from pathlib import Path

import numpy as np

from voice.exceptions import ProviderNotConfiguredError
from voice.tts.base import TTSProvider


class PiperProvider(TTSProvider):
    """Converts text to speech using a local Piper voice model."""

    def __init__(self, model_path: str):
        if not model_path:
            raise ProviderNotConfiguredError(
                "TTS_MODEL_PATH is not set. Download a Piper voice model "
                "(e.g. en_US-lessac-medium.onnx) and set the path — see "
                "docs/voice-system.md."
            )
        if not Path(model_path).is_file():
            raise ProviderNotConfiguredError(
                f"Piper voice model not found at '{model_path}'. See "
                "docs/voice-system.md for how to download it."
            )

        try:
            from piper.voice import PiperVoice
        except ImportError as exc:  # pragma: no cover - dependency is in requirements.txt
            raise ProviderNotConfiguredError(
                "piper-tts is not installed. Run: pip install -r requirements.txt"
            ) from exc

        try:
            self._voice = PiperVoice.load(model_path)
        except Exception as exc:  # noqa: BLE001 - covers missing/corrupt config, bad onnx file
            raise ProviderNotConfiguredError(
                f"Failed to load Piper voice model '{model_path}': {exc}"
            ) from exc

    def is_ready(self) -> bool:
        return self._voice is not None

    def synthesize(self, text: str) -> tuple[np.ndarray, int]:
        chunks = list(self._voice.synthesize(text))
        if not chunks:
            raise ProviderNotConfiguredError("Piper produced no audio for the given text")
        audio = np.concatenate([chunk.audio_float_array for chunk in chunks])
        return audio, chunks[0].sample_rate
