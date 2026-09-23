# Architecture

## Purpose

JARVIS is a persistent, voice-controlled, AI-powered personal digital
assistant for Windows. This document describes the architecture as of
**Phase 1** (voice engine), built on the **Phase 0** foundation and the
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

## Directory responsibilities

```
JARVIS/
├── backend/            FastAPI application: API, core, models, services
│   ├── api/             HTTP route definitions (thin — no business logic)
│   ├── core/            config, logging, database, security, LLM interface
│   ├── models/          SQLAlchemy declarative base (no domain models yet)
│   └── services/        business logic layer (empty — populated by later phases)
│
├── agent/               Agentic reasoning boundary
│   ├── planner/          turns a goal into steps (no implementation yet)
│   ├── memory/           MemoryInterface (no storage backend yet)
│   ├── tools/            Tool interface (no concrete tools yet)
│   └── orchestrator/     drives planner output through tools + memory (no implementation yet)
│
├── voice/               Voice pipeline: audio I/O, wakeword/, stt/, tts/ providers,
│                         and engine.py (VoiceEngine orchestrator) — see docs/voice-system.md
├── integrations/        External-service boundary (gmail/, calendar/, messaging/,
│                         github/, browser/, documents/) — interfaces only
├── desktop/              Windows shell (launcher/, tray/, service/) — not implemented
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

| Concern       | Choice                              | Status in Phase 0 |
|---------------|--------------------------------------|--------------------|
| Backend       | Python, FastAPI, WebSockets          | FastAPI app + `/health` only; no WebSocket endpoint yet |
| Agent         | LangGraph, LangChain, custom tools   | Boundary/interfaces only; no dependency added yet |
| LLM           | Ollama/local first, provider abstraction | `LLMProvider` interface only; no provider implemented |
| Database      | PostgreSQL, SQLAlchemy, Alembic      | Engine/session + Alembic env configured; no schema |
| Vector storage| pgvector or similar                  | Not set up yet — deferred until memory/RAG phase |
| Embeddings    | SentenceTransformers                 | Not added yet |
| Voice         | wake-word engine, Whisper/Faster-Whisper, TTS | Implemented: openWakeWord, Faster-Whisper, Piper (see docs/voice-system.md) |
| Frontend      | React, Tailwind CSS                  | Not scaffolded yet |
| Desktop       | Windows background app, tray, startup | Not implemented yet |

LangGraph/LangChain and voice/ML dependencies are deliberately **not**
added to `requirements.txt` yet — adding them before they're used would
violate the "no unnecessary dependencies" principle for this phase.

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
all added in Phase 1, see below. Still not implemented as of Phase 1: Gmail,
Google Calendar, WhatsApp/messaging, YouTube, Spotify, browser automation,
desktop automation, Windows tray/startup, remote access, personal memory,
personal RAG, a knowledge graph, task/reminder management, proactive
intelligence, research mode, vision, multi-agent orchestration, the React
dashboard, and production packaging. These are deferred to later phases
per the JARVIS master specification.

## What Phase 1 intentionally does not implement

Multi-turn conversation, conversation history/session state, memory of any
kind, LangGraph/planner/tool execution, and every integration/desktop/
frontend item listed above. See `docs/voice-system.md` for the full Phase 1
non-goal list.
