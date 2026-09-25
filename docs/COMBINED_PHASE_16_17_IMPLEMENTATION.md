# Combined Phase 16 + 17: Implementation report

Everything below was actually run on this Windows 11 machine unless a limitation says otherwise. Nothing was committed or pushed.

## 1. Executive summary

JARVIS now runs as a supervised, Windows-native background assistant and can reason across the user's authorized sources.

**Implemented and verified**
* Health monitor, truthful tray status/voice indicator, privacy modes (persisted), automatic restart of a crashed voice runtime with backoff, graceful stop from outside (`--stop`), power/lock state, structured redacted logging, event bus, durable notification history with de-duplication/quiet hours/acknowledgement, durable action audit, database retry/schema validation/pool options, local dashboard + API (token + loopback only), DOCX support, secret scanner, Windows install/start/stop/update/uninstall scripts.
* Personal Context Engine (entities, relationships with evidence, entity resolution, conflicts), deadline kinds, task dependencies, findings (cross-source rules), daily focus/planning, confirmed calendar execution with read-back verification, explanations with sources, contextual references, memory relevance ranking, activity timeline, preferences, morning briefing 2.0 and evening review, "prepare me for..." workflow, event-driven background runner. **No language-model call is made by any of it.**

**Not done / partial:** see section 10.

## 2. Architecture changes

New modules only; existing systems were extended, not rewritten.

```
backend/core/   redaction, logging (extended), events (bus), health, recovery (backoff + Supervisor), privacy, notifications,
                preferences, state_store, action_audit, metrics, sysmetrics, context, database (extended), security/{trust,approval}, llm/metered
backend/api/    routes/system.py (dashboard API), server.py (in-process uvicorn), dashboard.html
desktop/        runtime/{composition (composition root), health_checks, periodic, power (extended), manager (start_paused, activation)}
                tray/{status, tray (new menu)}, launcher/{app (lifecycle), cli (--stop), single_instance (ExitSignal), __main__ (config errors)}
agent/intelligence/  models, textnorm, extraction, deadlines, snapshot, context_engine, findings, dependencies, planner, plan_executor,
                confirmation, explain, refs, relevance, timeline, briefing, workflows, prefs_intents, service, router, proactive, runner
scripts/        secret_scan.py, benchmark.py, e2e_launcher_check.py, windows/*.ps1
```
Hook points into existing code: `ConversationEngine` (optional `intelligence`, `bus`; routing after the task executor's confirmation, before the
brain), `VoiceEngine` (`request_activation`, timers), `RuntimeManager` (`start_paused`, `request_activation`), `voice/bootstrap.py` (metered LLM).
The existing custom AgentBrain was kept (the repository does not use LangGraph; nothing was replaced).

## 3. Production hardening

| Requirement | Status |
|---|---|
| Windows startup without VS Code | Startup shortcut (existing) or Scheduled Task with restart-on-failure (`install_jarvis.ps1 -UseScheduledTask`). Scripts syntax-checked; **not executed end-to-end** (see limitations). |
| Tray | New menu: status, microphone state, Talk, Briefing, Tasks, Reminders, Memory, Integrations, Settings, Pause/Resume, Private mode, Restart, Exit. Items whose service is off are greyed out. |
| Health monitor | 12 services with states healthy/starting/degraded/disconnected/failed/disabled; overall online/starting/degraded/offline. Verified against the real process. |
| Automatic recovery | Supervisor restarts an `ERROR` voice runtime with exponential backoff, gives up after N attempts, retries after a cooldown. Integrations recover on the next successful call; their health re-check backs off 15 s -> 5 min. |
| Offline/degraded | `JARVIS_OFFLINE_MODE` makes Gmail/Calendar `unavailable` without any network call; answers say "I couldn't check your calendar". |
| Config | New `Settings` fields, `.env.example` complete (a test enforces that every `JARVIS_*`/`DB_*` field is documented). |
| Secrets | `scripts/secret_scan.py` (clean); redaction of credentials in logs/audit. |
| Logging | Text or JSON lines (`JARVIS_LOG_JSON`): timestamp, severity, component, event, correlation id; redacting filter. |
| Event bus | 19 event types; failing subscribers isolated. |
| Power | ACTIVE / BACKGROUND (locked) / SUSPENDED / RESUME / OFF; lock probe verified to run on this machine. Lock -> privacy BACKGROUND. |
| Privacy | ACTIVE / BACKGROUND / PAUSED / PRIVATE, persisted; PRIVATE starts the runtime paused so the microphone is never opened (verified with the real process). |
| Voice state accuracy | "Listening" only when the microphone stream is open (verified: active run reports `listening` with the stream open; private run reports `microphone_disabled`). |
| Notifications | CRITICAL/IMPORTANT/NORMAL/LOW routing, de-duplication, cooldown, history, acknowledgement, quiet hours, voice cutoff, retry of failed delivery; persisted across restarts. |
| Task/reminder persistence | Verified across a database close/reopen, including a recurring reminder and one that became due while "off". |
| Memory reliability | Persistence, duplicate control, supersede with history, secrets refused; **one real bug fixed** (slot normalization). |
| Tool security | Approval classes and tool categories (`backend/core/security/approval.py`), `Tool` defaults remain the strictest (HIGH risk, permission required). |
| Confirmation engine | Digest-bound, single-use, expiring, USER-only, explicit words for destructive actions. |
| Audit | Durable JSONL, redacted, rotated. |
| Database | `with_retry` (transient errors only), `schema_status` (compares Alembic head), pool options, `dispose_engine` on shutdown/resume, indexes and FK cascade verified on SQLite. **PostgreSQL itself was not available**; see limitations. |

## 4. Intelligence system

Pipeline: `SnapshotCollector` (read-only, per-source isolation) -> `PersonalContextEngine` (entities, resolution, relationships, deadlines,
conflicts, task proposals) -> `FindingsEngine` -> answers. See `PERSONAL_CONTEXT_ENGINE.md`, `INTELLIGENCE_ENGINE.md`, `PLANNING_ENGINE.md`.

## 5. Proactive intelligence

`IntelligenceRunner` (event-driven wake + interval, skips unchanged, re-evaluates time rules every 10 min) -> `IntelligenceNotifier` ->
`NotificationCenter`. Off by default (`JARVIS_INTELLIGENCE_PROACTIVE`). See `PROACTIVE_INTELLIGENCE.md`.

## 6. Security

Trust levels and prompt-injection scan (`trust.py`), confirmation engine, approval classes, action audit, loopback+token API. See
`PROMPT_INJECTION_SECURITY.md` and `DATA_PROVENANCE.md`.

## 7. Testing (only tests actually executed)

Full suite: **2110 passed, 601 skipped** (baseline 1892 passed, 601 skipped: 218 net new tests; two existing tray tests and one RAG test were updated for the new menu and DOCX support).

| TEST | RESULT | STATUS |
|---|---|---|
| tests/unit (redaction, JSON logs, event bus, backoff, supervisor, health, state files, preferences, privacy, indicator, notifications, metrics, database reliability) | 45 passed | PASS |
| tests/intelligence (scenario via router, extraction, resolution, graph, deadlines, planner, proactive runner, duplicates) | 73 passed | PASS |
| tests/security (injection, trust, approval, confirmation engine, audit, API auth/host, secret scan) | 38 passed | PASS |
| tests/desktop (tray status, power, lifecycle, privacy wiring, recovery, health checks) | 28 passed | PASS |
| tests/agent (conversation engine + intelligence) | 8 passed | PASS |
| tests/memory | 4 passed | PASS |
| tests/voice (STT/TTS/LLM failure, manual activation) | 4 passed | PASS |
| tests/integrations (DOCX incl. XML bomb, document intelligence, corrupted config) | 7 passed | PASS |
| tests/e2e (synthetic day, DB failure, OAuth expiry, calendar failure, malicious document, fuzzed input, restart persistence, sleep/resume) | 9 passed | PASS |
| PostgreSQL-only variants of existing tests | 601 skipped | NOT RUN (no PostgreSQL here) |
| `scripts/e2e_launcher_check.py --privacy private` (real process) | api up 2-3 s, runtime paused, mic closed, graceful exit 0, port released | PASS |
| `scripts/e2e_launcher_check.py --privacy active` (real process, real wake-word listener, microphone opened) | runtime running, `listening`, health truthful (LLM disconnected, DB "no migrations"), graceful exit 0 | PASS |

**Synthetic end-to-end (Part 67), what was and was not covered.** Covered by automated tests: synthetic email arrival -> extraction ->
project/event/deadline/task detection -> calendar cross-check -> notification evaluation -> "what's important tomorrow" -> "what should I
work on today" -> "create a plan" -> "add it to my calendar" (confirmation, creation, read-back) -> "why did you schedule that" ->
database failure, expired OAuth, calendar failure, offline mode, sleep/resume, restart persistence, shutdown. Covered with the real process:
launch, API/health, listener activation, graceful stop. **Not covered:** speaking the sentences to the real microphone (needs a person and
Ollama running), a real Windows restart, real sleep/resume, real Gmail/Calendar.

## 8. Performance (measured)

Real process (`e2e_launcher_check.py`, 20 s idle window, this machine, models cached, Ollama not running):

| Metric | Result |
|---|---|
| Process start -> API answering | 2-3 s |
| Process start -> runtime ready (models loaded) | 6-7 s |
| `startup_ms` reported by JARVIS (launcher start to "running") | ~0.8-1.7 s (the voice models load in the background afterwards) |
| Idle CPU, listening for the wake word | 0.43 % of the machine |
| Idle CPU, PRIVATE (paused) | 0.03 % |
| Idle RAM | ~485 MB (mostly Whisper/ONNX models) |
| LLM calls made by the intelligence layer | 0 |

Intelligence layer (`scripts/benchmark.py --runs 200`, synthetic in-memory sources, so **no network time**):

| Operation | mean | p95 |
|---|---|---|
| read sources + build graph + findings (cold) | 2.7 ms | 3.6 ms |
| focus answer (cached) / (cold) | 0.06 ms / 3.1 ms | 0.07 / 4.9 ms |
| plan my day (cold) | 3.1 ms | 3.9 ms |
| prepare-for-event (cached) | 0.07 ms | 0.12 ms |
| conflict check / why explanation | 0.01 ms / 0.1 ms | 0.01 / 0.15 ms |

Real Gmail/Calendar latency is dominated by the network and was not measured. Wake-word latency, STT, TTS and LLM timings are now recorded
by `metrics` (`wake_to_prompt_ms`, `stt_ms`, `tts_synthesis_ms`, `conversation_ms`, `llm_ms`) and shown on the dashboard, but **no spoken
conversation was run**, so those values were not measured here.

## 9. Deployment: run JARVIS without VS Code

```powershell
cd <JARVIS folder>
.\scripts\windows\install_jarvis.ps1            # venv, dependencies, .env, migrations, start-with-Windows
notepad .env                                     # set DATABASE_URL and the model paths (once)
.\scripts\windows\start_jarvis.ps1              # hidden window, tray icon appears
.\scripts\windows\stop_jarvis.ps1               # graceful stop
```
Details: `DEPLOYMENT.md`.

## 10. Known limitations

* PostgreSQL behavior is unverified here (601 tests skipped). Migrations were not re-run; no new tables were added in this phase.
* Gmail, Calendar and Telegram were tested through their fake clients, not against the real services.
* **GitHub context (Part 43) is not implemented** (no GitHub integration exists). Nothing claims otherwise.
* Deterministic, English-only, rule-based extraction: it will miss unusual phrasings and never guesses vague dates.
* Person/organization extraction is minimal (email sender names; an organization after "with/at" in an interview sentence).
* The cross-source graph is **computed on demand and cached in memory**; it is not persisted into the Phase 8 PostgreSQL graph. Dependencies, preferences, timeline, notifications, audit and privacy state are local JSON/JSONL files under `.jarvis/`.
* The dashboard is one static HTML page (no React build); it was tested through its API, not in a browser.
* Tray menu clicks and balloon notifications were not exercised interactively (the tray starts and the runtime exits cleanly with it enabled).
* Start-with-Windows registration, the Scheduled Task, `update_jarvis.ps1`, real sleep/resume and a Windows restart were not executed (the scripts parse cleanly; the state they rely on is unit-tested).
* No executable installer was built: PowerShell scripts were judged sufficient for the current architecture.
* The Phase 14 proactive engine and the new intelligence notifier can both run; when Phase 14 is enabled the overlapping finding kinds are skipped so the user is not told twice.

## 11. Manual requirements

* Install PostgreSQL, set `DATABASE_URL`, run `alembic -c database/alembic.ini upgrade head` (the live run shows the database as degraded until you do).
* Install/start Ollama (`ollama serve`) and pull the model in `LLM_MODEL` for spoken answers to questions the intelligence layer does not handle.
* Google OAuth setup for Gmail/Calendar (`docs/gmail-intelligence.md`, `docs/google-calendar-integration.md`); set the `*_ENABLED` flags.
* Windows microphone permission for desktop apps.
* Optional: run the installer from an account that may create Scheduled Tasks if you choose `-UseScheduledTask`.

## 12. Next phase

Verify on real infrastructure and close the honest gaps: (1) run the PostgreSQL test variants and the install/update scripts on a clean machine;
(2) a real-account soak test of Gmail/Calendar with the intelligence runner enabled; (3) persist the context graph and dependencies in PostgreSQL
(new Alembic revision) and connect it to the Phase 8 graph; (4) GitHub read-only integration; (5) a packaged installer if distribution beyond
one machine is needed; (6) a browser test of the dashboard and a React front end only if the static page proves insufficient.
