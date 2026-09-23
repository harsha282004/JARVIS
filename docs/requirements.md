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
  SPEAKING -> WAITING` state machine (repeating per turn since Phase 3), with no
  persisted conversation state.
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

## Phase 6 — Personal Memory

### Functional requirements

- Persistent memories (id, type FACT/PREFERENCE/GOAL/PROFILE/CONTEXT, content,
  source, explicit-or-inferred basis, confidence, status, timestamps, metadata)
  in PostgreSQL via the existing SQLAlchemy setup, with an Alembic migration.
- `MemoryInterface` (store, retrieve, update, delete, search) implemented by
  `MemoryService` over `MemoryRepository`.
- Extraction of explicit statements from the user's words after a completed
  turn; only the extracted note is stored, never the conversation.
- Policy: AUTO_SAVE safe explicit statements; CONFIRM sensitive, inferred or
  low-confidence ones; REJECT secrets. Inferred memories are LOW confidence and
  never auto-saved or allowed to override explicit ones.
- Deduplication, conflict handling (newer explicit statement wins, old kept as
  superseded), explicit correction, soft delete and purge.
- Keyword relevance retrieval; only retrieved memories update `last_accessed_at`.
- Relevant memories reach the AgentBrain/LLM as a delimited, sanitized,
  untrusted block appended after the rules.
- Memory failures never break the conversation and never report false success.
- `JARVIS_MEMORY_ENABLED`, `JARVIS_MEMORY_MAX_RETRIEVAL`,
  `JARVIS_MEMORY_AUTO_SAVE`, `JARVIS_MEMORY_MIN_CONFIDENCE` in the existing `Settings`.

### Non-functional requirements

- Local only; no secrets, audio or transcripts stored; logs carry ids/types/counts only.
- No path from LLM output to memory writes; memory cannot bypass `PermissionManager`.
- Tests isolated from the developer's database. No new dependencies.

### Explicitly out of scope for Phase 6

Document RAG / vector search (Phase 7), knowledge graph, memory UI, LLM-based
inference, and everything out of scope for earlier phases.

## Phase 5 — Permission & Security

### Functional requirements

- Typed `PermissionRequest` (id, tool/action, description, risk, scope,
  session id, timestamps, expiry, status) with statuses PENDING, APPROVED,
  DENIED, EXPIRED, CANCELLED (plus CONSUMED for used one-time approvals) and
  risk levels LOW, MEDIUM, HIGH, CRITICAL.
- Centralized policy: unknown tool, scope not allowed, malformed input or any
  error means DENY; approval is explicit (`approve`/`deny`/`cancel`/`expire`)
  and verified against the original request; only the manager produces APPROVED.
- Approvals are bound to (tool, action, parameter digest, session) and expire;
  ONE_TIME approvals are consumed on first use; SESSION approvals end with the
  conversation session; PERSISTENT needs explicit confirmation.
- `check`/`authorize`/`require` and `Tool.execute` are fail-closed gates.
- Agent action decisions create permission requests; nothing is executed.
- Security audit events (typed) recorded in memory and logged, without
  sensitive content.
- `JARVIS_PERMISSION_DEFAULT_EXPIRY_SECONDS`, `JARVIS_PERMISSION_AUDIT_ENABLED`
  in the existing `Settings`.

### Non-functional requirements

- No fail-open path; no real tool or external side effect.
- Phase 0 `PermissionManager`/`PermissionRequest` usage stays compatible.
- Local only; parameters are never stored or logged, only a digest.
- No new dependencies.

### Explicitly out of scope for Phase 5

Real tools, an approval UI/permission center (Phase 21), persistent permissions
or audit storage, cryptographic signing, and everything out of scope earlier.

## Phase 4 — Agent Brain

### Functional requirements

- `AgentBrain` turns a request (user text, conversation context supplied by
  ConversationEngine, tool descriptors) into a validated `AgentDecision`:
  intent (conversation, information_request, action_request,
  clarification_required, unsupported_request), action_required, plan, response,
  selected tools, requires_permission, confidence, short reasoning summary.
- Action requests get a deterministic `Plan` (prepare / permission / execute
  steps) capped by `JARVIS_AGENT_MAX_PLAN_STEPS`; the plan is never executed.
- Tools are selected by name from `ToolDescriptor`s; unknown tools are recorded
  as missing; permission is assumed required unless known otherwise.
- LLM output is parsed as data and validated; invalid output is retried once,
  then handled by a safe fallback with a structured error. An unavailable LLM
  raises instead of fabricating a reply.
- Replies to action/unsupported requests never claim the action happened.
- `JARVIS_AGENT_ENABLED` and `JARVIS_AGENT_MAX_PLAN_STEPS` in the existing
  `Settings`.

### Non-functional requirements

- No execution path from the LLM to the OS or any external system; the brain
  holds descriptors, not tools, and the existing `PermissionManager` is untouched.
- ConversationEngine remains the only owner of history.
- Logs exclude user text, tool arguments, and model output; no chain-of-thought.
- No new dependencies.

### Explicitly out of scope for Phase 4

Any concrete tool or tool execution, the permission workflow/UI (Phase 5),
memory, RAG, integrations, autonomous execution, and everything out of scope
for earlier phases.

## Phase 3 — Conversation Engine

### Functional requirements

- In-memory `ConversationSession` (UUID id, created_at, last_activity,
  messages, active/ended state) and a typed `Message` model
  (system/user/assistant with timestamp).
- Multi-turn context: each LLM request is system prompt + recent history +
  current user message, built by `ConversationEngine`, so follow-ups work.
- Bounded history via `JARVIS_MAX_CONVERSATION_MESSAGES`; the current user
  message and latest reply are never dropped.
- Inactivity timeout (`JARVIS_CONVERSATION_TIMEOUT_SECONDS`) ends the session;
  `reset()` ends it on demand.
- `LLMProvider.chat(messages)` shared interface; `OllamaProvider` adapts it;
  `generate()` remains as a single-turn wrapper.
- VoiceEngine holds a spoken multi-turn flow (follow-ups without the wake
  word until silence) without changing its voice state machine.
- An LLM failure adds nothing to history and leaves the session usable.

### Non-functional requirements

- Conversation state in memory only: never written to disk/PostgreSQL/logs,
  never sent to external services; logs carry ids, counts and lengths only.
- ConversationEngine has no audio code; VoiceEngine has no history code.
- No new dependencies; no second configuration system.

### Explicitly out of scope for Phase 3

Persistent memory, RAG, agent/planner/tools, integrations, barge-in,
token-exact context budgeting, and everything listed as out of scope for
Phases 1 and 2.

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
