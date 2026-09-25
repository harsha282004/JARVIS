# Architecture

## Purpose

JARVIS is a persistent, voice-controlled, AI-powered personal digital
assistant for Windows. This document describes the architecture as of
**Phase 11** (event & deadline intelligence) on top of **Phase 10** (Gmail intelligence, read-only), **Phase 9** (tasks and reminders), **Phase 8** (personal knowledge graph), **Phase 7** (personal RAG), **Phase 6** (personal memory), **Phase 5** (permission and security), **Phase 4** (agent brain), **Phase 3** (conversation engine), **Phase 2** (Windows runtime), **Phase 1** (voice engine) and the **Phase 0** foundation and the
directory boundaries all later phases build on. Voice-specific detail
(providers, pipeline, setup) lives in `docs/voice-system.md`; this
document stays the map of the whole codebase.

## Phase 0 scope

Phase 0 establishes:
- project structure and package boundaries
- configuration and logging
- a minimal FastAPI app with `GET /health`
- PostgreSQL connectivity infrastructure (no domain schema)
- architectural interfaces for future components (LLM provider, Tool,
  Integration, Memory, Voice provider)
- the security boundary between the agent and external actions
- documentation and a basic test suite

It does **not** implement voice, agent reasoning, memory, integrations,
desktop packaging, or the frontend. See "What Phase 0 intentionally does
not implement" below and `docs/requirements.md`.

## Phase 1 scope (voice engine)

Phase 1 adds a functional voice pipeline on top of the Phase 0 foundation:
local wake-word detection, local speech-to-text, an Ollama-backed
`LLMProvider` implementation, and local text-to-speech, wired together by
`voice/engine.py`'s `VoiceEngine` state machine. It reuses the Phase 0
config, logging, `LLMProvider` and `VoiceProvider` abstractions without
introducing a second configuration or logging system. It does **not**
implement agent reasoning (LangGraph/planner/tools), memory, multi-turn
conversation, or any integration. See `docs/voice-system.md` for full
detail and `docs/requirements.md` for the Phase 1 non-goals.

## Phase 20 scope (controlled browser agent)

`browser/` (engine, tools, YouTube workflow, router, driver) sits behind `IntelligenceRouter` after the Integration Hub router: voice/text -> conversation -> BrowserRouter -> BrowserTools (schema, category, PermissionManager, ConfirmationEngine) -> BrowserEngine -> Playwright. See `BROWSER_ARCHITECTURE.md`, `BROWSER_SECURITY.md`, `BROWSER_TOOLS.md`. The `voice/` package has no dependency on it.

## Phase 21 scope (autonomous multi-step tasks)

`autonomy/` (planner, tool router, observer/verifier, task runner, manager) sits ahead of the Hub and Browser routers in `IntelligenceRouter`. It plans a goal into steps, runs them through `ToolRouter` -> (`BrowserTools` | Integration Hub tools | local analysis), observes and verifies after each, and reuses the Phase 17 `ConfirmationEngine` for task-level confirmations. It adds no capability: no shell, file, credential or script tool. See `AUTONOMOUS_AGENT_ARCHITECTURE.md`, `AUTONOMOUS_AGENT_SECURITY.md`, `AUTONOMOUS_TASKS.md`.

## Phase 11 scope (event & deadline intelligence)

Phase 11 adds `agent/events/`: a typed `Event` model (events and deadlines, with provenance and extraction confidence),
`EventService` over `EventRepository` (PostgreSQL `events`, migration `0005`), date resolution on the Phase 9 `TimeParser`,
bounded temporal reasoning, informational conflict detection, deterministic extraction from Gmail, indexed documents and
explicit memories (only when the user asks), a controlled Knowledge Graph link (`EVENT`, `HAS_DEADLINE`), and eight
permission-gated tools through the existing executor. It is an internal layer: no calendar integration (Phase 12), no
proactive notifications (Phase 14). See `docs/event-and-deadline-intelligence.md`.

## Phase 10 scope (Gmail intelligence)

Phase 10 adds `integrations/gmail/`: a `GmailClient` interface with an httpx implementation (GET-only, bounded
retries), OAuth 2.0 desktop authentication with the read-only scope, MIME/HTML parsing into JARVIS-owned models,
whitelist-validated search queries, deterministic classification and grounded local-LLM summaries, and five
read-only tools. The AgentBrain may propose a validated `GmailAction` (words only, never ids or URLs); the same
`TaskActionExecutor` puts it through the `PermissionManager` and a `Tool`. Email text is untrusted: it never
reaches the brain and never enters the conversation history. No database table, no mailbox mirror. See
`docs/gmail-intelligence.md`.

## Phase 15 scope (Daily briefing & productivity intelligence)

Phase 15 adds `agent/briefing/`: a `ProductivityCollector` that reads the existing task, reminder, event, calendar, Gmail and messaging services
(each isolated, bounded and read-only) into a `ProductivityContext` of normalized items with source references; a deterministic `PriorityAnalyzer`;
a `Builder` for conflicts, preparation, risks, focus and voice wording; and a `BriefingService` with an optional, grounding-checked LLM rephrasing step.
The AgentBrain only proposes `briefing_generate` / `briefing_explain` (LOW risk, read-only). No table, migration, scheduler or notifier is added; Phase 14 remains the only
notification system. See `docs/daily-briefing-productivity.md`.

## Phase 14 scope (Proactive intelligence)

Phase 14 adds `agent/proactive/`: read-only signal sources over the existing task, event, calendar and Gmail services, a deterministic
`NotificationPolicy` (quiet hours, cooldown, de-duplication, priority/urgency, hourly cap), and a `ProactiveEngine` that runs as an extra pass of
the existing `ReminderScheduler` thread and delivers through the existing `DesktopNotifier`/`VoiceNotifier` (no second scheduler or notifier).
A `proactive_notifications` table (migration 0006) gives an atomic claim (exactly one delivery per signal), cooldown and traceability. A signal is
information only: the engine has no tool executor and no way to modify any source, and uses no language model. The AgentBrain's only involvement
is the read-only `proactive_explain` action. See `docs/proactive-intelligence.md`.

## Phase 13 scope (Messaging)

Phase 13 adds `integrations/messaging/`: capability-based provider interfaces (conversations, messages, search) with a
`ProviderRegistry`, one real provider (the official Telegram Bot API, read-only, `getMe`/`getUpdates` only) and a
`MessagingService`. The AgentBrain may propose a validated `MessageAction` (words only, never ids, providers, URLs or text to
send); the six tools are LOW risk and read-only. Message text is untrusted: never shown to the brain, kept out of history,
summarized only in a delimited tool-less LLM call. Nothing is stored (no table, no migration) and nothing is created from a
message. WhatsApp and personal-account access are not supported. See `docs/messaging-integration.md`.

## Phase 12 scope (Google Calendar)

Phase 12 adds `integrations/calendar/` (a `CalendarClient` interface with an httpx implementation, a `CalendarService`,
seven tools) on the shared `integrations/google_oauth.py` that Gmail now also uses. The AgentBrain may propose a validated
`CalendarAction` (words only, never ids, URLs or RRULEs); reads are LOW risk, create/update/cancel are MEDIUM and need the
user's spoken yes, bound to the exact event. Overlaps use the Phase 11 rules and are reported, never fixed. Events JARVIS
creates or changes are mapped to one Phase 11 `Event` each (bounded read-through, no wholesale mirror, no migration).
No invitations are sent. See `docs/google-calendar-integration.md`.

## Phase 9 scope (tasks and reminders)

Phase 9 adds `agent/tasks/`: typed `Task`/`Reminder`/`Recurrence` models with explicit status enums and a
transition table, `TaskService`/`ReminderService` (rules) over `TaskRepository` (PostgreSQL: `tasks`,
`reminders`, migration `0004`, conditional updates so the scheduler and voice requests cannot race),
natural-language time parsing in the user's `JARVIS_TIMEZONE` (UTC in the database), structured recurrence,
a `NotificationService` abstraction (tray balloon, spoken announcement), and a `ReminderScheduler` thread
started by the Windows launcher. The AgentBrain may propose a validated `TaskAction`; the
`TaskActionExecutor` puts it through the `PermissionManager` and a `Tool` before any service is touched.

```
VoiceEngine -> ConversationEngine -> AgentBrain -> TaskAction (validated data)
     -> TaskActionExecutor -> PermissionManager -> Tool -> TaskService / ReminderService -> PostgreSQL
Windows runtime -> ReminderScheduler -> ReminderService -> PostgreSQL
     -> NotificationService -> tray notification / VoiceEngine (speaks between conversations)
```

Tasks and reminders are separate from memory, RAG and the graph (no automatic links). See
`docs/tasks-and-reminders.md`.

## Phase 8 scope (personal knowledge graph)

Phase 8 adds `agent/knowledge_graph/`: typed `Entity`/`Relationship`/`Provenance` models, a
controlled relationship schema, deterministic canonicalization, `GraphService` (rules) over
`GraphRepository` (PostgreSQL: `kg_entities`, `kg_relationships`, `kg_provenance`, migration `0003`),
validated LLM extraction, listeners that keep memory- and document-derived facts consistent, and a
`GraphContextProvider` that adds a delimited untrusted `<knowledge_graph_context>` block to prompts
alongside memory and RAG context. It is separate from memory and RAG, never writes to them, and
never bypasses `PermissionManager`. See `docs/knowledge-graph.md`.

## Phase 7 scope (personal RAG)

Phase 7 adds `agent/rag/`: document loaders, a deterministic chunker, a local
`EmbeddingProvider` (SentenceTransformers), a `VectorStore` (PostgreSQL tables,
exact cosine search; no pgvector/ChromaDB), a `Retriever` with top-k and a relevance
threshold, grounded prompting with sources, and `RagService` (ingest, reindex, delete,
answer), plus tables `rag_documents`/`rag_chunks` and migration `0002`. The Agent
Brain gained a `document_question` intent; `ConversationEngine` routes those to
`RagService.answer`. Retrieved text is untrusted data, never reaches the brain's decision
call, and cannot trigger tools or memory writes. RAG is separate from Phase 6 memory.
See `docs/personal-rag.md`.

## Phase 6 scope (personal memory)

Phase 6 implements the Phase 0 `MemoryInterface` in `agent/memory/`
(`MemoryService` rules, `MemoryRepository` persistence, rule-based extraction,
safety screening, confirmation policy) with a `personal_memories` table and an
Alembic migration on the existing PostgreSQL/SQLAlchemy setup.
`ConversationEngine` retrieves relevant memories before reasoning, adds them to
the prompt as a delimited untrusted block, and extracts memories from the user's
words after a completed turn. Memory never bypasses `PermissionManager`. Not RAG,
not a knowledge graph, not transcript storage. See `docs/personal-memory.md`.

## Phase 5 scope (permission and security)

Phase 5 replaces `backend/core/security.py` with the package
`backend/core/security/` (typed `PermissionRequest`, `RiskLevel`,
`PermissionScope`, `PermissionStatus`, `PermissionPolicy`, `AuditLog`,
`PermissionManager`), adds risk/scope metadata to `ToolDescriptor`/`Tool`, a
fail-closed `Tool.execute` gate, and `agent/brain/permissions.py`, which turns
action decisions into permission requests (wired through `ConversationEngine`).
Boundary: `AgentDecision -> PermissionManager -> Tool -> external system`; the
last two steps do not exist yet. See `docs/security-and-permissions.md`.

## Phase 4 scope (agent brain)

Phase 4 adds `agent/brain/` (`AgentBrain`, decision models, output
validation) and `agent/planner/` (`Plan`, `Planner`), plus `ToolDescriptor` on
the existing `Tool` interface. Boundary: `VoiceEngine -> ConversationEngine ->
AgentBrain -> LLMProvider`. The brain produces a structured decision (intent,
plan, tool names, permission needs, reply) and executes nothing; the
`decision -> PermissionManager -> Tool` path is a later phase (Phase 9 later adds it for the local task/reminder
tools only). See `docs/agent-brain.md`.

## Phase 3 scope (conversation engine)

Phase 3 adds `backend/core/conversation/` (`ConversationEngine`, session and
message models, system prompt) and a chat-style `LLMProvider.chat(messages)`.
The boundary is `Windows runtime -> VoiceEngine -> ConversationEngine ->
LLMProvider -> OllamaProvider`: VoiceEngine owns audio, ConversationEngine
owns in-memory session/history/context/timeout/reset. No persistence, agent,
memory or tools. See `docs/conversation-engine.md`.

## Phase 2 scope (Windows runtime)

Phase 2 adds `desktop/`: a `RuntimeManager` that owns the VoiceEngine
lifecycle (start/pause/resume/restart/shutdown, error state, status), a
pystray system-tray controller, a sleep/resume watcher, an optional
Startup-folder shortcut, and the `python -m desktop.launcher` entry point.
The boundary is `Windows runtime -> RuntimeManager -> VoiceEngine ->
providers`; the runtime holds no reasoning, memory or integration logic. See
`docs/windows-runtime.md`.

## Directory responsibilities

```
JARVIS/
├── backend/            FastAPI application: API, core, models, services
│   ├── api/             HTTP route definitions (thin — no business logic)
│   ├── core/            config, logging, database, security/ (permissions, policy, audit),
│   │                    llm/ (provider interface, messages),
│   │                    conversation/ (ConversationEngine)
│   ├── models/          SQLAlchemy base + personal_memories table (Phase 6)
│   └── services/        business logic layer (empty — populated by later phases)
│
├── agent/               Agentic reasoning boundary
│   ├── brain/            AgentBrain, decision models, LLM-output validation (Phase 4)
│   ├── planner/          Plan models + deterministic Planner (Phase 4; describes, never runs)
│   ├── memory/           personal memory: interface, service, repository, extraction, safety (Phase 6)
│   ├── rag/              personal RAG: loaders, chunker, embeddings, vector store, retriever, service (Phase 7)
│   ├── knowledge_graph/  entities/relationships/provenance, GraphService, extraction, sync, context (Phase 8)
│   ├── tasks/            tasks, reminders, scheduler, notifications, time parsing, tools, executor (Phase 9)
│   ├── events/           events/deadlines, extraction, temporal reasoning, conflicts, graph links, tools (Phase 11)
│   ├── tools/            Tool interface + ToolDescriptor (the only concrete tools are the Phase 9 task/reminder tools in agent/tasks)
│   └── orchestrator/     will drive plans through PermissionManager + tools (empty; not built)
│
├── voice/               Voice pipeline: audio I/O, wakeword/, stt/, tts/ providers,
│                         and engine.py (VoiceEngine orchestrator) — see docs/voice-system.md
├── integrations/        External-service boundary (gmail/, calendar/, messaging/,
│                         github/, browser/, documents/) — interfaces only
├── desktop/              Windows runtime: runtime/ (RuntimeManager, state, power), tray/, launcher/
│                         (entry point, startup shortcut) — see docs/windows-runtime.md
│                         (service/ is an empty placeholder; no Windows service in Phase 2)
├── frontend/             React/Tailwind dashboard — not implemented
├── database/             Alembic migration environment (no revisions yet)
├── tests/                Automated tests for Phase 0 components
├── docs/                 This documentation
└── scripts/              Small operational scripts (e.g. scripts/check_db.py)
```

## Request flow (Phase 0)

```
HTTP client -> FastAPI (backend/api) -> backend/core -> response
```

No agent, tool, or integration is wired into the API yet. Only `/health`
exists.

## Future request flow (established as a boundary, not implemented)

```
User (voice or UI)
  -> voice/ (wake word, STT)                      [not implemented]
  -> agent/orchestrator                            [not implemented]
       -> agent/planner                            [not implemented]
       -> agent/tools (via backend/core/security)   [not implemented]
            -> integrations/ -> external system     [not implemented]
       -> agent/memory                              [not implemented]
  -> voice/tts / frontend                           [not implemented]
```

The key invariant, enforced structurally from Phase 0 onward: **the LLM
never calls a Tool directly.** See `docs/security.md`.

## Technology decisions

| Concern       | Choice                              | Status |
|---------------|--------------------------------------|--------------------|
| Backend       | Python, FastAPI, WebSockets          | FastAPI app + `/health` only; no WebSocket endpoint yet |
| Agent         | Custom brain/planner (LangGraph/LangChain not used) | AgentBrain + Planner (Phase 4, decisions only); the executor runs only validated Phase 9 task/reminder actions, through the PermissionManager |
| LLM           | Ollama/local first, provider abstraction | `LLMProvider` (chat, json_mode hint) + `OllamaProvider` implemented |
| Database      | PostgreSQL, SQLAlchemy, Alembic      | Engine/session + Alembic; `personal_memories` (Phase 6), `rag_documents`, `rag_chunks` (Phase 7), `kg_entities`, `kg_relationships`, `kg_provenance` (Phase 8), `tasks`, `reminders` (Phase 9), `events` (Phase 11) |
| Vector storage| pgvector or similar                  | PostgreSQL tables + exact NumPy cosine search (Phase 7; pgvector deliberately not required) |
| Embeddings    | SentenceTransformers                 | Implemented (Phase 7, local, all-MiniLM-L6-v2) |
| Voice         | wake-word engine, Whisper/Faster-Whisper, TTS | Implemented: openWakeWord, Faster-Whisper, Piper (see docs/voice-system.md) |
| Frontend      | React, Tailwind CSS                  | Not scaffolded yet |
| Desktop       | Windows background app, tray, startup | Implemented: pystray tray, Startup-folder shortcut (no installer/service) |

LangGraph/LangChain are deliberately **not** added to `requirements.txt`
yet — adding them before they're used would violate the "no unnecessary
dependencies" principle. Voice (Phase 1) and tray (Phase 2) dependencies are
added because those phases use them.

## Replaceability principle

Every future component (LLM provider, tool, integration, memory backend,
voice engine) is defined behind an abstract interface in Phase 0
specifically so it can be swapped without rewriting the core:
- `backend/core/llm/base.py` — `LLMProvider`
- `agent/tools/base.py` — `Tool`
- `agent/memory/base.py` — `MemoryInterface`
- `integrations/base.py` — `Integration`
- `voice/base.py` — `VoiceProvider`

## What Phase 0 intentionally did not implement

Wake word detection, Whisper/Piper, voice conversation, Ollama inference —
all added in Phase 1, see below. Still not implemented as of Phase 2: Gmail,
Google Calendar, WhatsApp/messaging, YouTube, Spotify, browser automation,
desktop automation, installer/packaging, remote access, personal memory,
personal RAG, a knowledge graph, task/reminder management, proactive
intelligence, research mode, vision, multi-agent orchestration, the React
dashboard, and production packaging. These are deferred to later phases
per the JARVIS master specification.

## What Phase 11 intentionally does not implement

Google Calendar or any calendar integration or sync, proactive notifications, reminders created for events, a daily
briefing, automatic or scheduled extraction, automatic rescheduling, recurring or multi-day events, event UI, and everything
in the Phase 10 list below.

## What Phase 15 intentionally does not implement

A scheduled or spoken-on-its-own briefing, any notification, productivity scores or judgments, any change to tasks, events, calendar, email or messages, briefing history,
a settings UI or dashboard, a desktop/coding agent, research or document intelligence, vision, multi-agent, remote/mobile access, media control, and everything in the Phase 14 list below.

## What Phase 14 intentionally does not implement

Autonomous actions of any kind (sending, replying, modifying or rescheduling tasks/events/calendar/email), a daily briefing, productivity intelligence,
reminder or messaging signals, a settings UI, snooze/mute, a desktop/coding agent, research or document intelligence, vision, multi-agent, remote/mobile
access, media control, and everything in the Phase 13 list below.

## What Phase 13 intentionally does not implement

Sending, replying, forwarding, editing, deleting or marking messages, WhatsApp or any personal-account access, background
monitoring or notifications, autonomous replies, automatic tasks/reminders/events/graph entries from messages, attachment
download or indexing, a message cache or mirror, a dashboard, and everything in the Phase 12 list below.

## What Phase 12 intentionally does not implement

Messaging platforms, proactive alerts, a daily briefing, autonomous scheduling or rescheduling, invitations or email replies,
Meet link creation, calendar sharing/creation, push sync, a dashboard, and everything in the Phase 11 list below.

## What Phase 10 intentionally does not implement

Sending, replying, deleting, labelling, archiving or marking mail, attachment download or indexing, a Gmail cache
or mirror, Gmail-derived memory or graph entities, Calendar, WhatsApp/other messaging, proactive alerts, a daily
briefing, automation, a dashboard, and everything in the Phase 9 list below.

## What Phase 9 intentionally does not implement

Google Calendar, Gmail, WhatsApp or any external messaging, proactive intelligence and daily briefings,
browser or desktop automation, remote access, a dashboard or notification-preference UI, sub-tasks,
productivity scoring, task links into memory or the graph, a Windows service (reminders fire only while
JARVIS runs), and everything in the Phase 8 list below.

## What Phase 8 intentionally does not implement

A graph database, graph visualization/UI, automatic extraction on every ingest or turn, temporal
reasoning beyond valid-from/until, negative relationships, and everything in the Phase 7 list below.

## What Phase 7 intentionally does not implement

Hybrid retrieval, reranking, OCR/DOCX, a document-management UI, pgvector/ChromaDB, email,
messaging or calendar ingestion, web crawling, and everything in the Phase 6 list below.

## What Phase 6 intentionally does not implement

Document RAG/vector search, a knowledge graph, LLM-based inference of memories,
a memory UI or voice commands for managing memory, and everything in the Phase 5
list below.

## What Phase 5 intentionally does not implement

Any real tool or execution, an approval UI / permission center, persistent
permissions or audit storage, and everything in the Phase 4 list below.

## What Phase 4 intentionally does not implement

Any tool or tool execution, the permission workflow/UI, autonomous or
multi-step execution, memory, and every integration. The brain classifies and
plans only.

## What Phase 3 intentionally does not implement

Persistent memory or conversation storage, agent/planner/tools, and
everything in the Phase 2 and Phase 1 lists below. Conversation state is
in memory only.

## What Phase 2 intentionally does not implement

Installer/packaging, a Windows service, IPC/remote control of the runtime,
and everything in the Phase 1 list below. The runtime is lifecycle only.

## What Phase 1 intentionally does not implement

Memory of any
kind (conversation history is in-memory only, added in Phase 3), LangGraph/planner/tool execution, and every integration/desktop/
frontend item listed above. See `docs/voice-system.md` for the full Phase 1
non-goal list.

## Phase 22 scope (Personal Operator: autonomous workflows)

`workflows/` coordinates the user's own systems into end-to-end workflows (email → deadline → task → reminder, briefings, meeting preparation, GitHub activity → tasks, document deadlines, "apply from the email"). Operator → workflow runner → operator/browser tool router → Integration Hub gate or `PermissionManager` → tool → read-back verification. Deterministic (no model call); typed data flow with provenance; facts are classified and only VERIFIED/HIGH_CONFIDENCE ones may drive a write; a write-ahead effect ledger makes every side effect idempotent and crash-recoverable; consequential steps need the user's confirmation through the shared `ConfirmationEngine`; conflicts between sources are surfaced, not resolved; nothing is ever sent, replied to, forwarded, deleted, published or purchased. See `PERSONAL_OPERATOR_ARCHITECTURE.md`, `WORKFLOW_ENGINE.md`, `WORKFLOW_SECURITY.md`, `WORKFLOW_TEMPLATES.md`.
