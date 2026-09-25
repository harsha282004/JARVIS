# Phase 18 audit (before implementation)

Inspected: the whole repository at the state left by Phases 16+17 (uncommitted working tree, 2110 tests passing, 601 PostgreSQL-only skipped), the
existing `integrations/`, the intelligence layer, notification/health/event code, and the Phase 16/17 docs. Nothing here was assumed from file names.

## 1. Existing integrations

| Integration | Where | State found |
|---|---|---|
| Gmail | `integrations/gmail/` | Read-only (`gmail.readonly`), OAuth desktop flow via shared `google_oauth.GoogleAuthenticator`, HTTP client with 401-refresh-once, 429/5xx backoff and `Retry-After`, parser (multipart, attachment **metadata only**), deterministic classification (important / action_required / promotional / ...), LLM summaries, tools behind PermissionManager. **Real Google not exercised** (fake clients + `httpx.MockTransport`). |
| Calendar | `integrations/calendar/` | Read + create/update/delete with per-call confirmation bound to exact parameters, etag conflict handling, overlap detection, Phase 11 event mapping (`sync.py`). Full listing every call; no incremental token. |
| Messaging | `integrations/messaging/` | Provider abstraction with capability detection; only provider is the official **Telegram Bot API**, read-only. WhatsApp explicitly unsupported. |
| Documents | `agent/rag/` (not under `integrations/`) | TXT/MD/PDF/DOCX ingestion with content-hash skip, chunking, local embeddings, PostgreSQL vector store; CLI `scripts/rag_cli.py`. **No watcher.** `integrations/documents/` is an empty package. |
| GitHub | `integrations/github/` | **Empty package.** Nothing implemented. |
| Browser | `integrations/browser/` | Empty package (out of scope for Phase 18). |

`integrations/base.py` holds a 1-method `Integration` ABC (`is_configured`) used only for tiny marker classes (`GmailIntegration`, `CalendarIntegration`).

## 2. Implemented capabilities that Phase 18 must reuse (not duplicate)

Gmail search/read/classification/action-request detection; Calendar CRUD + confirmation + conflicts; Telegram read; RAG ingest/search; Phase 17
`TextExtractor` (events, deadlines, tasks with provenance, injection scan), context engine, findings (email event missing from calendar, conflicts),
`ConfirmationEngine` + `PlanExecutor` (verified calendar creation), `NotificationCenter`, `HealthMonitor`, `EventBus`, preferences, audit log.

## 3. Incomplete / missing

* No common contract across integrations; each exposes its own `is_configured`/service shape. No registry: nothing can answer "is Gmail connected?" with real status; the Phase 16 `HealthMonitor` has ad-hoc per-integration checks.
* No permission model at integration level (write access exists only per tool call through PermissionManager); no user-level enable/disable of a source; no purge-on-disconnect.
* No normalized item model; each service returns its own models. Provenance exists in the intelligence layer only.
* No sync engine: Gmail/Calendar are read live on demand or on the intelligence runner's poll (`newer_than:7d`, whole window each time). No cursors, no per-integration last-sync/error/retry state, no idempotent persistence.
* No GitHub. No document watcher. No Gmail attachment download. Gmail topics (hackathon, internship, college, ...) and importance levels (CRITICAL..LOW) are not modeled; hackathon registration extraction does not exist.
* Error handling is per-integration exception classes; there is no shared classification (AUTH_ERROR, RATE_LIMIT, ...) for consistent user messages.
* No unified tool router with normalized `{success, source, data, metadata, error}` results.
* Dashboard shows service health only; no integration cards, no connect/disconnect.
* No secure-at-rest token storage: Google token files are plain JSON (git-ignored) in `.jarvis/`.

## 4. Authentication status

Gmail and Calendar: OAuth installed-app flow (loopback redirect, PKCE handled by google-auth-oauthlib), refresh on expiry, revocation surfaces as `*AuthRevoked`. Tokens in `.jarvis/*/token.json` **plaintext**. Telegram: bot token from env or file. GitHub: none. Manual OAuth consent is required for anything real.

## 5. Database dependencies

PostgreSQL for tasks/reminders/events/memory/RAG/graph/notifications (Alembic 0001-0006). Phase 16/17 state (privacy, preferences, audit, timeline, notification history) is local JSON under `.jarvis/`. No table for integration data or sync state. PostgreSQL still unavailable on this machine: new tables can be tested on SQLite only.

## 6. Duplicate functionality to avoid

Do not re-implement Gmail/Calendar clients, classification, extraction, confirmation, notification, or RAG ingestion. The hub **wraps** them in adapters. `integrations/base.py` stays (imports across the codebase depend on it); the new contract lives in `integrations/hub/` rather than renaming `base.py` into a package.

## 7. Integration architecture (target)

```
agent/intelligence router ──> HubTools (permission + enabled check, ToolResult) ──> IntegrationRegistry ──> Adapter ──> existing service/client ──> API
SyncEngine (interval + events, backoff, cursors) ──> Adapter.sync ──> NormalizedItem ──> HubRepository (idempotent upsert, SQLAlchemy) ──> EventBus
NormalizedItem / services ──> SnapshotCollector ──> Personal Context Engine (unchanged pipeline, plus repositories/commits/issues)
```

## 8. Security risks found

1. Plaintext OAuth tokens on disk (git-ignored, user-profile only). Mitigation planned: Windows DPAPI encryption for new secrets (GitHub); Google token files documented as a remaining limitation.
2. No way to switch a source off centrally: mitigated by registry enable/disable enforced in the tool router and collector.
3. Untrusted external text (email/issue/PR/commit messages/documents) must stay data: all new extraction goes through the existing scanner; GitHub text is sanitized and never reaches the agent brain.
4. GitHub tokens are powerful: read-only scopes only; no write tools; token never logged (redaction covers `ghp_`/`github_pat_`).
5. Dashboard write endpoints (connect/disconnect/sync) already sit behind loopback + per-run token; reused.
6. Attachment download is a privacy risk: opt-in only, size-capped, never automatic.

## 9. Plan (executed)

1. `integrations/hub/`: models (status, permissions, error kinds, ToolResult, NormalizedItem), error classifier, adapter contract, registry with persisted user settings.
2. SQLAlchemy `hub_items` + Alembic 0007, repository (idempotent upsert, search, purge, retention).
3. Sync engine: cursors, incremental, backoff/rate-limit, events, idempotent.
4. Adapters: Gmail (topics, importance, deadline/event/hackathon extraction, attachments opt-in), Calendar (normalize, diff sync), GitHub (new: PAT + device flow, DPAPI storage, ETag/rate-limit aware client), Messaging (wrap Telegram; document limits), Documents (folder watcher, incremental).
5. Unified tool router + deterministic conversation patterns; connect/disconnect/enable/disable; dashboard Integration Center.
6. Context engine: repositories/commits/issues and project association; provenance everywhere.
7. Tests (unit, failure, security, e2e synthetic), measurement, docs.
