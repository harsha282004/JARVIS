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

    # --- Voice: natural conversation (Phase 19). Defaults for the persisted voice settings (.jarvis/voice_settings.json wins once
    # the user changes something from the tray or dashboard). ---
    # End of speech is detected by silence instead of the fixed AUDIO_LISTEN_SECONDS window (which is kept only when this is false).
    VOICE_USE_VAD: bool = True
    VOICE_SILENCE_SECONDS: float = Field(default=1.0, ge=0.3, le=5.0)
    VOICE_MAX_UTTERANCE_SECONDS: float = Field(default=15.0, ge=2.0, le=60.0)
    VOICE_SPEECH_THRESHOLD: float = Field(default=0.015, ge=0.001, le=0.5)
    # After the wake word, follow-ups need no wake word until this much silence.
    VOICE_CONVERSATION_TIMEOUT_SECONDS: float = Field(default=20.0, ge=3.0, le=600.0)
    VOICE_TTS_SPEED: float = Field(default=1.0, ge=0.5, le=2.0)
    VOICE_TTS_VOLUME: float = Field(default=1.0, ge=0.0, le=1.0)
    # Spoken answers longer than this are shortened for the ear; the full text stays on the dashboard.
    VOICE_SPOKEN_MAX_CHARS: int = Field(default=320, ge=80, le=2000)
    # Whether a critical alert may be spoken during Do Not Disturb.
    VOICE_DND_ALLOW_CRITICAL: bool = True

    # --- Browser agent (Phase 20). The browser opens on demand (never at start-up) and only through the registered browser tools. ---
    BROWSER_ENABLED: bool = True
    # auto = try the installed Microsoft Edge, then Chrome, then Playwright's bundled Chromium; or msedge | chrome | chromium.
    BROWSER_TYPE: str = "auto"
    BROWSER_HEADLESS: bool = False
    # A profile dedicated to JARVIS (never your everyday browser profile), so a sign-in you complete yourself persists.
    BROWSER_PROFILE_DIR: str = ".jarvis/browser_profile"
    BROWSER_DEFAULT_TIMEOUT_SECONDS: float = Field(default=10.0, gt=0, le=120)
    BROWSER_NAVIGATION_TIMEOUT_SECONDS: float = Field(default=20.0, gt=0, le=180)
    BROWSER_MAX_TABS: int = Field(default=8, ge=1, le=50)
    BROWSER_RETRIES: int = Field(default=2, ge=0, le=5)  # only for safe, repeatable operations (loading, reading, finding)
    BROWSER_DOWNLOAD_DIR: str = ".jarvis/downloads"
    BROWSER_UPLOAD_DIR: str = ".jarvis/uploads"  # the only folder a file may be uploaded from
    BROWSER_SCREENSHOT_MODE: str = "memory"  # off | memory (measured, not kept) | disk (saved under .jarvis/screenshots)
    BROWSER_ALLOW_PRIVATE_HOSTS: bool = False  # localhost / LAN / cloud-metadata addresses stay blocked unless you turn this on
    BROWSER_SEARCH_URL: str = "https://www.bing.com/search?q={query}"  # DuckDuckGo shows a human-check to automated browsers, which JARVIS never tries to solve

    # --- Autonomous agent (Phase 21): multi-step tasks over the browser and the integrations, every step behind the tool router and permission checks ---
    AUTONOMY_ENABLED: bool = True
    AUTONOMY_MAX_DURATION_SECONDS: float = Field(default=180.0, gt=0, le=3600)
    AUTONOMY_MAX_STEPS: int = Field(default=25, ge=1, le=100)
    AUTONOMY_MAX_TOOL_CALLS: int = Field(default=40, ge=1, le=300)
    AUTONOMY_MAX_RETRIES: int = Field(default=2, ge=0, le=5)
    AUTONOMY_MAX_REPLANS: int = Field(default=3, ge=0, le=10)
    AUTONOMY_LOOP_THRESHOLD: int = Field(default=3, ge=2, le=10)  # the same action with the same page state this many times stops the task
    AUTONOMY_MAX_CONSECUTIVE_FAILURES: int = Field(default=3, ge=1, le=10)
    AUTONOMY_OBSERVATION_TIMEOUT_SECONDS: float = Field(default=10.0, gt=0, le=120)
    AUTONOMY_CONFIRMATION_TIMEOUT_SECONDS: float = Field(default=120.0, gt=0, le=3600)
    AUTONOMY_BROWSER_TASK_TIMEOUT_SECONDS: float = Field(default=60.0, gt=0, le=600)
    AUTONOMY_INLINE_WAIT_SECONDS: float = Field(default=25.0, ge=0, le=300)  # how long a spoken request waits for a quick task before "I'm working on it"
    AUTONOMY_VOICE_PROGRESS: bool = True                                     # short spoken progress updates ("I found your repository.")
    AUTONOMY_HISTORY_SIZE: int = Field(default=30, ge=1, le=200)

    # Personal Operator (Phase 22): end-to-end workflows over Gmail, Calendar, Tasks, Reminders, GitHub, Documents, Memory and the browser
    WORKFLOWS_ENABLED: bool = True
    WORKFLOW_MAX_CONCURRENT: int = Field(default=2, ge=1, le=6)
    WORKFLOW_MAX_DURATION_SECONDS: float = Field(default=240.0, gt=0, le=3600)
    WORKFLOW_MAX_STEPS: int = Field(default=14, ge=1, le=40)
    WORKFLOW_MAX_TOOL_CALLS: int = Field(default=30, ge=1, le=200)
    WORKFLOW_MAX_RETRIES: int = Field(default=2, ge=0, le=5)                  # reads only; a write is never retried
    WORKFLOW_MAX_SYSTEMS: int = Field(default=5, ge=1, le=8)                   # integrations one workflow may touch
    WORKFLOW_CONFIRMATION_TIMEOUT_SECONDS: float = Field(default=120.0, gt=0, le=3600)
    WORKFLOW_INLINE_WAIT_SECONDS: float = Field(default=12.0, ge=0, le=120)
    WORKFLOW_HISTORY_SIZE: int = Field(default=12, ge=1, le=100)
    WORKFLOW_PROACTIVE_ENABLED: bool = True                                    # proactive intelligence may start suggestion-only (read-only) workflows


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

    # --- Google Calendar (off until you set it up, see docs/google-calendar-integration.md) ---
    JARVIS_CALENDAR_ENABLED: bool = False
    # OAuth client for Calendar, as an alternative to the JSON file below. If empty, GMAIL_CLIENT_ID/SECRET are used
    # (one Google "Desktop app" client can serve both APIs). `.env` is git-ignored; the secret is never printed or logged.
    CALENDAR_CLIENT_ID: str = ""
    CALENDAR_CLIENT_SECRET: SecretStr = SecretStr("")
    JARVIS_CALENDAR_CREDENTIALS_PATH: str = ".jarvis/calendar/credentials.json"
    JARVIS_CALENDAR_TOKEN_PATH: str = ".jarvis/calendar/token.json"
    # Most events read per calendar in one request (JARVIS never downloads a whole calendar).
    JARVIS_CALENDAR_MAX_RESULTS: int = Field(default=20, ge=1, le=100)

    # --- Messaging (read-only; off until you set it up, see docs/messaging-integration.md) ---
    # The only supported provider is the official Telegram Bot API: it reads messages sent to a bot you create with
    # BotFather (and groups the bot is added to), never your personal Telegram or WhatsApp chats.
    JARVIS_MESSAGING_ENABLED: bool = False
    # The bot token from BotFather, as an alternative to the file below. `.env` is git-ignored; never printed or logged.
    MESSAGING_TELEGRAM_BOT_TOKEN: SecretStr = SecretStr("")
    JARVIS_MESSAGING_TELEGRAM_TOKEN_PATH: str = ".jarvis/messaging/telegram_token"
    # Most messages read or spoken per request (JARVIS never downloads a whole history).
    JARVIS_MESSAGING_MAX_RESULTS: int = Field(default=20, ge=1, le=100)

    # --- Daily briefing & productivity intelligence (Phase 15; read-only, see docs/daily-briefing-productivity.md) ---
    # Summarizes and prioritizes what your existing tasks, reminders, deadlines, calendar, email and messages already say.
    # It never creates, changes, sends or deletes anything. Sources that are not set up are simply left out.
    JARVIS_BRIEFING_ENABLED: bool = True
    # Most items named per section when you ask for a detailed briefing (spoken briefings use counts instead of long lists).
    JARVIS_BRIEFING_MAX_ITEMS: int = Field(default=10, ge=1, le=50)
    # How many days ahead "upcoming" tasks, deadlines and events reach beyond the period you asked about.
    JARVIS_BRIEFING_LOOKAHEAD_DAYS: int = Field(default=7, ge=1, le=30)
    # Most important/action-required emails a briefing considers (0 leaves email out of briefings).
    JARVIS_BRIEFING_EMAIL_LIMIT: int = Field(default=5, ge=0, le=20)
    # true = the local LLM may rephrase the briefing more naturally, accepted only if it adds nothing the facts do not contain.
    JARVIS_BRIEFING_USE_LLM: bool = False

    # --- Proactive intelligence (Phase 14; see docs/proactive-intelligence.md; run the Alembic migration once) ---
    # OBSERVE -> ANALYZE -> DECIDE -> NOTIFY: JARVIS may tell you about a due task, an approaching event or deadline, a
    # calendar conflict or an email that needs attention, through the existing tray/voice channels. It only notifies;
    # it never acts. Off by default. Existing reminders keep working whether this is on or off.
    JARVIS_PROACTIVE_ENABLED: bool = False
    # How often the engine looks (it runs inside the existing reminder scheduler thread and throttles itself).
    JARVIS_PROACTIVE_POLL_SECONDS: float = Field(default=60.0, ge=10.0, le=3600.0)
    # How far ahead a task or deadline may start to notify (the first tier); tighter tiers are fixed at 1 hour / 15 minutes.
    JARVIS_PROACTIVE_LOOKAHEAD_MINUTES: int = Field(default=1440, ge=15, le=10080)
    # The same source is not notified again within this time unless it became more urgent.
    JARVIS_PROACTIVE_COOLDOWN_MINUTES: int = Field(default=60, ge=0, le=1440)
    # Quiet hours: nothing is delivered (it waits) except a CRITICAL + IMMEDIATE item, to the tray only. HH:MM, local time.
    JARVIS_PROACTIVE_QUIET_HOURS_ENABLED: bool = True
    JARVIS_PROACTIVE_QUIET_START: str = "23:00"
    JARVIS_PROACTIVE_QUIET_END: str = "07:00"
    # At most this many proactive notifications per hour (IMMEDIATE ones are exempt).
    JARVIS_PROACTIVE_MAX_PER_HOUR: int = Field(default=6, ge=1, le=60)
    # Google Calendar / Gmail are read in the background at most this often (minutes).
    JARVIS_PROACTIVE_EXTERNAL_POLL_MINUTES: float = Field(default=10.0, ge=5.0, le=240.0)
    # Which external sources may be observed (each also needs its own integration enabled and set up).
    JARVIS_PROACTIVE_CALENDAR: bool = True
    JARVIS_PROACTIVE_GMAIL: bool = False

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

    # --- Production hardening (Phase 16) ---
    # Directory for JARVIS's own local state (privacy mode, preferences, audit log, timeline). Relative to the project folder; git-ignored.
    JARVIS_STATE_DIR: str = ".jarvis"
    # true = one JSON object per log line (timestamp, severity, component, event, correlation id) instead of the readable line.
    JARVIS_LOG_JSON: bool = False
    # true = the tray runtime also serves the local dashboard and health API on API_HOST:API_PORT (127.0.0.1 only by default).
    JARVIS_API_ENABLED: bool = True
    # true = never contact cloud services (Gmail, Calendar, messaging); local memory, documents, tasks and reminders keep working.
    JARVIS_OFFLINE_MODE: bool = False
    # Seconds between service health checks (tray/dashboard status and automatic recovery).
    JARVIS_HEALTH_INTERVAL_SECONDS: float = Field(default=30.0, ge=5.0, le=3600.0)
    # Restart a crashed voice subsystem automatically (with backoff; gives up after JARVIS_RECOVERY_MAX_ATTEMPTS, then waits and retries).
    JARVIS_AUTO_RECOVERY: bool = True
    JARVIS_RECOVERY_MAX_ATTEMPTS: int = Field(default=5, ge=1, le=50)
    JARVIS_RECOVERY_INITIAL_SECONDS: float = Field(default=2.0, ge=0.1, le=300.0)
    JARVIS_RECOVERY_COOLDOWN_SECONDS: float = Field(default=300.0, ge=1.0, le=86400.0)
    # active | background | paused | private. Applied only when no privacy mode has been saved yet (a saved PRIVATE survives restarts).
    JARVIS_PRIVACY_DEFAULT: Literal["active", "background", "paused", "private"] = "active"
    # PostgreSQL connection pool (ignored for other databases).
    DB_POOL_SIZE: int = Field(default=5, ge=1, le=50)
    DB_MAX_OVERFLOW: int = Field(default=5, ge=0, le=50)
    DB_POOL_RECYCLE_SECONDS: int = Field(default=1800, ge=30)
    DB_CONNECT_TIMEOUT_SECONDS: int = Field(default=10, ge=1, le=120)
    # Notification reliability: the same event is not announced again within this many minutes unless it changed.
    JARVIS_NOTIFICATION_COOLDOWN_MINUTES: int = Field(default=60, ge=0, le=1440)

    # --- Personal intelligence (Phase 17; read-only analysis and planning, see docs/INTELLIGENCE_ENGINE.md) ---
    JARVIS_INTELLIGENCE_ENABLED: bool = True
    # Working day used when planning (HH:MM, local time). A plan never schedules outside it.
    JARVIS_WORKDAY_START: str = "09:00"
    JARVIS_WORKDAY_END: str = "18:00"
    # Minutes assumed for a task with no estimate, and the gap kept before and after calendar events when planning.
    JARVIS_DEFAULT_TASK_MINUTES: int = Field(default=60, ge=5, le=480)
    JARVIS_PLAN_BUFFER_MINUTES: int = Field(default=10, ge=0, le=60)
    # true = create a task automatically when an email asks you to do something by a date; false (default) = ask first.
    JARVIS_AUTO_CREATE_TASKS: bool = False
    # Seconds between background analysis runs (they are skipped when nothing changed and also triggered by events).
    JARVIS_INTELLIGENCE_INTERVAL_SECONDS: float = Field(default=300.0, ge=30.0, le=86400.0)
    # Proactive recommendations from the intelligence layer (delivered through the NotificationCenter, never acted on).
    JARVIS_INTELLIGENCE_PROACTIVE: bool = False
    # Optional scheduled briefings (HH:MM local). The morning briefing is always available on request.
    JARVIS_BRIEFING_TIME: str = "08:00"
    JARVIS_EVENING_REVIEW_ENABLED: bool = False
    JARVIS_EVENING_REVIEW_TIME: str = "20:00"

    # --- Integration Hub (Phase 18; see docs/INTEGRATION_ARCHITECTURE.md) ---
    JARVIS_HUB_ENABLED: bool = True
    # Encrypt OAuth/API tokens at rest with Windows DPAPI (bound to your Windows account). Old plaintext token files keep working and are re-saved encrypted on refresh.
    JARVIS_ENCRYPT_TOKENS: bool = True
    # How often the sync loop looks for integrations that are due (each integration also has its own interval and backoff).
    JARVIS_SYNC_LOOP_SECONDS: float = Field(default=60.0, ge=10.0, le=3600.0)
    # First Gmail sync reads this many days back; later syncs read only what is new.
    JARVIS_GMAIL_SYNC_INITIAL_DAYS: int = Field(default=14, ge=1, le=90)
    # Synchronized items older than this are deleted (their sources are untouched).
    JARVIS_HUB_RETENTION_DAYS: int = Field(default=90, ge=7, le=3650)
    # Where attachments you explicitly ask JARVIS to index are saved.
    JARVIS_GMAIL_ATTACHMENTS_DIR: str = ".jarvis/attachments"
    # GitHub (read-only). A fine-grained token (GITHUB_TOKEN or the encrypted token file) is recommended; the OAuth client id enables the device flow.
    JARVIS_GITHUB_ENABLED: bool = False
    GITHUB_TOKEN: SecretStr = SecretStr("")
    JARVIS_GITHUB_TOKEN_PATH: str = ".jarvis/github/token"
    GITHUB_OAUTH_CLIENT_ID: str = ""
    JARVIS_GITHUB_OAUTH_SCOPE: str = "read:user"
    # Documents: folders JARVIS may watch and index (separate paths with ";"). Empty = nothing is watched.
    JARVIS_DOCUMENT_DIRS: str = ""
    JARVIS_DOCUMENT_REMOVE_DELETED: bool = False

    @field_validator(
        "JARVIS_PROACTIVE_QUIET_START", "JARVIS_PROACTIVE_QUIET_END", "JARVIS_WORKDAY_START", "JARVIS_WORKDAY_END",
        "JARVIS_BRIEFING_TIME", "JARVIS_EVENING_REVIEW_TIME",
    )
    @classmethod
    def _valid_clock(cls, value: str) -> str:
        value = value.strip()
        hours, _, minutes = value.partition(":")
        if not (hours.isdigit() and minutes.isdigit() and len(minutes) == 2 and int(hours) < 24 and int(minutes) < 60):
            raise ValueError("quiet hours must be written as HH:MM, for example 23:00")
        return f"{int(hours):02d}:{minutes}"

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
