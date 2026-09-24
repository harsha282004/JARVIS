"""Centralized application configuration.

Settings are loaded from environment variables (and a local `.env` file in
development). Required values have no fabricated defaults: if a required
setting is missing, startup fails with a clear error instead of guessing.
"""

from functools import lru_cache
from typing import Literal
from zoneinfo import ZoneInfo

from pydantic import Field, SecretStr, field_validator
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

    # --- Conversation (in-memory only) ---
    # Inactivity after which the current conversation session ends.
    JARVIS_CONVERSATION_TIMEOUT_SECONDS: float = Field(default=120.0, gt=0)
    # Most recent user/assistant messages kept as context (minimum 2).
    JARVIS_MAX_CONVERSATION_MESSAGES: int = Field(default=20, ge=2)

    # --- Agent brain (reasoning/planning only; executes nothing) ---
    JARVIS_AGENT_ENABLED: bool = True
    # Upper bound on steps in a generated plan.
    JARVIS_AGENT_MAX_PLAN_STEPS: int = Field(default=8, ge=1)

    # --- Personal memory (local PostgreSQL; explicit user statements only) ---
    JARVIS_MEMORY_ENABLED: bool = True
    # Most relevant memories added to a request.
    JARVIS_MEMORY_MAX_RETRIEVAL: int = Field(default=5, ge=1, le=20)
    # false = every candidate waits for confirmation instead of being saved.
    JARVIS_MEMORY_AUTO_SAVE: bool = True
    # Candidates below this confidence are never auto-saved.
    JARVIS_MEMORY_MIN_CONFIDENCE: Literal["low", "medium", "high"] = "medium"

    # --- Personal RAG (local documents; separate from personal memory) ---
    JARVIS_RAG_ENABLED: bool = True
    JARVIS_RAG_EMBEDDING_MODEL: str = "sentence-transformers/all-MiniLM-L6-v2"
    JARVIS_RAG_TOP_K: int = Field(default=5, ge=1, le=20)
    JARVIS_RAG_CHUNK_SIZE: int = Field(default=800, ge=50)
    JARVIS_RAG_CHUNK_OVERLAP: int = Field(default=100, ge=0)
    # Cosine similarity below this is treated as not relevant (insufficient-context path).
    JARVIS_RAG_MIN_SCORE: float = Field(default=0.35, ge=-1.0, le=1.0)
    JARVIS_RAG_MAX_DOCUMENT_SIZE_MB: int = Field(default=25, ge=1)
    JARVIS_RAG_MAX_CHUNKS_PER_DOCUMENT: int = Field(default=2000, ge=1)

    # --- Personal knowledge graph (relational, in PostgreSQL) ---
    JARVIS_KG_ENABLED: bool = True
    JARVIS_KG_MAX_PATH_DEPTH: int = Field(default=3, ge=1, le=6)
    JARVIS_KG_MAX_RESULTS: int = Field(default=20, ge=1, le=100)
    # Relationships below this confidence are not stored by extraction nor shown to the LLM.
    JARVIS_KG_MIN_CONFIDENCE: Literal["low", "medium", "high"] = "medium"

    # --- Tasks and reminders (local PostgreSQL; run the Alembic migration once) ---
    JARVIS_TASKS_ENABLED: bool = True
    JARVIS_REMINDERS_ENABLED: bool = True
    # IANA timezone name for parsing and showing times ("Asia/Kolkata"). Empty = detect this computer's
    # timezone once at startup. The database always stores UTC.
    JARVIS_TIMEZONE: str = ""
    # How often the scheduler looks for due reminders.
    JARVIS_REMINDER_POLL_SECONDS: float = Field(default=15.0, ge=1.0, le=3600.0)
    # A reminder that came due while JARVIS was not running: "notify" delivers it late, marked as missed;
    # "expire" does not deliver it.
    JARVIS_MISSED_REMINDER_POLICY: Literal["notify", "expire"] = "notify"
    JARVIS_DEFAULT_TASK_PRIORITY: Literal["low", "medium", "high", "critical"] = "medium"
    JARVIS_REMINDER_DESKTOP_NOTIFICATIONS: bool = True
    JARVIS_REMINDER_VOICE_NOTIFICATIONS: bool = True

    # --- Gmail intelligence (read-only; off until you set it up, see docs/gmail-intelligence.md) ---
    JARVIS_GMAIL_ENABLED: bool = False
    # OAuth client id/secret from your Google Cloud "Desktop app" client, as an alternative to the JSON file below.
    # (`.env` is git-ignored. The secret is never printed or logged.)
    GMAIL_CLIENT_ID: str = ""
    GMAIL_CLIENT_SECRET: SecretStr = SecretStr("")
    # Google OAuth "Desktop app" client file and the token created by `python scripts/gmail_cli.py auth`.
    # Relative paths are relative to the project folder. Both locations are git-ignored.
    JARVIS_GMAIL_CREDENTIALS_PATH: str = ".jarvis/gmail/credentials.json"
    JARVIS_GMAIL_TOKEN_PATH: str = ".jarvis/gmail/token.json"
    # Most emails fetched by one search (JARVIS never downloads a whole mailbox).
    JARVIS_GMAIL_MAX_RESULTS: int = Field(default=10, ge=1, le=50)

    # --- Event & deadline intelligence (local PostgreSQL; run the Alembic migration once) ---
    JARVIS_EVENTS_ENABLED: bool = True
    # How far ahead "what's coming up" looks.
    JARVIS_EVENT_DEFAULT_LOOKAHEAD_DAYS: int = Field(default=7, ge=1, le=365)
    # Most events listed or searched at once.
    JARVIS_EVENT_MAX_RESULTS: int = Field(default=20, ge=1, le=100)

    # --- Permissions & security audit ---
    # How long a permission request stays valid. Unknown tools are always denied;
    # that is an invariant, not a setting.
    JARVIS_PERMISSION_DEFAULT_EXPIRY_SECONDS: float = Field(default=300.0, gt=0)
    # In-memory audit trail plus structured SECURITY_EVENT log lines.
    JARVIS_PERMISSION_AUDIT_ENABLED: bool = True

    # --- Windows runtime ---
    # Master switch: when false, `python -m desktop.launcher` exits immediately.
    # Lets a startup-launched JARVIS be disabled without removing the shortcut.
    JARVIS_RUNTIME_ENABLED: bool = True
    # false = run without a tray icon (headless; stop with Ctrl+C).
    JARVIS_TRAY_ENABLED: bool = True

    @field_validator("JARVIS_TIMEZONE")
    @classmethod
    def _valid_timezone(cls, value: str) -> str:
        value = value.strip()
        if value:
            try:
                ZoneInfo(value)
            except Exception:  # noqa: BLE001 - unknown key, bad format or missing tz database
                raise ValueError("JARVIS_TIMEZONE must be an IANA timezone name such as 'Europe/London'") from None
        return value


@lru_cache
def get_settings() -> Settings:
    """Return a cached Settings instance.

    Raises pydantic.ValidationError if required environment variables are
    missing or malformed.
    """
    return Settings()
