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

See "What Phase 0 intentionally does not implement" in
`docs/architecture.md`. In short: voice, agent reasoning/execution, memory,
all integrations, desktop packaging, the frontend, and production
deployment. These are future-phase requirements, tracked against the
JARVIS master specification but not implemented here.

## Environment requirements

- Python 3.11+
- PostgreSQL (for database connectivity verification; not required to run
  the test suite or the `/health` endpoint)
- See `requirements.txt` for Python dependencies.
