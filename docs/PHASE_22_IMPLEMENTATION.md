# Phase 22 — Personal Operator: implementation report

## Architecture changes
New package `workflows/` (Personal Operator). Chain: Operator → workflow runner (dependency-aware task engine) → operator/browser tool router → Integration Hub gate / PermissionManager → tool → read-back verification. Reuses Phase 17 ConfirmationEngine, Phase 18 hub, Phase 20 browser, Phase 21 ToolRouter/Risk. Router order: operator → autonomy → hub → browser; cancel words intercepted before the confirmation engine. Details: `PERSONAL_OPERATOR_ARCHITECTURE.md`, `WORKFLOW_ENGINE.md`.

## Files
Created: `workflows/{__init__,models,facts,priority,store,tools,templates,planner,runner,operator,build}.py`, `scripts/workflow_real_check.py`, `tests/workflow_helpers.py`, `tests/workflows/test_{units_planner_facts,cross_integration,scenarios_failures,security_limits,wiring_api_config,proactive_memory_recovery,real_browser}.py`, docs `PERSONAL_OPERATOR_ARCHITECTURE`, `WORKFLOW_ENGINE`, `WORKFLOW_SECURITY`, `WORKFLOW_TEMPLATES`, `PHASE_22_IMPLEMENTATION`.
Modified: `integrations/github/adapter.py`, `agent/intelligence/router.py`, `desktop/runtime/composition.py`, `desktop/launcher/cli.py`, `backend/core/{config,context}.py`, `backend/api/routes/system.py`, `backend/api/dashboard.html`, `browser/engine.py`, `scripts/e2e_launcher_check.py`, `.env.example`, `tests/autonomy/test_wiring_api_tray_config.py`, `tests/browser/test_voice_scenarios_api_tray.py`, docs (AUTONOMOUS_AGENT_ARCHITECTURE, AUTONOMOUS_TASKS, BROWSER_ARCHITECTURE, VOICE_ARCHITECTURE, CONFIGURATION, IMPLEMENTATION_LOG, architecture, README).

## Capabilities, integrations, templates
13 templates (`WORKFLOW_TEMPLATES.md`) over 23 operator tools (20 read/analysis, 3 guarded writes) + 6 browser tools. Integrations: Gmail, Calendar, Tasks, Reminders, GitHub (identity-verified), Documents (file+page provenance), Memory (context only), Browser, Notifications, Voice. Morning briefing with priority synthesis and reasons; "Tell me more"; proactive suggestion-only workflows on `DEADLINE_DETECTED`; follow-ups ("Turn that into a reminder").

## Dependency handling, provenance
A failed dependency blocks its dependants (result lists what was blocked and why); optional sources degrade into warnings; references only to declared dependencies. Every fact carries source/id/timestamp/confidence and a status (VERIFIED, HIGH_CONFIDENCE, LOW_CONFIDENCE, AMBIGUOUS, UNVERIFIED); only the first two may drive writes. Provenance is stored on created tasks/reminders.

## Permission model, confirmation, recovery
Risk/permission/scope computed by code; nothing can send/reply/forward/delete/publish/purchase (no tool). Steps ≥ EXTERNAL_EFFECT are grouped into one confirmation via the shared ConfirmationEngine (timeout/decline/no channel → not done). Cancel/pause/resume; retries for reads only; write-ahead effect ledger; crash recovery loads workflows PAUSED, re-validates, adopts existing effects, never repeats one, never auto-resumes.

## Security audit
See `WORKFLOW_SECURITY.md`: 7 findings fixed (wrong date after conflict answer, confirmation race, unvalidated checkpoint/argument types, unbounded waiting workflows, cancel swallowed as "no", empty-result verification, misplanned application); residual risks listed.

## Tests executed (measured, this machine, Windows, Python 3.13)
| Suite | Result |
|---|---|
| `tests/workflows` (UNIT + INTEGRATION + failure injection + security + wiring/API + 3 REAL browser) | 170 passed, 0 skipped |
| Full suite | **2896 passed, 618 skipped, 0 failed** (baseline before Phase 22: 2710 passed / 634 skipped; +186 passed; the 16 fewer skips are Phase 20/21 real-browser tests that now run — see below) |
| `scripts/e2e_launcher_check.py --privacy active` (real process) | all 27 checks true incl. 6 new operator checks, exit 0, no ERROR in log, idle CPU 0.44 %, 490 MB |
Cross-integration (10) and end-to-end scenarios (10) are in `test_cross_integration.py` / `test_scenarios_failures.py`; failure injection (Gmail off/auth/network/permission, calendar down/off, browser unavailable, missing/hedged deadline, duplicate task, passed reminder time, LLM unavailable, confirmation timeout, no confirmation channel, crash + restart ×3, cancellation phrases, prompt injection) in the latter.

**Latent bug found by real-world testing:** `BrowserEngine._page_result` verified the final page against the host without its port, so every local-server real-browser test of Phases 20/21 was silently skipping. Fixed; all 16 now pass.

## Real-world validation
- REAL: real headless Edge against a local site driving operator browser steps (read-only workflow; submit waits for confirmation, page unchanged until "yes", then clicked; hostile page read as data, nothing clicked) — passed. Real launcher process with the real API/dashboard — passed.
- NOT VERIFIED: Gmail, Calendar, GitHub, Documents against live accounts — none are connected on this machine (integrations report "disconnected (not set up)"); verified only over deterministic fakes and the honest "not connected" behaviour in the real process. `scripts/workflow_real_check.py` runs read-only workflows against your running JARVIS and reports NOT VERIFIED for anything not connected.

## Performance (15 runs, 7-step email→task→reminder workflow over in-memory fakes)
planning 4.4 ms; per step 18.8 ms mean; total 297 ms (task_create 38 ms and reminder_create 47 ms include SQLite write + read-back; search_email 29 ms; extraction 5 ms; verification/routing <1.5 ms; the rest is per-step checkpoint file writes); CPU 0.13 s per workflow; memory +10 MB over 15 runs; LLM calls 0.

## Known limitations
English deterministic grammar (only listed workflow shapes; other compound requests get the "model unavailable" message; the proposer hook exists but no model is wired); VERIFIED = explicit, not true; live accounts unverified; recovery ledger covers the operator's own writes and confirmed browser clicks only; task titles are attacker-influenced data; checkpoint write per step costs ~100+ ms per workflow; the calendar cross-check is an implicit read-only scope in deadline workflows.

## Manual validation
1. Connect Gmail/Calendar/GitHub, run `python -m desktop.launcher`. 2. `python scripts/workflow_real_check.py`. 3. Say "Give me my morning briefing", "Check my important emails and tell me what needs attention today". 4. With a test email containing a deadline: "Find the <topic> email, create a task for it and remind me two days before" — check the task/reminder, say it again (no duplicates). 5. "Apply for the <topic> from my email" — confirm it stops and asks; say "cancel the workflow". 6. Kill JARVIS mid-workflow, restart: "what workflows are unfinished", then "resume". 7. Dashboard → Personal Operator panel.
