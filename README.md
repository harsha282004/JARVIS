# JARVIS

A persistent, voice-controlled, AI-powered personal digital assistant for
Windows.

## Current status: Phase 1 — Voice Engine

Phase 0 (foundation) is complete, and Phase 1 adds a functional local
voice pipeline: say "Hey JARVIS", ask a question, get a spoken answer from
a local LLM via Ollama. **No agent reasoning/tools, memory, personal RAG,
integrations (Gmail, Calendar, messaging, ...), desktop packaging, or
frontend functionality is implemented yet**, and there is no multi-turn
conversation — each activation is a single, independent exchange.

See `docs/architecture.md` for full scope, `docs/voice-system.md` for the
voice pipeline specifically, and `docs/requirements.md` for what each
phase does and does not cover.

## Technology stack

| Layer      | Technology |
|------------|------------|
| Backend    | Python, FastAPI |
| Agent      | LangGraph / LangChain (planned) |
| LLM        | Ollama (local), provider-abstracted — implemented (`OllamaProvider`) |
| Database   | PostgreSQL, SQLAlchemy, Alembic |
| Voice      | openWakeWord (wake word), Faster-Whisper (STT), Piper (TTS) — implemented |
| Frontend   | React, Tailwind CSS (planned) |
| Desktop    | Windows background app, system tray (planned) |

## Architecture overview

```
backend/        FastAPI application (API, core, models, services)
agent/          Agent boundary: planner, memory, tools, orchestrator (interfaces only)
voice/          Voice pipeline: audio I/O, wakeword, stt, tts, VoiceEngine — implemented
integrations/   External-service boundary: gmail, calendar, messaging, ... (interfaces only)
desktop/        Windows shell: launcher, tray, service (not implemented)
frontend/       React/Tailwind dashboard (not implemented)
database/       Alembic migrations
tests/          Automated tests (unit + tests/integration)
docs/           Architecture, requirements, security, development, voice-system docs
scripts/        Operational scripts (check_db.py, run_voice.py)
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

## Voice setup

The voice pipeline needs a wake-word model, a Piper TTS voice, and a
running Ollama server before `python scripts/run_voice.py` will work —
full download commands and hardware notes are in
[`docs/voice-system.md`](docs/voice-system.md). Quick version:

```powershell
python -c "from openwakeword.utils import download_models; download_models(['hey_jarvis_v0.1'], target_directory='models/wakeword')"
python -c "from pathlib import Path; from piper.download_voices import download_voice; download_voice('en_US-lessac-medium', Path('models/tts'))"
ollama pull llama3
python scripts/run_voice.py
```

## Known limitations (Phase 1)

- Fixed-duration listening window after "Hey JARVIS" (no end-of-speech
  detection yet).
- No multi-turn conversation, context, or interruption handling.
- JARVIS has no memory, Gmail, Calendar, messaging, or RAG — it will say
  so if asked, rather than inventing an answer.

## Security model

The LLM never has unrestricted OS access. Every action flows through a
permission boundary before reaching a tool or external system:
`LLM -> PermissionManager -> Tool -> External System`. Details in
[`docs/security.md`](docs/security.md).

## Roadmap

Phase 0 established the foundation; Phase 1 (this repository) adds the
voice engine. Later phases — agent execution/tools, memory/RAG,
integrations (Gmail, Calendar, messaging), desktop packaging, and the
frontend dashboard — are described in the JARVIS master project
specification and are **not** implemented here. Do not assume any
capability beyond `GET /health`, database connectivity checking, and the
single-turn voice pipeline described above currently works.
