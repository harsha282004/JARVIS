# JARVIS

A persistent, voice-controlled, AI-powered personal digital assistant for
Windows.

## Current status: Phase 4 — Agent Brain

Phases 0-3 are complete. Phase 4 adds a reasoning layer: for each request
JARVIS classifies the intent (conversation, information, action, clarification,
unsupported), decides whether an action would be needed, and produces a
structured decision with a plan, selected tool names and permission needs.
**It executes nothing and no tools exist yet**: an action request such as
"send an email" gets a plan and an honest "I can't carry out actions like that
yet". Phase 3 made JARVIS multi-turn: follow-up
questions ("Who created it?") are answered using the earlier turns of the
same in-memory conversation session, which ends after an inactivity timeout.
Phase 2 runs
the voice engine as a persistent Windows background app with a system-tray
icon (status, pause/resume, restart, exit), graceful shutdown, sleep/resume
recovery, and optional start-with-Windows. Phase 1 provides a functional local
voice pipeline: say "Hey JARVIS", ask a question, get a spoken answer from
a local LLM via Ollama. **No tool execution, memory, personal RAG,
integrations (Gmail, Calendar, messaging, ...), installer/packaging, or
frontend functionality is implemented yet**, and there is no
persistent memory; conversation history is in memory only and is lost on exit.

See `docs/architecture.md` for full scope, `docs/voice-system.md` for the
voice pipeline, `docs/windows-runtime.md` for the Windows runtime,
`docs/conversation-engine.md` for multi-turn conversation,
`docs/agent-brain.md` for the agent brain, and `docs/requirements.md` for what each
phase does and does not cover.

## Technology stack

| Layer      | Technology |
|------------|------------|
| Backend    | Python, FastAPI |
| Agent      | Custom AgentBrain + Planner (implemented, no execution); LangGraph / LangChain not used |
| LLM        | Ollama (local), provider-abstracted — implemented (`OllamaProvider`) |
| Database   | PostgreSQL, SQLAlchemy, Alembic |
| Voice      | openWakeWord (wake word), Faster-Whisper (STT), Piper (TTS) — implemented |
| Frontend   | React, Tailwind CSS (planned) |
| Desktop    | Windows background app + system tray (pystray) — implemented; installer planned |

## Architecture overview

```
backend/        FastAPI application (API, core incl. LLM + conversation engine, models, services)
agent/          brain + planner (implemented, decision/plan only); tools, memory (interfaces only); orchestrator (empty)
voice/          Voice pipeline: audio I/O, wakeword, stt, tts, VoiceEngine — implemented
integrations/   External-service boundary: gmail, calendar, messaging, ... (interfaces only)
desktop/        Windows runtime: runtime (lifecycle), tray, launcher (startup) — implemented
frontend/       React/Tailwind dashboard (not implemented)
database/       Alembic migrations
tests/          Automated tests (unit + tests/integration)
docs/           Architecture, requirements, security, development, voice-system, windows-runtime, conversation-engine, agent-brain docs
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

## Windows runtime

```powershell
python -m desktop.launcher                    # run with tray icon
python -m desktop.launcher --enable-startup   # optional: start with Windows
python -m desktop.launcher --disable-startup
```

See [`docs/windows-runtime.md`](docs/windows-runtime.md) for the tray menu,
lifecycle, startup integration, troubleshooting and limitations.

## Known limitations

- Fixed-duration listening window after "Hey JARVIS" (no end-of-speech
  detection yet).
- Follow-up listening is a fixed window; no interruption (barge-in) handling.
- Conversation context is in memory only, limited by message count.
- Agent brain: decisions and plans only; every action request is declined
  because no tools exist; classification quality depends on the local model.
- Runtime: no installer or Windows service; no external control besides the
  tray/Ctrl+C; sleep is detected after the fact (see the runtime doc).
- JARVIS has no memory, Gmail, Calendar, messaging, or RAG — it will say
  so if asked, rather than inventing an answer.

## Security model

The LLM never has unrestricted OS access. Every action flows through a
permission boundary before reaching a tool or external system:
`LLM -> PermissionManager -> Tool -> External System`. Details in
[`docs/security.md`](docs/security.md).

## Roadmap

Phase 0 established the foundation, Phase 1 added the voice engine and
Phase 2 the Windows runtime and Phase 3 multi-turn conversation and Phase 4 the agent brain. Later phases — tool execution and permissions, memory/RAG,
integrations (Gmail, Calendar, messaging), packaging, and the
frontend dashboard — are described in the JARVIS master project
specification and are **not** implemented here. Do not assume any
capability beyond `GET /health`, database connectivity checking, and the
multi-turn voice pipeline (run as a tray app) described above currently works.
