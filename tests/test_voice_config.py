"""Phase 1 configuration tests: voice settings load with safe defaults."""

from backend.core.config import Settings, get_settings


def test_voice_settings_defaults():
    # _env_file=None: defaults must hold independent of any local .env,
    # which a developer may have pointed at real downloaded models.
    settings = Settings(
        _env_file=None,
        DATABASE_URL="postgresql+psycopg2://jarvis:jarvis@localhost:5432/jarvis_test",
    )
    assert settings.WAKE_WORD_ENABLED is True
    assert settings.WAKE_WORD_PROVIDER == "openwakeword"
    assert settings.WAKE_WORD_MODEL_PATH == ""
    assert settings.STT_PROVIDER == "faster_whisper"
    assert settings.STT_MODEL == "base"
    assert settings.STT_LANGUAGE == "en"
    assert settings.TTS_PROVIDER == "piper"
    assert settings.TTS_MODEL_PATH == ""
    assert settings.AUDIO_SAMPLE_RATE == 16000


def test_voice_settings_overridable_from_env(monkeypatch):
    monkeypatch.setenv("WAKE_WORD_MODEL_PATH", "C:/models/hey_jarvis_v0.1.onnx")
    monkeypatch.setenv("STT_MODEL", "small")
    get_settings.cache_clear()
    try:
        settings = get_settings()
        assert settings.WAKE_WORD_MODEL_PATH == "C:/models/hey_jarvis_v0.1.onnx"
        assert settings.STT_MODEL == "small"
    finally:
        get_settings.cache_clear()
