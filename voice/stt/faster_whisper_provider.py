"""STTProvider implementation backed by Faster-Whisper.

Runs fully local (CTranslate2 inference, CPU by default). Larger model
sizes are more accurate but need more RAM/CPU (or a GPU) and take longer
per utterance — see docs/voice-system.md for the size/hardware tradeoffs.
"""

import math

import numpy as np

from voice.exceptions import ProviderNotConfiguredError
from voice.stt.base import STTProvider, Transcription


# A short vocabulary hint. Measured with the base model on synthetic speech: a lone "Stop." was heard as "Top" in 1 of 4 runs and
# "Cancel." as "Council"/"Pencil" in 3 of 4 without it, and correctly in 8 of 8 with it. It does not make silence produce text
# (checked: silence still transcribes to "").
DEFAULT_PROMPT = "Voice commands for JARVIS: stop, cancel, wait, yes, no, remind me, calendar, email."


class FasterWhisperProvider(STTProvider):
    """Converts captured audio to text using a local Faster-Whisper model."""

    def __init__(self, model_size: str, language: str, device: str = "cpu", initial_prompt: str | None = DEFAULT_PROMPT):
        try:
            from faster_whisper import WhisperModel
        except ImportError as exc:  # pragma: no cover - dependency is in requirements.txt
            raise ProviderNotConfiguredError(
                "faster-whisper is not installed. Run: pip install -r requirements.txt"
            ) from exc

        self._language = language
        self._prompt = initial_prompt or None
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
        return self.transcribe_detailed(audio, sample_rate).text

    def transcribe_detailed(self, audio: np.ndarray, sample_rate: int) -> Transcription:
        if audio.dtype == np.int16:
            audio = audio.astype(np.float32) / 32768.0

        # vad_filter: silence must transcribe to "" (not hallucinated text),
        # since a silent follow-up window is how a conversation ends.
        segments, info = self._model.transcribe(
            audio, language=self._language, beam_size=1, vad_filter=True, initial_prompt=self._prompt
        )
        segments = list(segments)
        text = " ".join(segment.text.strip() for segment in segments).strip()
        confidence = None
        if segments:
            # Two independent signals: the model's belief that this is speech at all, and how sure it was of its words.
            speech = 1.0 - max(float(getattr(s, "no_speech_prob", 0.0)) for s in segments)
            words = [math.exp(float(s.avg_logprob)) for s in segments if getattr(s, "avg_logprob", None) is not None]
            confidence = round(max(0.0, min(1.0, speech * (sum(words) / len(words) if words else 1.0))), 3)
        return Transcription(text, confidence if text else None, len(audio) / sample_rate if sample_rate else 0.0,
                             getattr(info, "language", None))
