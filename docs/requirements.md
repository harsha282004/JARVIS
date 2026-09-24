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

## Phase 12 — Google Calendar Integration

### Functional requirements

- Shared Google OAuth (desktop flow, least-privilege scopes `calendar.events` and `calendar.calendarlist.readonly`), separate
  token, refresh, revoked/unavailable handling; nothing hardcoded; credentials and tokens git-ignored.
- A `CalendarClient` abstraction and JARVIS-owned models; list calendars, read/search events, details, create (with duration,
  all-day, location, recurrence, explicit-email attendees, time zones), update, cancel.
- Strict AgentBrain calendar actions; the model can never supply ids, URLs, tokens, RRULEs or invitation settings.
- Reads LOW/no approval; create/update/cancel MEDIUM with the user's spoken yes bound to the exact event; unknown tools denied.
- Phase 11 conflict detection reused (reported, never fixed); bounded Phase 11 mapping, no second event model, no migration.
- `JARVIS_CALENDAR_ENABLED`, `JARVIS_CALENDAR_CREDENTIALS_PATH`, `JARVIS_CALENDAR_TOKEN_PATH`, `JARVIS_CALENDAR_MAX_RESULTS`.

### Non-functional requirements

- Calendar text is untrusted data: sanitized, never shown to the brain, kept out of history; no event text, attendees or
  tokens in logs; no invitation emails; no new dependencies.

### Explicitly out of scope for Phase 12

Messaging, proactive intelligence, daily briefing, autonomous scheduling/rescheduling, email replies, dashboard, and
everything out of scope earlier.

## Phase 11 — Event & Deadline Intelligence

### Functional requirements

- A persistent `Event` model (PostgreSQL, migration `0005`) for events and deadlines with typed type/status enums,
  timezone-aware UTC timestamps, all-day handling, priority (Phase 9 scale, never guessed), source provenance and
  extraction confidence; deadlines kept distinct (due time, no start) and related to tasks by reference, not duplication.
- `EventService`: create, get, update, complete, cancel, confirm, upcoming/overdue/scoped lists, search, ambiguity
  detection, source and task association; a repository abstraction for persistence.
- Date resolution reusing the Phase 9 parser; ambiguous or vague dates are asked about, never guessed.
- Deterministic extraction from Gmail (Phase 10), indexed documents (Phase 7) and explicit memories (Phase 6) only when the
  user asks; provenance kept; low-confidence results wait for confirmation; duplicate protection by source.
- Bounded temporal reasoning (today, tomorrow, this/next week, next 7 days, overdue, countdowns) on the user's timezone.
- Informational conflict detection (overlap, all-day); nothing is rescheduled.
- Controlled Knowledge Graph links (`PROJECT/GOAL -> HAS_DEADLINE -> EVENT`, `EVENT -> DOCUMENTED_IN -> DOCUMENT`,
  `PERSON -> RELATED_TO -> EVENT`) to existing entities only, with provenance.
- AgentBrain event actions (`event_create/list/search/get/complete/cancel/update/extract`) validated strictly; the model
  cannot supply ids; permission-gated tools (reads and creation low-risk, update/cancel/extract need the user's yes).
- `JARVIS_EVENTS_ENABLED`, `JARVIS_EVENT_DEFAULT_LOOKAHEAD_DAYS`, `JARVIS_EVENT_MAX_RESULTS`.

### Non-functional requirements

- External text (email, documents, memory) is untrusted data: never an instruction, never shown to the brain, never kept in
  history; nothing executable; only short evidence stored; no content in logs.
- No new dependencies; no calendar, network, notification or scheduler code in the events layer.

### Explicitly out of scope for Phase 11

Google Calendar and any calendar sync, proactive notifications, daily briefing, productivity intelligence, automation,
remote access, a dashboard, autonomous scheduling or rescheduling, and everything out of scope earlier.

## Phase 10 — Gmail Intelligence

### Functional requirements

- Gmail OAuth 2.0 for a local desktop app with the least-privilege `gmail.readonly` scope; token storage, refresh,
  revoked-authorization detection and a clear setup error when credentials are missing.
- A `GmailClient` abstraction, JARVIS-owned message/thread/attachment-metadata models, robust MIME/HTML body
  extraction, chronological threads.
- Bounded, whitelist-validated search (Gmail operators), pagination, no-result and ambiguity handling.
- Deterministic classification (important, action required, informational, promotional, personal, unknown) and
  grounded summaries (message, thread, action items) using the local LLM.
- Strict AgentBrain Gmail actions (`gmail_search`, `gmail_get_message`, `gmail_get_thread`, `gmail_summarize`,
  `gmail_classify`); the model can never supply ids, URLs, tokens, paths or commands; messages are identified by code.
- Read-only, PermissionManager-gated tools; unregistered Gmail tools stay denied.
- Bounded retries and backoff for rate limits and outages, with speakable errors.
- `JARVIS_GMAIL_ENABLED`, `JARVIS_GMAIL_CREDENTIALS_PATH`, `JARVIS_GMAIL_TOKEN_PATH`, `JARVIS_GMAIL_MAX_RESULTS`
  (plus `GMAIL_CLIENT_ID` / `GMAIL_CLIENT_SECRET`); credentials and tokens git-ignored.

### Non-functional requirements

- Email is untrusted data: delimited and sanitized for the summarizer, never shown to the brain, never kept in
  history; it cannot invoke tools, change permissions or reach the filesystem.
- No email content, tokens or secrets in logs; no mailbox persistence; local Ollama only.
- New dependencies: `google-auth`, `google-auth-oauthlib` (and `requests`).

### Explicitly out of scope for Phase 10

Sending, deleting, modifying, labelling or archiving mail, attachment download/RAG, Calendar, messaging platforms,
proactive intelligence, daily briefing, automation, remote access, a dashboard, and everything out of scope earlier.

## Phase 9 — Task & Reminder Engine

### Functional requirements

- Persistent `Task` and `Reminder` models (PostgreSQL, Alembic migration `0004`) with typed statuses
  (task: PENDING, IN_PROGRESS, COMPLETED, CANCELLED, OVERDUE; reminder: SCHEDULED, TRIGGERED, CANCELLED,
  EXPIRED) and priorities (LOW..CRITICAL); tasks and reminders are separate, a reminder may reference a task.
- `TaskService`/`ReminderService`: create, get, list, update, complete, cancel, reopen (explicit), delete
  (code only), due lookup; validated state transitions; transactional task-plus-reminder creation.
- Natural-language times (tomorrow at 9 AM, in 30 minutes, today at 6 PM, next Monday at 8 AM) as explicit
  timezone-aware datetimes in `JARVIS_TIMEZONE`; UTC storage.
- Structured daily, weekly and monthly recurrence as one row that advances; cancelling stops it.
- A `ReminderScheduler` thread integrated with the Windows runtime; duplicate-trigger protection; a
  documented missed-reminder policy; clean start and stop.
- A `NotificationService` abstraction with a local desktop (tray) notification and a spoken announcement
  through the VoiceEngine.
- AgentBrain emits a validated structured task/reminder action; execution goes
  PermissionManager -> Tool -> service -> database. Ambiguous targets are clarified, never guessed.
- Task/reminder queries (today, overdue, upcoming, incomplete, next reminder) with a documented sort order.
- `JARVIS_TASKS_ENABLED`, `JARVIS_REMINDERS_ENABLED`, `JARVIS_TIMEZONE`, `JARVIS_REMINDER_POLL_SECONDS`,
  `JARVIS_MISSED_REMINDER_POLICY`, `JARVIS_DEFAULT_TASK_PRIORITY` (and two channel switches).

### Non-functional requirements

- Local only; no cloud task service, external messaging or analytics; logs carry ids and statuses, never
  task or reminder text.
- The database being down never breaks JARVIS and never produces a false "created"; the scheduler recovers.
- The model cannot supply ids or SQL; every mutation passes the PermissionManager; unknown tools stay denied.
- New dependencies: `dateparser`, `tzdata` (timezone database for Windows), `tzlocal`.

### Explicitly out of scope for Phase 9

Calendar, Gmail, WhatsApp/external messaging, proactive intelligence, daily briefing, browser or desktop
automation, remote JARVIS, a dashboard, notification preferences, and everything out of scope earlier.

## Phase 8 — Personal Knowledge Graph

### Functional requirements

- Typed entities (PERSON, PROJECT, TECHNOLOGY, ORGANIZATION, DOCUMENT, SKILL, GOAL, LOCATION,
  TOPIC) and relationships from a controlled vocabulary with allowed type combinations, stored
  relationally (`kg_entities`, `kg_relationships`, `kg_provenance`, foreign keys, Alembic migration).
- Deterministic canonicalization and deduplication of entities; one relationship per
  (source, type, target) with multiple provenance rows; no aggressive or semantic merging.
- Provenance (memory id; document id, name, page, chunk), confidence (LOW/MEDIUM/HIGH) and trust
  (INFERRED / VERIFIED_SOURCE / EXPLICIT_USER); inferred facts never override higher-trust ones.
- Memory to graph and document to graph integration; deleting/superseding a memory or
  deleting/re-indexing a document invalidates the facts that depended on it alone.
- Structured, validated LLM extraction; unknown types, hallucinated entities and malformed output are
  rejected and nothing is partially written.
- Queries: entity lookup, related entities with type filters, provenance, bounded path search with
  cycle protection; relevant facts given to the LLM as a delimited untrusted block.
- Conflicts preserve history and provenance; `valid_from`/`valid_until`/status distinguish current
  from historical facts.
- `JARVIS_KG_ENABLED`, `JARVIS_KG_MAX_PATH_DEPTH`, `JARVIS_KG_MAX_RESULTS`, `JARVIS_KG_MIN_CONFIDENCE`.

### Non-functional requirements

- Graph content is untrusted data: it cannot execute tools, change permissions, policy or prompts'
  rules; all mutations go through `GraphService`; no SQL from model output.
- Local only; no document text in logs; graph failures never break the conversation or invent facts.
- No new dependencies; the graph does not replace or write to memory or RAG.

### Explicitly out of scope for Phase 8

A graph database, visualization/UI, tasks/reminders, calendar/email/messaging graphs, automatic
extraction on every ingest or turn, and everything out of scope earlier.

## Phase 7 — Personal RAG

### Functional requirements

- Local ingestion of TXT, Markdown and PDF documents: validate, SHA-256 hash, extract
  (PDF pages preserved), deterministic chunking (configurable size/overlap), local
  embeddings, storage in PostgreSQL (`rag_documents`, `rag_chunks`, Alembic migration).
- Content-hash identity: unchanged files are skipped, identical content under another
  filename is recognized as a duplicate, changed files are re-indexed with no stale
  chunks, and a failed re-index keeps the previous valid index.
- Document statuses PENDING/PROCESSING/INDEXED/FAILED/DELETED with a content-free
  failure reason; size and chunk limits reject rather than partially index.
- Semantic retrieval: top-k, relevance threshold, results with chunk/document ids,
  score, filename and page; no raw vectors exposed.
- Grounded answers with source references; a controlled insufficient-context reply
  when nothing relevant is found; no grounded claim when the LLM or retrieval fails.
- Agent Brain `document_question` intent with a standalone query; the conversation
  history stays owned by `ConversationEngine`.
- Document deletion/reindex API that never touches the original file.
- `JARVIS_RAG_*` settings in the existing `Settings`.

### Non-functional requirements

- Local only: no cloud upload, external embedding API or telemetry; no document text in logs.
- Retrieved text is untrusted data: delimited, sanitized, never able to trigger tools,
  approvals, memory writes or policy changes; the LLM cannot ingest or delete documents.
- Separate from personal memory; RAG results are never stored as memory.
- New dependencies limited to `pypdf` and `sentence-transformers`.

### Explicitly out of scope for Phase 7

Hybrid retrieval, reranking, OCR, DOCX, a document UI, pgvector/ChromaDB, Gmail/WhatsApp/
Calendar ingestion, web crawling, the knowledge graph, and everything out of scope earlier.

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
