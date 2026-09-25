# Sync engine

`integrations/hub/sync.py`. One `SyncEngine` for every integration; `SyncRunner` is the background thread (`jarvis-sync`, a launcher lifecycle component).

## Per-integration state (registry, `.jarvis/integrations.json`)

`cursor` (opaque, adapter-specific) · `last_sync_at` · `last_error_kind`/`last_error` · `failures` · `retry_at` · `enabled` · `granted` permissions. Stored items live in the `hub_items` table.

## What one sync does

1. **Skip** (and say why) if: the integration is switched off, not connected, waiting for you to reconnect (`AUTH_ERROR`, `CONFIGURATION_ERROR`, `PERMISSION_ERROR`), still backing off (`retry_at`), or not yet due (`sync_interval_seconds`). A forced sync (dashboard "Sync now", "Sync Gmail now") ignores due/backoff but not off/not-connected.
2. Take a per-integration lock (no overlapping syncs), mark SYNCING, publish `INTEGRATION_SYNC_STARTED`.
3. `adapter.sync(cursor, limit)` → items changed since the cursor.
4. **Upsert** each item by `(source, kind, source_id)`: `created` / `updated` (content hash changed) / `unchanged`. Items reported gone are marked deleted (hidden, revived if they return).
5. Record success (new cursor, time; failures reset) and publish `INTEGRATION_SYNC_COMPLETED`; hand the *changed* items to the hub feed (events, notifications). A failing consumer is logged and never undoes the sync.
6. On failure: classify (`AUTH_ERROR`, `PERMISSION_ERROR`, `RATE_LIMIT`, `NETWORK_ERROR`, `SERVER_ERROR`, `INVALID_REQUEST`, `NOT_FOUND`, `CONFIGURATION_ERROR`, `UNKNOWN_ERROR`), record, publish `INTEGRATION_SYNC_FAILED`.

## Backoff and rate limits

| Error | Next attempt |
|---|---|
| `NETWORK_ERROR`, `SERVER_ERROR` | 60 s × 2^(failures−1), capped at 1 h |
| `RATE_LIMIT` | not before `Retry-After`/reset (capped at 1 h), otherwise exponential |
| `AUTH_ERROR`, `CONFIGURATION_ERROR`, `PERMISSION_ERROR` | **no automatic retry** until you reconnect (status DISCONNECTED with the message) |
| `NOT_FOUND`, `INVALID_REQUEST`, `UNKNOWN_ERROR` | recorded, ERROR status; retried at the normal interval |

A successful sync clears the failure count. Tested: exponential doubling, honoring a 30-minute Retry-After, no calls while backing off, no calls after an auth failure even hours later, recovery.

## Incremental strategies

| Source | Cursor / method |
|---|---|
| Gmail | `{"after": <epoch of newest message>}` → query `in:inbox after:<epoch−60>`; if cut off by the page bound, `{"query", "page"}` continues the same search |
| Calendar | window 7 days back / 60 ahead; cursor = ids + start times last seen → removed events detected; unchanged events skipped by content hash |
| GitHub | `{"since": <time>}` for commits; ETag conditional requests (304 = free) for everything |
| Documents | `(mtime_ns, size)` per path in `document_watch.json`; only changed files are indexed; bounded per sync |
| Telegram | `{"since": <epoch>}` |

Idempotence is a property of the store (`upsert`), not of luck: re-running a sync, or overlapping cursors, changes nothing (tested for Gmail, Calendar, GitHub).

## Scheduling and resources

`SyncRunner` wakes every `JARVIS_SYNC_LOOP_SECONDS` (60) and syncs only integrations that are **due**; it also wakes early on `INTEGRATION_CONNECTED`, `SYSTEM_RESUME`, `INTEGRATION_RECOVERED`. It does nothing in PRIVATE mode. Default intervals: Gmail 10 min, Calendar 15 min, GitHub 15 min, Telegram 10 min, Documents 5 min. Retention: items older than `JARVIS_HUB_RETENTION_DAYS` (90) are pruned daily. **Push/webhooks are not used** (they need a public endpoint); the design keeps an "event-driven" path (bus events) for everything downstream.

## Measured cost (synthetic sources, JARVIS-side work only; SQLite in memory)

Gmail cold sync of 50 emails (150 items) ≈0.6 s, incremental with no new mail ≈35 ms; Calendar 100 events ≈65 ms cold / ≈55 ms unchanged; GitHub ≈28 ms / ≈34 ms; store upsert ≈1.4 ms per item, same ≈1.4 ms for a duplicate (detected, not stored); context update with hub data ≈120-130 ms. See `PHASE_18_IMPLEMENTATION.md` §Performance.
