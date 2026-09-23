# Architecture

## Purpose

JARVIS is a persistent, voice-controlled, AI-powered personal digital
assistant for Windows. This document describes the architecture as of
**Phase 7** (personal RAG) on top of **Phase 6** (personal memory), **Phase 5** (permission and security), **Phase 4** (agent brain), **Phase 3** (conversation engine), **Phase 2** (Windows runtime), **Phase 1** (voice engine) and the **Phase 0** foundation and the
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
`decision -> PermissionManager -> Tool` path is a later phase. No real tools
exist. See `docs/agent-brain.md`.

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
│   ├── tools/            Tool interface + ToolDescriptor (no concrete tools yet)
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
| Agent         | Custom brain/planner (LangGraph/LangChain not used) | AgentBrain + Planner implemented (Phase 4, decisions only); no tools, no execution |
| LLM           | Ollama/local first, provider abstraction | `LLMProvider` (chat, json_mode hint) + `OllamaProvider` implemented |
| Database      | PostgreSQL, SQLAlchemy, Alembic      | Engine/session + Alembic; `personal_memories` (Phase 6), `rag_documents`, `rag_chunks` (Phase 7) |
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
