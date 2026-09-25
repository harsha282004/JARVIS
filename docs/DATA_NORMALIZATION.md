# Data normalization and provenance

Every integration converts what it reads into `NormalizedItem` (`integrations/hub/models.py`). Nothing above the adapters sees a Google, GitHub or Telegram model.

```
NormalizedItem
  kind          email | message | event | task | deadline | document | project | repository | commit | issue | pull_request | meeting
  source        gmail | calendar | github | telegram | documents
  source_id     id in that system  (message id · calendar_id/event_id · owner/repo@sha · owner/repo#7 · owner/repo!8 · file path)
  external_id   id needed for a safe update/delete (calendar event id)
  timestamp     when the source item happened / was written
  retrieved_at  when JARVIS read it
  title         short, sanitized
  summary       ≤ 240 characters (never a whole email or document)
  metadata      small, kind-specific (topic, importance, location, end time, labels, page, …) — no addresses, no bodies
  confidence    low | medium | high — certainty of an *extraction*; retrieved facts are high
  item_id       sha1(source|kind|source_id)          content_hash  changes only when the content changes
```

## Derived items

An email produces one `email` item plus, when its text states them, `event` and `deadline` items with `source_id = "<message id>#event0"` / `#deadline1` / `#registration`, `metadata.message_id`, the **evidence sentence**, and the extraction confidence. Telegram messages do the same. These derived items are what other engines consume without re-reading the source.

## Provenance chain

`source_type` + `source_id` + `source_timestamp` + `retrieved_at` + `confidence` travel with the item (`NormalizedItem.provenance()`), are stored per row, and are converted into the intelligence layer's `Provenance` when items become graph entities. "Where did you get that?" answers from them: *"That came from an email (email 'JARVIS project review', dated Sep 24)."* / *"…GitHub…"* / *"…syllabus.txt, page 1."*

## Storage (`hub_items`, Alembic `0007_hub`)

Unique `(source, kind, source_id)`; indexes on `(source, kind, source_timestamp)` and `retrieved_at`; `deleted_at` hides an item that vanished at the source; all times UTC (converted before storing — SQLite would otherwise keep the wall clock and drop the offset; this was found and fixed by a test). Search is parameterized (text is never interpolated into SQL). The migration was applied and reverted on a scratch SQLite database in the tests; **PostgreSQL was not available to run it there**.

## Duplicate prevention

* Same source item retrieved again → same row (`unchanged`); changed → `updated`; the same mailbox message listed twice → one row (tested); the same calendar event synced twice → one row; a moved event → an update.
* Cross-source duplicates (the same interview in an email and on the calendar) are **not** merged in the store — they are different sources. The Personal Context Engine resolves them (name similarity + same day; same name but different days is reported as a conflict, not merged; two events of one source are never merged; a single shared generic word never matches).

## Retention and removal

Items older than `JARVIS_HUB_RETENTION_DAYS` are pruned; `disconnect(purge=True)` deletes everything a source stored; a switched-off source is not read even from the local store.
