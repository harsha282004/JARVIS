# Knowledge graph (Phase 8) and the personal context graph (Phase 17)

Two graphs exist on purpose; neither replaces the other.

| | Phase 8 graph (`docs/knowledge-graph.md`) | Context graph (`agent/intelligence/`) |
|---|---|---|
| Built from | personal memory and indexed documents | tasks, reminders, saved events, calendar, email, memory, documents |
| Stored in | PostgreSQL (`kg_*` tables) | memory only, rebuilt on demand (cached ~20 s) |
| Purpose | relationship questions ("Which projects use Python?") | cross-source reasoning, planning, conflicts, preparation |
| Provenance | source rows | `Provenance` on every entity and relationship |

**Why not a graph database / not persisted yet.** The data volume is one person's tasks, events and a few dozen emails; rebuilding takes ~3 ms with fake sources.
Persisting it adds cache-invalidation risk (a stale edge would be a false claim) without a measured need. If a need appears, the intended step is a new
Alembic revision on the existing PostgreSQL database, not a new store. This is **not implemented**; the PostgreSQL path could not be exercised on this machine.

What *is* stored durably (local JSON under `.jarvis/`): task dependencies, preferences, the activity timeline, notification history, the audit log, privacy mode.
