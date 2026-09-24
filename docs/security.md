# Security Model

## Core principle

The LLM must never have unrestricted access to the operating system, the
filesystem, the network, or any external account. All action flows through
a fixed chain:

```
LLM
 ↓
PermissionManager   (backend/core/security/)
 ↓
Tool                (agent/tools/base.py)
 ↓
External System     (integrations/, desktop/, ...)
```

The LLM proposes an action (e.g. "send this email"). The agent brain turns
that into a structured decision, the decision becomes a `PermissionRequest`
held by the manager, and a tool runs only after the manager authorizes exactly
that call (`Tool.execute` -> `PermissionManager.check`). A tool
implementation must never be called directly by LLM output, and must never
reach an external system without going through this check first.

## Phase 5 update

Phase 0's deny-everything placeholder has been replaced by a real permission
layer (typed requests, risk levels, scopes, policy, approval, expiry, action
binding, session scoping and an in-memory audit trail). It is still deny by
default: unknown tools, unknown/expired/mismatched/malformed requests and any
security failure are denied. See `docs/security-and-permissions.md`. The Phase 0
description below is kept for history.

## Phase 0 implementation

- `backend/core/security` (a module in Phase 0, now a package that still exports
  `PermissionManager`, `PermissionRequest` and `PermissionDenied`).
- The default policy is **deny by default**: in Phase 0 `authorize()` always
  returned `False`. (Phase 5 keeps that for anything without an approved,
  matching record.)
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

- Consent prompts / a permission UI (Phase 21) and persistent audit storage.
  (Per-tool rules, scopes and an in-memory audit trail exist since Phase 5.)
- A credential vault. Gmail and Google Calendar keep OAuth tokens in local, git-ignored files (never logged or sent to the
  model; see docs/google-calendar-integration.md); messaging and other integrations do not exist yet.
- Sandboxing or process-level isolation for tool execution.

These are expected to be built out once concrete tools and integrations
exist in later phases, on top of the `PermissionManager` boundary
established here.
