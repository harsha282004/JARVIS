"""Unit tests: voice provider interfaces are correctly shaped abstractions."""

import pytest


def test_wakeword_provider_is_abstract():
    from voice.wakeword.base import WakeWordProvider

    with pytest.raises(TypeError):
        WakeWordProvider()


def test_stt_provider_is_abstract():
    from voice.stt.base import STTProvider

    with pytest.raises(TypeError):
        STTProvider()


def test_tts_provider_is_abstract():
    from voice.tts.base import TTSProvider

    with pytest.raises(TypeError):
        TTSProvider()


def test_llm_provider_still_abstract():
    from backend.core.llm.base import LLMProvider

    with pytest.raises(TypeError):
        LLMProvider()


def test_voice_module_imports_cleanly():
    import voice.audio  # noqa: F401
    import voice.bootstrap  # noqa: F401
    import voice.engine  # noqa: F401
    import voice.exceptions  # noqa: F401
