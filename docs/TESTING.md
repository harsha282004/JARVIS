# Testing

```powershell
.venv\Scripts\python.exe -m pytest -q                 # everything (about 1-2 minutes)
.venv\Scripts\python.exe -m pytest tests/intelligence tests/security tests/e2e -q
```

## Layout

```
tests/unit/          redaction, structured logs, event bus, backoff/supervisor, health, state files, preferences, privacy, notifications, metrics, database reliability
tests/intelligence/  extraction, resolution, graph, deadlines, planner, proactive runner, duplicate prevention, the scenario through the router
tests/security/      injection, trust levels, approval policy, confirmation engine, audit log, API auth/host checks, secret scan
tests/desktop/       tray status, power state, application lifecycle, privacy<->runtime wiring, crash recovery, health checks
tests/agent/         ConversationEngine + intelligence (routing order, LLM independence, history hygiene, references)
tests/memory/        persistence, duplicates, supersede with history, secrets refused
tests/voice/         STT/TTS/LLM failure injection, manual activation
tests/integrations/  DOCX (incl. XML bomb), document intelligence, corrupted configuration
tests/e2e/           synthetic day, database/OAuth/calendar failures, malicious document, fuzzed input, restart persistence, sleep/resume
tests/*.py           Phases 0-15 (unchanged except two tray tests and one RAG test updated for the new menu / DOCX)
tests/integration/   real-service tests (need real credentials/servers; skipped otherwise)
tests/intelligence_helpers.py   the harness: REAL services over SQLite + in-memory fake Gmail/Calendar clients
```

## Rules followed

* Synthetic data only (the "JARVIS project review" scenario); no real credentials or mail.
* Tests assert behavior (what is said, what is created, that nothing changed, that results were read back), not that functions exist.
* Time is injected (fixed clock: Thursday 2026-09-24 09:00 IST). Sleeping/backoff is injected.
* The intelligence layer runs with an LLM that **fails the test if called**.
* PostgreSQL variants run only when `JARVIS_TEST_DATABASE_URL` points at a disposable database; otherwise they are skipped (601 skips here).

## Real-process checks (not pytest)

`scripts/e2e_launcher_check.py` launches JARVIS as a separate process with a throw-away state directory and SQLite database, waits for the API, verifies status/health/microphone truthfulness,
sends the graceful stop, and checks the exit code and released port. `scripts/benchmark.py` measures intelligence latency. `scripts/secret_scan.py` scans the repository.

## Results

See `COMBINED_PHASE_16_17_IMPLEMENTATION.md` section 7 for the executed results and section 10 for what was not run.
