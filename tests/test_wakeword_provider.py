"""Unit tests for OpenWakeWordProvider configuration handling.

Does not load a real model (that requires a downloaded .onnx file and is
covered by manual end-to-end testing) — only verifies the provider fails
clearly and does not fabricate readiness when misconfigured.
"""

import pytest

from voice.exceptions import ProviderNotConfiguredError
from voice.wakeword.openwakeword_provider import OpenWakeWordProvider


def test_missing_model_path_raises_clear_error():
    with pytest.raises(ProviderNotConfiguredError, match="WAKE_WORD_MODEL_PATH"):
        OpenWakeWordProvider(model_path="", threshold=0.5)


def test_nonexistent_model_file_raises_clear_error(tmp_path):
    missing = tmp_path / "does_not_exist.onnx"
    with pytest.raises(ProviderNotConfiguredError, match="not found"):
        OpenWakeWordProvider(model_path=str(missing), threshold=0.5)
