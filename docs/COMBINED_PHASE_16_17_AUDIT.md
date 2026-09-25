# Combined Phase 16 + 17: Audit (before implementation)

Written from reading the repository and running it, before any change was made. Baseline: `git` clean on `main` at commit "phase 15";
Python 3.13; ~22,700 lines of application code; test suite **1892 passed, 601 skipped** (the skips are the PostgreSQL-only variants:
`JARVIS_TEST_DATABASE_URL` is not set and there is no PostgreSQL service on this machine).

## 1. Current architecture (as found)

```
voice/          wake word (openWakeWord) -> STT (faster-whisper) -> ConversationEngine -> TTS (Piper); VoiceEngine state machine
backend/core/   config (pydantic Settings), database (SQLAlchemy), llm (Ollama provider), conversation engine, security (PermissionManager)
agent/          AgentBrain (custom, LLM-classified intents; NOT LangGraph), planner (plan-only), memory, rag, knowledge_graph (Phase 8),
                tasks/reminders + scheduler, events/deadlines, proactive (Phase 14), briefing (Phase 15), tools
integrations/   gmail (read-only), calendar (read + confirmed writes), messaging (Telegram Bot API, read-only)
desktop/        launcher (CLI, single instance, Startup-folder shortcut), runtime (RuntimeManager, sleep/resume watcher), tray (pystray)
frontend/       empty (.gitkeep)        database/  Alembic 0001..0006
```

## 2. Implemented (verified by reading code and by the existing tests)

Voice pipeline and Windows tray runtime; multi-turn conversation; agent brain with validated action schemas; PermissionManager with
action-bound approvals; personal memory (rule-based, secrets refused); personal RAG (TXT/MD/PDF, local embeddings, honest "not found");
Phase 8 knowledge graph derived from memory and documents; tasks/reminders (timezone-aware, recurrence, missed-reminder policy);
events/deadlines; Gmail, Calendar and Telegram integrations with untrusted-text handling; Phase 14 proactive notifications (quiet hours,
cooldown, de-duplication); Phase 15 briefings.

## 3. Partially implemented

| Area | State found |
|---|---|
| Startup without VS Code | Startup-folder shortcut existed; nothing supervised the process or its parts |
| Tray | Runtime state only (color + Start/Pause/Restart/Exit); no health, no privacy, no briefing/tasks/reminders/memory entries |
| Power state | Sleep/resume *gap* detection only; no lock (BACKGROUND) state |
| Logging | Plain-text line format only; no component/event fields, no correlation id, no secret redaction |
| Notifications | Phase 14 policy covered its own signals; no shared history/acknowledgement/preferences for other producers |
| Audit | In-memory `AuditLog` for permission decisions only; lost on exit |
| Database | Engine with `pool_pre_ping` only; no pool sizing, no schema-version check, no retry helper |
| Config | Central `Settings` existed; no production/offline/privacy/recovery/intelligence settings |
| Documents | TXT, Markdown, PDF only (DOCX explicitly unsupported) |

## 4. Missing

Service health monitor; degraded/offline reporting; automatic recovery of a crashed voice runtime; privacy modes (PRIVATE etc.) and a
truthful microphone indicator; event bus; durable action audit; centralized approval classes and confirmation engine outside the task
executor; prompt-injection scanning; cross-source reasoning (email x calendar x tasks x memory x documents); entity resolution; deadline
kinds; task dependencies; contextual references ("when is it due?"); daily planning and plan execution with verification; conflict
detection across sources; user preference store; quiet-hours preference; morning/evening review with an ATTENTION section; activity
timeline; workflows ("prepare me for..."); dashboard; performance measurement; Windows install/update/uninstall scripts; GitHub context.

## 5. Reliability issues found

1. A crash of the voice worker set the runtime to `ERROR` and stayed there: nothing restarted it.
2. An invalid `.env` value made `python -m desktop.launcher` die with a raw traceback at import time (found by a test written for this phase); the intended "exit code 2" path was never reached.
3. `MemoryService.store()` compared a candidate's `slot` in canonical form but stored it raw, so a caller passing `"favorite language"` never superseded the older memory (found by a test written for this phase; the rule-based extractor pre-normalizes, so normal use was unaffected).
4. Runtime state was the only status shown; a running-but-broken assistant (LLM down, database not migrated) looked healthy.
5. `localhost` health probes waited twice as long as needed (IPv6 then IPv4).

## 6. Security issues found

* **No secrets are committed.** `git ls-files` contains no `.env`, tokens or credentials; the only secret-shaped strings are obvious fakes in tests. A scanner (`scripts/secret_scan.py`) now enforces this.
* `.gitignore` already covered `.env`, `.jarvis/`, tokens, logs, models; a few entries (`*.pem`, `*.key`, scratch output) were added.
* Logs could carry a credential if a library put one in an exception message; there was no redaction layer.
* External text (email, calendar, documents) was already treated as untrusted in Phases 10-13; there was no shared detector, and no rule for "cannot confirm an action".

## 7. Intelligence gaps

Each source was a silo: the email knew about "review Friday", the calendar about "Final Year Project Review", the task list about
"Prepare project review slides", and nothing connected them, noticed a missing calendar entry, or noticed that memory and calendar
disagreed. Phase 15 briefings listed items but did not reason about relationships, dependencies, planning or evidence.

## 8. Deployment gaps

No install/update/uninstall procedure, no graceful "stop" from outside the tray, no restart-on-failure option, no documentation of running
without VS Code, no dashboard.

## 9. Test coverage gaps

No tests for: health/recovery, privacy, notification reliability, cross-source reasoning, planning, confirmation binding, injection,
failure injection (database, OAuth, calendar, network, LLM), the launcher as a real process, performance.

## 10. Recommended implementation order (followed)

1. Core hardening primitives (redaction, structured logging, event bus, health, recovery, privacy, notifications, audit, preferences, database helpers).
2. Runtime wiring (tray, supervisor, power, composition root, graceful stop).
3. Intelligence layer (snapshot -> context graph -> findings -> answers/plans/explanations -> confirmed execution with verification).
4. Conversation integration, dashboard/API, DOCX.
5. Tests (unit, security, desktop, intelligence, agent, e2e, real-process launcher check), measurement, documentation.
