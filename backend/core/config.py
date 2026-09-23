"""Centralized application configuration.

Settings are loaded from environment variables (and a local `.env` file in
development). Required values have no fabricated defaults: if a required
setting is missing, startup fails with a clear error instead of guessing.
"""

from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=True,
        extra="ignore",
    )

    # --- Application ---
    APP_ENV: Literal["development", "test", "production"] = "development"
    APP_NAME: str = "JARVIS"
    LOG_LEVEL: str = "INFO"

    # --- API server ---
    API_HOST: str = "127.0.0.1"
    API_PORT: int = 8000

    # --- Database ---
    # Required: no default. Startup must fail clearly if this is unset
    # rather than silently falling back to a fabricated connection string.
    DATABASE_URL: str = Field(...)

    # --- LLM provider ---
    LLM_PROVIDER: str = "ollama"
    # Model name for whichever LLM_PROVIDER is active (e.g. an Ollama model
    # tag such as "llama3"). Phase 1 intentionally does not add a separate
    # OLLAMA_MODEL field — LLM_MODEL is the single source of truth so the
    # provider abstraction stays provider-agnostic.
    LLM_MODEL: str = "llama3"
    OLLAMA_BASE_URL: str = "http://localhost:11434"

    # --- Voice: wake word ---
    WAKE_WORD_ENABLED: bool = True
    WAKE_WORD_PROVIDER: str = "openwakeword"
    # Path to a downloaded openWakeWord ONNX model file (e.g. hey_jarvis_v0.1.onnx).
    # No default: must be set explicitly before the wake-word provider can start.
    WAKE_WORD_MODEL_PATH: str = ""
    WAKE_WORD_THRESHOLD: float = 0.5

    # --- Voice: audio I/O ---
    # Empty string = system default input/output device.
    MICROPHONE_DEVICE: str = ""
    AUDIO_SAMPLE_RATE: int = 16000
    # Fixed capture window for a single utterance after wake-word activation.
    # Phase 3 will replace this with proper end-of-speech detection.
    AUDIO_LISTEN_SECONDS: float = 5.0

    # --- Voice: speech-to-text ---
    STT_PROVIDER: str = "faster_whisper"
    STT_MODEL: str = "base"
    STT_LANGUAGE: str = "en"
    STT_DEVICE: str = "cpu"

    # --- Voice: text-to-speech ---
    TTS_PROVIDER: str = "piper"
    # Path to a downloaded Piper voice model (.onnx). No default: must be
    # set explicitly before the TTS provider can start.
    TTS_MODEL_PATH: str = ""
    TTS_VOICE: str = "en_US-lessac-medium"

    # --- Windows runtime ---
    # Master switch: when false, `python -m desktop.launcher` exits immediately.
    # Lets a startup-launched JARVIS be disabled without removing the shortcut.
    JARVIS_RUNTIME_ENABLED: bool = True
    # false = run without a tray icon (headless; stop with Ctrl+C).
    JARVIS_TRAY_ENABLED: bool = True


@lru_cache
def get_settings() -> Settings:
    """Return a cached Settings instance.

    Raises pydantic.ValidationError if required environment variables are
    missing or malformed.
    """
    return Settings()
