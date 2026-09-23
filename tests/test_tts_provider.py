"""Unit tests for PiperProvider configuration handling.

Does not load a real voice model (covered by manual end-to-end testing) —
only verifies the provider fails clearly rather than pretending audio was
generated.
"""

import pytest

from voice.exceptions import ProviderNotConfiguredError
from voice.tts.piper_provider import PiperProvider


def test_missing_model_path_raises_clear_error():
    with pytest.raises(ProviderNotConfiguredError, match="TTS_MODEL_PATH"):
        PiperProvider(model_path="")


def test_nonexistent_model_file_raises_clear_error(tmp_path):
    missing = tmp_path / "does_not_exist.onnx"
    with pytest.raises(ProviderNotConfiguredError, match="not found"):
        PiperProvider(model_path=str(missing))
