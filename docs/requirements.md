# Requirements

## Phase 0 — Foundation & Architecture

### Functional requirements

- The backend must start as a FastAPI application.
- `GET /health` must return a structured response confirming the service
  is alive: `{"status": "ok", "service": "JARVIS"}`.
- Configuration must be loaded from environment variables (`.env` in
  development), with no secrets committed to the repository.
- A required configuration value that is missing must cause a clear,
  immediate failure — never a fabricated default.
- Logging must be centrally configured, with a consistent format
  (timestamp, level, module, message) and a configurable level.
- The system must provide a way to verify PostgreSQL connectivity without
  faking success when the database is unavailable.
- Architectural interfaces must exist for: LLM provider, Tool, Integration,
  Memory, Voice provider — with no concrete implementations.
- A permission boundary (`PermissionManager`) must exist between any future
  agent/tool execution and external systems, defaulting to deny.

### Non-functional requirements

- Modular, readable, typed-where-practical code.
- No business logic inside FastAPI route handlers.
- No circular imports between `backend`, `agent`, `voice`, `integrations`.
- No unnecessary dependencies — only what Phase 0 actually uses.
- Deterministic, automated tests covering configuration, the health
  endpoint, database configuration behavior, and core imports.

### Explicitly out of scope for Phase 0

See "What Phase 0 intentionally did not implement" in
`docs/architecture.md`. In short (as of Phase 0): voice, agent
reasoning/execution, memory, all integrations, desktop packaging, the
frontend, and production deployment.

## Phase 1 — Voice Engine

### Functional requirements

- Local wake-word detection for "Hey JARVIS" (openWakeWord), with no
  vendor account/access key required.
- Local speech-to-text (Faster-Whisper) converting captured audio to text.
- An `OllamaProvider` implementation of the Phase 0 `LLMProvider`
  interface, calling a local Ollama server.
- Local text-to-speech (Piper) converting the LLM's response to audio and
  playing it through the speakers.
- `VoiceEngine` (`voice/engine.py`) driving one wake-word -> response
  cycle through a `WAITING -> LISTENING -> TRANSCRIBING -> THINKING ->
  SPEAKING -> WAITING` state machine, with no persisted conversation state.
- All new configuration added to the existing Phase 0 `Settings` class —
  no second configuration system.
- The LLM must never claim access to email, calendar, messages, files,
  tasks, or personal memory it doesn't have (Phase 1 has none of these).
- A provider (wake word, STT, LLM, TTS) that cannot start (missing model
  file, unreachable server, bad config) must fail with a clear error, not
  fabricate a response.

### Non-functional requirements

- Every provider stays behind its Phase 0/1 interface — replaceable via
  configuration, not hardcoded call sites.
- No continuous microphone upload, no silent/background audio recording,
  no raw audio persisted to disk, no microphone data exposed via any API.
- No secrets or credentials logged; no API keys/passwords/tokens
  hardcoded.
- Only genuinely required dependencies added (see `requirements.txt`);
  no LangGraph/LangChain in Phase 1.
- Deterministic unit tests (mocked backends) plus integration tests that
  self-skip when real models/servers aren't available — never a fabricated
  pass.

### Explicitly out of scope for Phase 1

See "What Phase 1 intentionally does not implement" in
`docs/architecture.md` and the non-goals list in `docs/voice-system.md`.
In short: multi-turn conversation, conversation memory, personal
memory/RAG, a knowledge graph, all integrations (Gmail, Calendar,
messaging, YouTube, Spotify, browser/desktop automation), tasks/reminders,
proactive notifications, remote access, Windows startup/tray, the React
dashboard, multi-agent orchestration, research mode, vision, and
production deployment.

## Phase 2 — Windows Runtime

### Functional requirements

- `python -m desktop.launcher` runs JARVIS as a long-lived background
  process (no VS Code/terminal needed; `pythonw` for no console window),
  reusing the Phase 1 `VoiceEngine`/bootstrap.
- Explicit lifecycle states STARTING, RUNNING, PAUSED, STOPPING, STOPPED,
  ERROR, owned by `RuntimeManager`; idempotent, safe shutdown that releases
  the microphone.
- System tray icon showing status with Start/Resume, Pause, Restart, Exit.
- Runtime-level pause/resume (microphone released while paused).
- Recovery after Windows sleep (microphone reacquired) where practical.
- Optional, explicit, user-level start-with-Windows (no admin, no installer).
- Status (runtime state, voice state, startup time, last error, microphone
  active) and lifecycle logging via the existing logging system.
- Engine/microphone failures surface as ERROR without killing the tray.
- Only `JARVIS_RUNTIME_ENABLED` and `JARVIS_TRAY_ENABLED` added to the
  existing `Settings`.

### Non-functional requirements

- Runtime contains no LLM/memory/integration logic; the Phase 1 state machine
  is unchanged apart from two lifecycle hooks.
- New dependencies limited to `pystray` and `Pillow` (tray).
- Deterministic tests with fakes, clearly separated from manual Windows checks.

### Explicitly out of scope for Phase 2

Installer/packaging, Windows service, IPC/remote control, and everything
listed as out of scope for Phase 1 (multi-turn conversation, agent,
memory, integrations, dashboard, ...).

## Environment requirements

- Python 3.11+
- PostgreSQL (for database connectivity verification; not required to run
  the test suite or the `/health` endpoint)
- A Windows desktop session (for the tray; `JARVIS_TRAY_ENABLED=false` runs headless)
- Windows microphone and speaker devices (for the voice pipeline;
  `pytest` itself does not require them)
- Ollama, installed and running locally with a pulled model, for real LLM
  responses (unit tests mock this; only `tests/integration` needs it)
- See `requirements.txt` for Python dependencies, and
  `docs/voice-system.md` for voice-specific model downloads.
