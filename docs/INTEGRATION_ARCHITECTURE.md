# Integration Hub architecture (Phase 18)

```
                    JARVIS agent / IntelligenceRouter / HubRouter
                                     │  (spoken request → deterministic pattern)
                                     ▼
                     HubTools.call(name, args)      ← the only door to an integration
              schema validation → registry gate (enabled? connected? permission granted?)
                                     │
                     ┌───────────────┴───────────────┐
                     ▼                               ▼
             IntegrationRegistry              SyncEngine (+ SyncRunner thread)
     enabled · permissions · status ·         cursor · backoff · rate limit · idempotent
     last sync · last error · cursor                 │
                     │                               ▼
                     ▼                     Adapter.sync() → NormalizedItem
     Adapters (wrap the EXISTING services)             │
   Gmail  Calendar  GitHub  Messaging  Documents       ▼
        │      │       │        │          │     HubRepository (hub_items, SQLAlchemy)
        ▼      ▼       ▼        ▼          ▼           │
   GmailService … CalendarService … GitHubClient … MessagingService … RagService
                                                       ▼
                     EventBus (EMAIL_RECEIVED, CALENDAR_EVENT_*, GITHUB_ACTIVITY_RECEIVED, DOCUMENT_INDEXED, INTEGRATION_*)
                                                       ▼
        SnapshotCollector.hub → Personal Context Engine → findings / plans / answers → Notification Center
```

## Modules (`integrations/hub/`, `integrations/*/adapter.py`)

| Module | Role |
|---|---|
| `hub/models.py` | `IntegrationStatus`, `ErrorKind`, `classify_error`, `Permission`, `NormalizedItem`, `ItemKind`, `ToolResult` |
| `hub/registry.py` | `IntegrationAdapter` contract, `IntegrationRegistry` (persisted user settings + derived status), `UnconfiguredAdapter` |
| `hub/repository.py` | idempotent `upsert`, search, removed/revive, purge, retention (`backend/models/hub.py`, Alembic `0007_hub`) |
| `hub/sync.py` | `SyncEngine` (one integration's sync), `SyncRunner` (background thread) |
| `hub/tools.py` | `HubTools`: the tool router (validation, permission gate, normalized results, cache fallback) |
| `hub/hub.py` | `IntegrationHub` facade + the feed into the bus/notifications/context |
| `gmail/adapter.py`, `gmail/analysis.py` | Gmail adapter; topic, importance, deadline/event/registration extraction |
| `calendar/adapter.py` | normalization, diff sync, conflict/duplicate facts, verified writes |
| `github/{models,client,auth,adapter}.py` | **new**: read-only client, token store + device flow, adapter, project↔repository associations |
| `messaging/adapter.py` | wraps the Telegram provider; documents the unsupported platforms |
| `documents/adapter.py` | watched folders → existing RAG index, incremental |
| `backend/core/integration_switch.py` | the global on/off switch every service consults |
| `backend/core/secrets.py` | DPAPI encryption at rest |
| `agent/intelligence/hub_router.py` | spoken requests about integrations |

## Design decisions

* **Wrap, don't rewrite.** Gmail/Calendar/Telegram/RAG keep their clients, parsers, tools and tests. The hub adds the missing common layer. `integrations/base.py` was **not** turned into a package (much code imports it); the contract lives in `integrations/hub/`.
* **Adding or removing an integration** = registering or dropping one `IntegrationAdapter` (see `desktop/runtime/composition.py::_build_hub`). The agent, sync engine, tool router and dashboard are generic. Operations an adapter does not implement are reported unsupported (`adapter.supported`), never faked.
* **One switch.** The registry installs itself as `integration_switch`; `voice/bootstrap.py` builds the Gmail/Calendar services and the Telegram token source so they also ask it. Switching an integration off therefore stops the Phase 10-15 tools, the intelligence layer, briefings, proactive checks and the sync engine at once (tested).
* **Placeholders are honest.** Integrations that are not enabled in configuration are registered as `UnconfiguredAdapter`, so "Is GitHub connected?" answers "not set up" with the setting to change.
* **Status is derived, never assumed** (`IntegrationRegistry.info`): DISABLED → AUTHENTICATING → DISCONNECTED (not set up / needs you) → SYNCING → DEGRADED (network, rate limit, server; retrying) → ERROR → HEALTHY (last sync succeeded) → CONNECTED (never synced).
* **Permissions.** Reads are granted by default; `CREATE/UPDATE/DELETE_EVENT`, `READ_ATTACHMENT`, `INDEX_DOCUMENTS` are opt-in (`OPT_IN_PERMISSIONS`). The gate is `registry.allowed(name, permission)`. Every write additionally needs the user's confirmation (below).
* **Writes** only exist behind `ConfirmationEngine` → `PlanExecutor` → read-back verification. The tool router has no write tools (tested).
* **Untrusted text** (email, commit messages, issue titles, messages, documents) is sanitized, size-bounded, scanned for injection, and only quoted as data; replies stay out of the LLM history.
* **Local-first.** Normalized items are stored locally (title, ≤240-character summary, small metadata; never whole emails/documents); the intelligence layer and hub tools make no LLM call.

## What feeds the Personal Context Engine

Gmail and Calendar continue to reach it through the `SnapshotCollector` (unchanged, extraction cached). New via `hub.external_items()`: GitHub repositories/commits/open issues/open PRs (entities `REPOSITORY`, `COMMIT`, `ISSUE`, `PULL_REQUEST`, linked to the repository and to a project **only** by your explicit association or a project name in the title), dates found in Telegram messages (deadlines/events with provenance `message`), and hackathon registrations found in email. Items from a switched-off integration are not read even from the local store.

## Not covered

See `PHASE_18_IMPLEMENTATION.md` §Limitations.
