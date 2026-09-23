# Security Model

## Core principle

The LLM must never have unrestricted access to the operating system, the
filesystem, the network, or any external account. All action flows through
a fixed chain:

```
LLM
 ↓
PermissionManager   (backend/core/security.py)
 ↓
Tool                (agent/tools/base.py)
 ↓
External System     (integrations/, desktop/, ...)
```

The LLM proposes an action (e.g. "send this email"). The agent's
orchestrator translates that proposal into a `PermissionRequest` and asks
`PermissionManager.authorize()` before any `Tool.run()` is invoked. A tool
implementation must never be called directly by LLM output, and must never
reach an external system without going through this check first.

## Phase 0 implementation

- `backend/core/security.py` defines `PermissionManager`, `PermissionRequest`,
  and `PermissionDenied`.
- The default policy is **deny by default**: `PermissionManager.authorize()`
  currently always returns `False` and logs the denial. No fine-grained
  rules (per-user consent, scopes, prompts, audit trail persistence) exist
  yet — that is a later-phase concern.
- No `Tool` subclasses exist yet, so nothing currently calls
  `PermissionManager`. The boundary is established ahead of the tools that
  will need it.

## Secrets and configuration

- All configuration, including future integration credentials, is sourced
  from environment variables via `backend/core/config.py` (`pydantic-settings`).
- Real secrets must live only in a local `.env` file, which is excluded by
  `.gitignore`. Only `.env.example`, containing placeholder values, is
  committed.
- A required configuration value with no safe default (e.g. `DATABASE_URL`)
  causes the application to fail at startup with a clear error rather than
  substituting a fabricated value.
- Logging (`backend/core/logging.py`) must never be passed secret values —
  log calls throughout the codebase should log identifiers and outcomes,
  not credentials or tokens.

## What is intentionally not implemented yet

- Per-user or per-tool permission rules and consent prompts.
- Persistent audit logging of authorized/denied actions.
- Credential storage/retrieval for real integrations (Gmail, Calendar,
  messaging, etc.) — only placeholder env var names exist in `.env.example`.
- Sandboxing or process-level isolation for tool execution.

These are expected to be built out once concrete tools and integrations
exist in later phases, on top of the `PermissionManager` boundary
established here.
