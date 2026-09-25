# Autonomous agent architecture (Phase 21)

JARVIS can take a multi-step goal ("Open GitHub, find my Virtual Campus repository, open the README and summarize the setup requirements"), plan it, carry it out step by step behind the existing permission and confirmation layers, observe and verify after every step, recover or replan when something goes wrong, and stop safely. It is **not** an unrestricted computer controller: the only surfaces are the Phase 20 browser tools, the Phase 18 GitHub integration, and deterministic text analysis.

```
voice / text
  -> ConversationEngine -> IntelligenceRouter
       -> AutonomyRouter        answers to a waiting task | "stop / pause / resume / what are you doing?" | a new multi-step goal
       -> HubRouter -> BrowserRouter                       (Phase 18 / 20, single commands, unchanged)
  AutonomyManager (one task at a time; history; limits; confirmation hand-off; shutdown)
    Planner   goal -> clauses -> steps + subgoals + expected states + checks; validate(); risk; preview
    TaskRunner (background thread)
       loop:  observe -> [skip if already satisfied] -> resolve typed references -> risk gate (WAITING_FOR_PERMISSION)
              -> ToolRouter.call -> observe -> Verifier -> store outputs -> next
              on failure: done anyway? -> ask the user -> needs the user (sign-in/CAPTCHA) -> safe retry -> replan/fallback -> stop honestly
    ToolRouter  the only door:  api tools (Integration Hub)  |  local analysis  |  BrowserTools.call  (schemas, categories, PermissionManager, confirmations)
    Observer / Verifier  cached browser state (+ media state when needed); explicit checks against the tool result AND the observed state
```

Everything here is deterministic. There is no model call in planning, action selection, observation or verification (measured: zero `llm_calls`), so tasks work offline, cost no latency for "thinking", and cannot be steered by web text. A model-proposed plan can be plugged in (`Planner.from_proposal`) and goes through exactly the same validation.

## Modules (`autonomy/`)

| File | Role |
|---|---|
| `models.py` | `Task`, `Step`, `SubGoal`, `Check`, `Ref`, `Observation`, `Verdict`, `Risk`, `TaskStatus` (plain data) |
| `planner.py` | grammar, compilation, `validate()`, `expand_fallback()`, `from_proposal()`, task preview |
| `toolrouter.py` | registry of tools by family, schema validation, code-computed risk/permission, typed reference resolution, API and local tools, final report wording |
| `analysis.py` | README summaries (commands never repeated), technology detection, page-section check, official-result ranking |
| `observe.py` | `Observer` (state + before/after diff) and `Verifier` (explicit checks) |
| `runner.py` | the controlled loop, confirmation gate, recovery, replanning, loop detection, limits |
| `manager.py` | `AutonomyManager` (task lifecycle, clarification, controls, history) and `AutonomyRouter` (conversation front door) |
| `build.py` | production wiring from settings |

New elsewhere: `read_repository_readme` (GitHub client/adapter/hub tool, read-only, bounded, sanitized, injection flag); `ConversationEngine.cancel_task()/task_active()`; voice "Stop"/"Cancel" cancel a running task; `/tasks*` API; dashboard panel; tray items; 13 `AUTONOMY_*` settings.

## Task model

`TaskStatus`: `PLANNING`, `WAITING_FOR_PERMISSION`, `RUNNING`, `WAITING_FOR_USER`, `VERIFYING`, `PAUSED` (added: pause/resume needed a state), `COMPLETED`, `FAILED`, `CANCELLED`, `BLOCKED`. `BLOCKED` = it needs something only the user can give (a sign-in, a CAPTCHA, a confirmation that never came).

A `Task` has: id, goal, status, steps, current step, sub-goals, start/update/finish times, `risk_level`, a blackboard (typed results of earlier steps; **never shown or stored**), an action history (≤60 entries, in memory), the pending question/choices, the result or failure, counters (tool calls, retries, replans, consecutive failures) and a preview for risky tasks.

A `Step` has: id, description, tool, arguments (literals or typed `Ref`s), expected state, verification checks, retry policy (`safe` = may be repeated), `satisfied_when` (skip if already true), `fallback` rule name, and, computed by code, `permission` and `risk`.

`Risk`: `READ_ONLY` < `LOW_RISK` < `EXTERNAL_EFFECT` (downloads, submits, posts) < `SENSITIVE` (uploads, purchases, account/security changes, granting access) < `DESTRUCTIVE` (delete/remove). The **task's risk is the maximum over its steps**, whatever their order.

## Planning

The goal is cleaned (wake word removed), refused if it asks for a shell, passwords/cookies/keys, CAPTCHA/MFA bypass, disabling security, deleting files, or the system prompt, split into clauses ("then", "and" before a verb, commas), and each clause is matched by a grammar: open a site, find a repository (or "latest"), open the repository, read/summarize a README (setup or overview), technologies, YouTube search/play/volume, web search + open the official result, portfolio-section check, find-and-download, upload, submit. Clauses compile into steps with **context awareness**: a site already open, YouTube results already showing, a repository found earlier are skipped, not redone. Missing information becomes a question ("What's the address of your portfolio website?"), never a guess. Single browser commands ("Open GitHub", "Pause") are *not* autonomous goals: they stay with the Phase 20 router.

Tool preference: the GitHub API before a browser (repository lookup, README), deterministic analysis for reading, the browser only for what needs it. Without the integration the plan uses the browser and says so ("GitHub isn't connected, so I searched GitHub publicly… I can't tell whether it's yours").

`validate()` rejects: unknown or forbidden tools, arguments failing the tool's schema (extra keys included), references to results no earlier step produces or that are not on the allow-list of typed keys, unavailable capabilities (browser off, integration not connected), duplicate ids, plans over `AUTONOMY_MAX_STEPS`. Only tool name, arguments and description are taken from a proposal; risk, permission, confirmation and status are always computed here.

## Observation and verification

Before each step the runner observes (URL, host:port, title, tab count/active tab, browser state; media state for video steps; sign-in/CAPTCHA/dialog flags folded in from tool results); after it, it observes again and records the **difference** ("url; title; tab_count") in the history. No page or screen is captured for this; the browser engine already keeps the state.

Verification is explicit per step: `outcome_verified` (the tool confirmed), `url_host`/`url_is`/`url_repo`/`url_matches_board`/`title_contains`/`page_changed`/`data_key`/`results_exist`/`playing`/`volume`/`browser_open`. State checks use the **observed** state, not the tool's word: for video, the page is re-read (bounded by `AUTONOMY_OBSERVATION_TIMEOUT_SECONDS`) until it settles. This caught a real bug: YouTube's ad was "playing" per the tool while still buffering.

Completion is judged against the goal: a plan ends with a `compose_report` step that builds the answer from what the task actually learned; a task with no answer is `FAILED`, not "done". Opening a repository is not completing "summarize its README".

## Recovery and replanning

| Situation | Behavior |
|---|---|
| tool reports failure but the expected state already holds | step marked done "from the page state after an error" (nothing repeated) |
| several equally good matches / result choices | `WAITING_FOR_USER`: "I found 3 matches: 1, …. Which one do you mean?"; the reply ("the satellite report", "the second one", "the official one", a number) resumes **this** task |
| sign-in or CAPTCHA | `BLOCKED`: "the page needs a human check, which only you can complete; I didn't try to get past it" |
| transient failure, step marked safe (loading, reading, finding) | bounded retry |
| unsafe step (click, type, upload, submit, download) | never retried blindly; state checked first, then honest failure |
| GitHub API failure / not found | fallback: search GitHub in the browser, find the link, open it, derive `owner/repo` from the URL |
| README API failure | fallback: read the repository page |
| "Element not found" | fallback: look again (`find_element`) and click the best current match |
| browser crash | the engine recovers (Phase 20); a safe step continues, an unsafe step reports "The browser crashed and I restarted it. Please ask again." (found by test: the download is not repeated) |
| same action + same page state repeatedly | loop stop: "I couldn't complete that because the page isn't responding as expected." |

## Permissions and confirmation

Every step passes: `ToolRouter` (registry, schema) → risk computed from the tool, the arguments **and the plan's wording** (a download whose target is only known at run time is still priced) → for browser tools `BrowserTools.call` (five categories, the PermissionManager, its own confirmation gate). At `EXTERNAL_EFFECT` and above the task pauses (`WAITING_FOR_PERMISSION`), the request is registered with the shared Phase 17 `ConfirmationEngine` (single use, strict yes/no, bound to the exact step; the Phase 19 low-confidence voice guard therefore applies), and the step runs only after the user's own yes. Risk is re-evaluated with the **real** values after references resolve: a page can only raise the risk (e.g. a control named "Buy now"), never lower it. "No" cancels the task ("you declined that step"); an unclear answer never confirms; no answer in `AUTONOMY_CONFIRMATION_TIMEOUT_SECONDS` → `BLOCKED`.

## Limits

Duration, steps, tool calls, per-step retries (and task-wide 2×), replans, consecutive failures, loop threshold, browser tabs (`BROWSER_MAX_TABS`). Reaching one ends the task safely with the reason.

## Voice, dashboard, tray

Voice starts tasks ("Hey JARVIS, find my repository and summarize the README"), answers clarifications, and cancels: a spoken **Stop/Cancel** cancels the running task first ("Okay, I stopped the task."). A quick task answers inline (up to `AUTONOMY_INLINE_WAIT_SECONDS`); otherwise JARVIS says "Got it… I'm working on it", gives at most two short progress updates ("I found your repository.", "I've read the README.") and delivers the result as a `high` announcement. Risky tasks are announced with a preview ("I can do this: 1… Step 3 requires your confirmation."). Dashboard: *Autonomous task* (goal, status, progress `n / m`, current action, verification, risk, question, result/failure, step list, Pause/Resume/Stop, recent history). Tray: current task label, Pause/Resume/Stop task. API: `GET /tasks`, `POST /tasks/pause|resume|cancel`.

## Shutdown and persistence

Shutdown cancels the active task (`cancel_reason = "JARVIS was shutting down"`), asks the browser to stop, marks it and records it. Nothing resumes on the next start. Persisted: `.jarvis/autonomy_history.json`: goal (redacted), status, time, duration, steps, risk, a ≤200-character outcome. Never persisted: the blackboard, page text, README text, credentials, the action history.

## Phase 22: the Personal Operator on top of this layer

Phase 22 adds `workflows/` (see `PERSONAL_OPERATOR_ARCHITECTURE.md`, `WORKFLOW_ENGINE.md`). It reuses this layer rather than replacing it:

- **Browser and repository actions** in a workflow go through the same `ToolRouter` (schemas, code-computed risk, `BrowserTools` → `PermissionManager`); the operator only adds a second registry of *personal-data* tools (Gmail, Calendar, Tasks, Reminders, GitHub, Documents, Memory, Notifications) in front of the same Integration Hub gate.
- **Risk** uses the same `Risk` enum and `needs_confirmation` rule; the confirmation hand-off uses the same shared `ConfirmationEngine` (one open question per session).
- The **planner discipline is the same** (deterministic grammar, validation, model proposals only through the same checks) with two additions that matter for personal data: typed `From` references restricted to declared dependencies, and a fact-status gate (only VERIFIED / HIGH_CONFIDENCE facts may drive a write).
- The router order is now: operator → autonomy → hub → browser. Ordinary single requests match none of the operator's templates and fall through unchanged. "Stop"/"Cancel" cancels a running workflow and/or a running task.
- The two coexist: an autonomous browser task and a workflow can run at the same time; the browser lock serializes their browser use.
