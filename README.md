# JARVIS

A persistent, voice-controlled, AI-powered personal digital assistant for
Windows.

## Current status: Phase 0 — Foundation & Architecture

This repository currently contains only the Phase 0 foundation: project
structure, configuration, logging, a minimal FastAPI health endpoint,
PostgreSQL connectivity infrastructure, and architectural interfaces for
every future component. **No voice, agent reasoning, memory, integrations,
desktop packaging, or frontend functionality is implemented yet.**

See `docs/architecture.md` for full scope and rationale, and
`docs/requirements.md` for what Phase 0 does and does not cover.

## Technology stack

| Layer      | Technology |
|------------|------------|
| Backend    | Python, FastAPI |
| Agent      | LangGraph / LangChain (planned) |
| LLM        | Ollama/local first, provider-abstracted |
| Database   | PostgreSQL, SQLAlchemy, Alembic |
| Voice      | Wake-word engine, Whisper/Faster-Whisper, TTS (planned) |
| Frontend   | React, Tailwind CSS (planned) |
| Desktop    | Windows background app, system tray (planned) |

## Architecture overview

```
backend/        FastAPI application (API, core, models, services)
agent/          Agent boundary: planner, memory, tools, orchestrator
voice/          Voice pipeline boundary: wakeword, stt, tts
integrations/   External-service boundary: gmail, calendar, messaging, ...
desktop/        Windows shell: launcher, tray, service
frontend/       React/Tailwind dashboard
database/       Alembic migrations
tests/          Automated tests
docs/           Architecture, requirements, security, development docs
scripts/        Operational scripts
```

Every future component (LLM provider, tool, integration, memory backend,
voice engine) is defined behind an abstract interface so it can be
implemented and swapped later without rewriting the core. Full detail in
[`docs/architecture.md`](docs/architecture.md).

## Setup

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy .env.example .env
uvicorn backend.main:app --reload
```

Verify it's alive:
```powershell
curl http://127.0.0.1:8000/health
```

Full instructions: [`docs/development.md`](docs/development.md).

## Security model

The LLM never has unrestricted OS access. Every action flows through a
permission boundary before reaching a tool or external system:
`LLM -> PermissionManager -> Tool -> External System`. Details in
[`docs/security.md`](docs/security.md).

## Roadmap

Phase 0 (this repository) establishes the foundation. Later phases —
voice, agent execution, memory/RAG, integrations (Gmail, Calendar,
messaging), desktop packaging, and the frontend dashboard — are described
in the JARVIS master project specification and are **not** implemented
here. Do not assume any capability beyond `GET /health` and database
connectivity checking currently works.
