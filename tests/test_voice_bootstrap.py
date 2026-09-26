"""Unit tests for voice.bootstrap provider selection and config validation."""

import pytest

from backend.core.config import Settings
from voice.bootstrap import (
    _build_llm,
    _build_stt,
    _build_tts,
    _build_wakeword,
    build_voice_engine,
)
from voice.exceptions import ProviderNotConfiguredError


def _settings(**overrides) -> Settings:
    base = {
        "DATABASE_URL": "postgresql+psycopg2://jarvis:jarvis@localhost:5432/jarvis_test",
    }
    base.update(overrides)
    return Settings(_env_file=None, **base)


def test_wake_word_disabled_raises():
    settings = _settings(WAKE_WORD_ENABLED=False)
    with pytest.raises(ProviderNotConfiguredError, match="WAKE_WORD_ENABLED"):
        build_voice_engine(settings)


def test_unknown_wakeword_provider_raises():
    settings = _settings(WAKE_WORD_PROVIDER="some_other_vendor")
    with pytest.raises(ProviderNotConfiguredError, match="WAKE_WORD_PROVIDER"):
        _build_wakeword(settings)


def test_unknown_stt_provider_raises():
    settings = _settings(STT_PROVIDER="google_cloud_speech")
    with pytest.raises(ProviderNotConfiguredError, match="STT_PROVIDER"):
        _build_stt(settings)


def test_unknown_llm_provider_raises():
    settings = _settings(LLM_PROVIDER="some_cloud_llm")
    with pytest.raises(ProviderNotConfiguredError, match="LLM_PROVIDER"):
        _build_llm(settings)


def test_unknown_tts_provider_raises():
    settings = _settings(TTS_PROVIDER="some_other_tts")
    with pytest.raises(ProviderNotConfiguredError, match="TTS_PROVIDER"):
        _build_tts(settings)


def test_groq_provider_selected_for_default_settings():
    from backend.core.llm.groq_provider import GroqProvider

    provider = _build_llm(_settings())
    assert isinstance(provider, GroqProvider) and provider.model == "openai/gpt-oss-20b"


def test_ollama_provider_is_still_selectable():
    from backend.core.llm.ollama_provider import OllamaProvider

    assert isinstance(_build_llm(_settings(LLM_PROVIDER="ollama", LLM_MODEL="llama3")), OllamaProvider)


def test_unknown_llm_provider_is_a_provider_configuration_error():
    with pytest.raises(ProviderNotConfiguredError, match="Unknown LLM_PROVIDER"):
        _build_llm(_settings(LLM_PROVIDER="mystery"))
