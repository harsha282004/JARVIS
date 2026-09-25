# Phase 18 implementation report: Integration Hub & unified personal data layer

Built on the uncommitted Phase 16+17 working tree. Nothing was committed or pushed. Audit: `PHASE_18_AUDIT.md`.

## Completed

**Implemented and tested (synthetic data, fakes/mocks for every external service):**
Integration contract + registry + permission model + health/status model; error classification (9 kinds); normalized item model with provenance; idempotent SQLAlchemy store + Alembic `0007_hub`; sync engine (cursors, idempotence, paging, diffing, backoff, `Retry-After`, no auto-retry on auth failure) and background runner; tool router with schema validation, permission gate and uniform `ToolResult`; Gmail adapter (topic, importance, deadline/event/registration extraction, attachments opt-in, incremental sync); Calendar adapter (normalization with external ids, diff sync, conflict/duplicate facts, verified create/update/delete); **new GitHub integration** (read-only client with rate-limit budget + ETag, token store, device flow, adapter, project associations); Telegram adapter; document watcher; global integration switch; DPAPI token encryption; dashboard Integration Center + API; spoken hub requests; GitHub/hub data in the Personal Context Engine; health-monitor registration.

**Requires manual configuration (could not be executed here):** Google OAuth consent (Gmail, Calendar), a GitHub token/OAuth App, a Telegram bot, PostgreSQL + `alembic upgrade head` (adds `hub_items`), Ollama for non-hub questions.

**Unsupported (documented, not faked):** WhatsApp personal accounts, personal Telegram chats, Signal, iMessage; Gmail Pub/Sub push and Calendar push channels (need a public HTTPS endpoint).

## Architecture

See `INTEGRATION_ARCHITECTURE.md`. Adapters wrap the existing services; `integrations/base.py` was kept (not converted into a package) because much code imports it; the new contract is `integrations/hub/registry.py::IntegrationAdapter`.

## Authentication

See `OAUTH_SECURITY.md`. Google flows are the Phase 10/12 ones (unchanged, plus `forget()`, `revoke_remote()`, optional DPAPI encryption at rest); GitHub: token (fine-grained, read-only, recommended) or device flow; tokens encrypted with DPAPI, never logged/exposed (tested).

## Synchronization

See `SYNC_ENGINE.md` (per-source cursors, backoff table, intervals).

## Data model

See `DATA_NORMALIZATION.md`. New tables: `hub_items` only.

## Security

* Reads need `enabled + connected + permission`; `CREATE/UPDATE/DELETE_EVENT`, `READ_ATTACHMENT`, `INDEX_DOCUMENTS` are opt-in; the tool router has no write tools; the only write path is ConfirmationEngine → PlanExecutor → read-back (tested: no calendar change without a yes; a vague or hostile "yes" changes nothing; external content cannot confirm).
* Disconnect/revoke need a confirmation naming what will be deleted.
* Untrusted text (email, commit messages, issue titles, messages, documents) is sanitized, bounded, scanned, quoted as data; tested with hostile commit messages, emails and messages.
* Repository names validated before URL use; SQL search parameterized; attachment file names sanitized and confined to the attachments folder; attachments size-capped; DOCX XML bombs refused.
* `python scripts/secret_scan.py` clean; tests assert tokens never appear in logs, status, tool results or API responses.

## Testing (only what was executed)

Full suite: **2251 passed, 601 skipped** (Phase 16+17 ended at 2110 passed; +141). The 601 skips are the PostgreSQL-only variants (no PostgreSQL here). Two older tests were updated: the "latest migration" pin (now `0007_create_hub_items`) and the messaging package file list (adds `adapter.py`). The shared fake Gmail client in `tests/gmail_helpers.py` gained `after:` filtering, paging and attachments.

| TEST | RESULT | STATUS |
|---|---|---|
| `test_hub_core.py`: error classification (18 cases), tool-result shape, permissions/opt-in, status derivation, connect/disconnect/purge, store idempotence/search/revive/retention, **migration up/down on scratch SQLite**, DPAPI round trip + plaintext fallback + corrupted secret, encrypted Google token reload/forget, revoke request | 38 passed | PASS |
| `test_hub_sync.py`: Gmail incremental + paging + provenance, Calendar diff/removal/updates, backoff doubling/recovery, Retry-After, auth failure not retried, events, consumer failure isolation, disabled/unconfigured never called, privacy mode, GitHub normalization/idempotence, document watcher (incremental, hidden/unsupported skipped, parser failure), Telegram extraction, unavailable messaging | 19 passed | PASS |
| `test_gmail_github_units.py`: topics/importance/injection cap, deadline/event/registration/location extraction, attachments (opt-in, path escape, type/size), GitHub client (status mapping, rate-limit budget with zero requests while blocked, Retry-After, 5xx/network, malformed JSON, invalid/missing token, path injection, ETag, hostile text), token store, device flow (pending/slow_down/denied/expired), adapter | 42 passed | PASS |
| `test_hub_tools_e2e.py`: tool validation and shape, permission/switch gating, cache fallback, spoken requests, permission → confirmation → verified create, disconnect flow, switch-off everywhere, GitHub by voice, associations → context graph, provenance, **Part 48 end-to-end**, GitHub 429/401/403/503 and malformed response spoken, calendar unavailable, duplicate mail/event, no credentials in output, structural write checks, adapter verified writes | 34 passed | PASS |
| `test_hub_api_composition.py`: `/integrations` endpoints (auth, host, no secrets, enable/disable/sync/connect/disconnect/permissions), registry health mapping, production composition | 8 passed | PASS |
| `scripts/e2e_launcher_check.py --privacy private` with the hub (real process) | API up, `/integrations` shows all five integrations truthfully ("not set up"), microphone closed, graceful exit 0 | PASS |
| PostgreSQL-only variants | 601 skipped | NOT RUN |

**Part 48 (synthetic end to end) as executed by the test:** synthetic email → Gmail sync creates the email, event and deadline items (bus events fire) → Calendar sync → "What's important tomorrow?" says the project review is in an email with no matching calendar event and offers → "Add it." → confirmation naming the event → "yes" → created, read back → "Done. I added 'JARVIS project review' tomorrow at 11 AM to your calendar and confirmed it's there." → "What emails do I have about the project?" → Gmail search result → "What changed in my JARVIS GitHub repository?" → actual (mock) GitHub commits/issues → "Where did you get that?" → a real source; zero LLM calls. Failures covered: OAuth expiry, calendar unavailable, GitHub rate limit / invalid token / permission denied / 5xx / malformed response / network failure, duplicate event and email, document parser failure, messaging unavailable, malicious email/commit/message.

## Performance (measured, `scripts/benchmark_hub.py`; fakes, so **no network time**; SQLite in memory)

| Operation | Result |
|---|---|
| Gmail sync, 50 emails (150 items) cold | 589 ms |
| Gmail incremental, nothing new | 35 ms |
| Calendar sync, 100 events cold / unchanged | 65 ms / 55 ms |
| GitHub sync cold / unchanged (ETag) | 28 ms / 34 ms |
| Documents: index 300 files (7 bounded passes) / unchanged scan / 20 changed | 1.3 s total / 183 ms / 240 ms |
| Store insert per new item / duplicate detection per item | 1.4 ms / 1.4 ms |
| Context update including hub data | ~125 ms |
| `search_email` tool / `search_all` (stored) / a spoken GitHub question | 74 ms / 31 ms / 4.5 ms |
| CPU during the benchmark (busy loop of syncs) | ~54-79 % of one core (this is a stress run, not idle) |
| Memory | 74 → 86 MB in the benchmark process |

Idle background cost was not separately re-measured for Phase 18; with no integration enabled the sync loop wakes once a minute and finds nothing due (the Phase 16/17 idle figure, 0.43 % CPU / ~485 MB, was for the whole runtime). Real API latency and PostgreSQL insert latency were **not measured**.

## Limitations

1. **No real account was contacted** — Google, GitHub and Telegram behavior is proven against mocks of their documented APIs, not against the services.
2. **PostgreSQL** unavailable: `0007_hub` verified on SQLite only.
3. **Push is not implemented** (Gmail Pub/Sub, Calendar channels, GitHub webhooks): they need a public HTTPS endpoint. Incremental cursor polling plus bus events is used instead. Document watching polls (5 min) instead of using file-system events.
4. The hub router is **deterministic patterns**: phrasing outside them falls to the older LLM/tools path. The LLM brain is not handed hub tools (by design the agent cannot call an API), so unusual wording may not reach them.
5. Spoken **update/delete calendar events** and **"index this attachment"** are not hub tools: update/delete stay with the Phase 12 tools (not gated by the hub's `UPDATE_EVENT`/`DELETE_EVENT`), attachment download exists as an adapter method only.
6. Google token files are encrypted only when (re)saved after this change; `.env` values and the Google OAuth client JSON are plaintext.
7. Gmail: inbox only; ≤50 messages per sync run (continued by page cursor); English rule-based extraction.
8. GitHub: repositories the token sees; ≤8 tracked repositories per sync; no branch/CI/code-search speech.
9. The dashboard was tested through its API and by asserting the page's content, **not in a browser**; tray menu clicks and real notifications were not exercised.
10. Cross-source merging still lives in the context engine (not the store): the same interview in email and calendar are two `hub_items`.

## Manual requirements

PostgreSQL + `alembic -c database/alembic.ini upgrade head`; Google Cloud OAuth client and consent for Gmail and Calendar; a GitHub fine-grained token (`python scripts/github_cli.py token`) and `JARVIS_GITHUB_ENABLED=true`; a Telegram bot for messaging; `JARVIS_DOCUMENT_DIRS` for the watcher; opt-in permissions you want (create events, attachments, indexing).

## Next phase

Phase 19 — Advanced Voice & Natural Conversation — can start: Phase 18 leaves the voice path untouched and gives it what it needs (a truthful status of every source, uniform tool results, deterministic answers that keep working without the LLM, verified actions, provenance for "where did you get that?"). Before or alongside it, close the real-world gaps above: run everything once against real accounts and PostgreSQL, then decide whether Phase 19's natural-language layer should replace the pattern router with LLM-selected hub tools (the tool router and permission gate are already the right boundary for that).
