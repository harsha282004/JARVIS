# Local Development

## Prerequisites

- Python 3.11+
- PostgreSQL (optional for Phase 0 — only needed to exercise real DB
  connectivity; the test suite and `/health` endpoint do not require it)

## Setup

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy .env.example .env
```

Edit `.env` with your local values, in particular `DATABASE_URL` if you
have PostgreSQL running.

## Running the API

```powershell
uvicorn backend.main:app --reload --host 127.0.0.1 --port 8000
```

Then verify:
```powershell
curl http://127.0.0.1:8000/health
```
Expected response:
```json
{"status": "ok", "service": "JARVIS"}
```

## Checking database connectivity

```powershell
python scripts/check_db.py
```

This prints `Database connection: OK` or `Database connection: FAILED`
without ever faking success.

## Running tests

```powershell
pytest
```

Tests are deterministic and do not require a live PostgreSQL instance —
`tests/conftest.py` sets a default `DATABASE_URL` for settings validation,
and `test_database.py` only checks that the connectivity check behaves
correctly (returns a bool), not that a real database is reachable.

## Voice pipeline (Phase 1)

See `docs/voice-system.md` for full setup (model downloads, Ollama, and
the manual end-to-end test). Quick start once models/Ollama are ready:

```powershell
python scripts/run_voice.py
```

Voice-specific tests:
```powershell
pytest                     # unit tests, no hardware/models/network required
pytest tests/integration   # real providers; self-skips whatever isn't configured
```

## Project layout

See `docs/architecture.md` for full directory responsibilities.

## Conventions

- No business logic in FastAPI route handlers (`backend/api/routes/`) —
  put it in `backend/services/` as that layer grows.
- New architectural interfaces go in the relevant package's `base.py`
  (e.g. `agent/tools/base.py`), following the existing `LLMProvider` /
  `Tool` / `Integration` / `MemoryInterface` / `VoiceProvider` pattern.
- Keep configuration centralized in `backend/core/config.py` — don't read
  `os.environ` directly elsewhere.
