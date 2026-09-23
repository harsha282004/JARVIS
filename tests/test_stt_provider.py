"""Unit tests for FasterWhisperProvider error handling.

Does not download/load a real Whisper model (that needs network access on
first run and is covered by manual end-to-end testing) — verifies the
provider wraps backend failures in a clear ProviderNotConfiguredError
instead of pretending a model loaded.
"""

import faster_whisper
import pytest

from voice.exceptions import ProviderNotConfiguredError
from voice.stt.faster_whisper_provider import FasterWhisperProvider


def test_backend_load_failure_raises_clear_error(monkeypatch):
    def raise_error(*args, **kwargs):
        raise RuntimeError("could not resolve model")

    monkeypatch.setattr(faster_whisper, "WhisperModel", raise_error)

    with pytest.raises(ProviderNotConfiguredError, match="Faster-Whisper"):
        FasterWhisperProvider(model_size="not-a-real-model", language="en")
