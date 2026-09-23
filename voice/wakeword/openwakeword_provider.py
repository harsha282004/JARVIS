"""WakeWordProvider implementation backed by openWakeWord.

openWakeWord is used instead of Porcupine because it runs fully local
with pretrained, freely downloadable ONNX models and does not require a
vendor account or access key. It ships a pretrained model for the exact
phrase JARVIS targets ("hey_jarvis_v0.1") — see docs/voice-system.md for
where to download it. This module never claims the wake word works unless
a real model file was loaded successfully.
"""

from pathlib import Path

import numpy as np

from voice.exceptions import ProviderNotConfiguredError
from voice.wakeword.base import WakeWordProvider

_FRAME_SAMPLES = 1280  # openWakeWord requires 80ms (1280 samples at 16kHz) chunks


class OpenWakeWordProvider(WakeWordProvider):
    """Detects a wake word from a stream of 16kHz mono int16 audio frames."""

    def __init__(self, model_path: str, threshold: float = 0.5):
        if not model_path:
            raise ProviderNotConfiguredError(
                "WAKE_WORD_MODEL_PATH is not set. Download an openWakeWord "
                "model (e.g. hey_jarvis_v0.1.onnx) and set the path — see "
                "docs/voice-system.md."
            )
        if not Path(model_path).is_file():
            raise ProviderNotConfiguredError(
                f"Wake-word model file not found at '{model_path}'. See "
                "docs/voice-system.md for how to download it."
            )

        try:
            from openwakeword.model import Model
        except ImportError as exc:  # pragma: no cover - dependency is in requirements.txt
            raise ProviderNotConfiguredError(
                "openwakeword is not installed. Run: pip install -r requirements.txt"
            ) from exc

        self._threshold = threshold
        self._model_name = Path(model_path).stem

        # openWakeWord also needs a melspectrogram and an embedding feature
        # model. We keep the whole wake-word setup self-contained in one
        # directory rather than relying on the package's install location
        # (see docs/voice-system.md for exactly what must be downloaded).
        model_dir = Path(model_path).parent
        melspec_path = model_dir / "melspectrogram.onnx"
        embedding_path = model_dir / "embedding_model.onnx"
        missing = [p for p in (melspec_path, embedding_path) if not p.is_file()]
        if missing:
            raise ProviderNotConfiguredError(
                "Missing openWakeWord feature model(s): "
                f"{', '.join(str(p) for p in missing)}. See docs/voice-system.md."
            )

        try:
            self._model = Model(
                wakeword_models=[model_path],
                inference_framework="onnx",
                melspec_model_path=str(melspec_path),
                embedding_model_path=str(embedding_path),
            )
        except Exception as exc:  # noqa: BLE001 - openWakeWord raises various backend errors
            raise ProviderNotConfiguredError(
                f"Failed to load wake-word model '{model_path}': {exc}"
            ) from exc

    def is_ready(self) -> bool:
        return self._model is not None

    @property
    def frame_samples(self) -> int:
        return _FRAME_SAMPLES

    def process(self, frame: np.ndarray) -> bool:
        scores = self._model.predict(frame)
        score = scores.get(self._model_name, 0.0)
        return score >= self._threshold
