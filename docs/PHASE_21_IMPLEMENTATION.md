# Phase 21 implementation report: autonomous computer agent, multi-step planning, controlled task execution

Built on the working tree that still contains the uncommitted Phase 19 and 20 changes. Nothing was committed or pushed. Architecture: `AUTONOMOUS_AGENT_ARCHITECTURE.md`; security and audit: `AUTONOMOUS_AGENT_SECURITY.md`; usage: `AUTONOMOUS_TASKS.md`; settings: `CONFIGURATION.md`; decisions and bugs found: `IMPLEMENTATION_LOG.md`.

## Architecture changes

A new package `autonomy/` sits ahead of the Hub and Browser routers in `IntelligenceRouter`. Goals become validated plans; a background `TaskRunner` executes them one verified step at a time through a single `ToolRouter` (Phase 20 browser tools via `BrowserTools.call`, read-only GitHub tools via the Integration Hub, deterministic local analysis). Task-level confirmations reuse the Phase 17 `ConfirmationEngine`. Planning, action choice, observation and verification are deterministic code (no model call: measured zero `llm_calls`); a model-proposed plan is supported through the same validator. Voice "Stop"/"Cancel" cancel a running task. No new capability was added: no shell, file, credential, cookie, script or OS tool.

## Files

**Created:** `autonomy/{__init__,models,planner,toolrouter,analysis,observe,runner,manager,build}.py`; `scripts/autonomy_real_check.py`; `tests/autonomy_helpers.py`; `tests/autonomy/{__init__,test_planner_router_analysis,test_execution_recovery_security,test_wiring_api_tray_config,test_real_world}.py`; docs `AUTONOMOUS_AGENT_ARCHITECTURE.md`, `AUTONOMOUS_AGENT_SECURITY.md`, `AUTONOMOUS_TASKS.md`, `PHASE_21_IMPLEMENTATION.md`.

**Modified:** `integrations/github/{client,adapter}.py` and `integrations/hub/tools.py` (read-only `read_repository_readme`); `agent/intelligence/router.py` (AutonomyRouter first, `cancel_task/task_active`); `backend/core/conversation/engine.py`; `voice/engine.py` (Stop/Cancel cancel a task); `browser/{tools,urlsafe,youtube}.py` (bare-name upload schema; exact same-site for IPs/ports; playback requires a running video), `backend/core/{config,context}.py`, `.env.example` (13 `AUTONOMY_*`), `backend/api/routes/system.py` (`/tasks*`), `backend/api/dashboard.html` (Autonomous task panel), `desktop/tray/tray.py`, `desktop/runtime/composition.py`, `desktop/launcher/cli.py`, `scripts/e2e_launcher_check.py`, `tests/hub_helpers.py`, `tests/browser_helpers.py`, `tests/browser/test_voice_scenarios_api_tray.py` (wiring assertion), docs `BROWSER_ARCHITECTURE.md`, `BROWSER_TOOLS.md`, `VOICE_ARCHITECTURE.md`, `CONFIGURATION.md`, `IMPLEMENTATION_LOG.md`, `architecture.md`, `README.md`.

## Autonomous task capabilities

Find a repository (by name, "latest"), open it, read its README through the API, summarize setup requirements or overview, identify technologies; YouTube search → choose the official video (asks when unclear) → play → set volume, each verified; web search → open the most relevant official result (asks when unclear); portfolio section check; find-and-download with a choice question and a confirmation; upload-and-submit with two confirmations; context reuse ("Open YouTube" … "Play the official one and set the volume to 30%"); missing information asked and the same request resumed; status ("What are you doing?"), "Why did that fail?", pause/resume, stop.

## Planner, observation, verification, replanning, recovery

* **Planner:** clause grammar → steps with expected state, checks, retry policy, `satisfied_when`, fallback, sub-goals; `validate()` (tools exist, schema, references, availability, length); code-computed risk/permission; task risk = maximum; preview for risky tasks; `from_proposal()` for externally proposed plans.
* **Observation:** browser address/host:port/title/tabs/state cached (no page or screen capture), media state when needed, before/after difference recorded per step.
* **Verification:** explicit checks per step against the tool result *and* independent observation; completion judged from what the task learned (`compose_report`).
* **Replanning / fallbacks:** GitHub API failure or not-found → browser search; README API failure → repository page; "element not found" → look again and retarget; already-satisfied steps skipped; ambiguity → question → resume.
* **Recovery:** done-anyway detection, bounded safe retries, unsafe steps never blindly repeated (including after a browser crash), sign-in/CAPTCHA → `BLOCKED`, honest failure messages ("I found Satellite report 2025, but the download failed: …"), loop detection and limits.

## Permission behavior and security controls

Risk levels `READ_ONLY … DESTRUCTIVE`; ≥ `EXTERNAL_EFFECT` pauses in `WAITING_FOR_PERMISSION` and needs the user's yes through the shared confirmation engine (a "no" cancels, an unclear answer does not confirm, no answer → `BLOCKED`); the page can only raise a step's risk. External content cannot start a task, choose a tool or an untyped argument, or be repeated as an instruction (README commands dropped and counted). Dedicated audit and findings: `AUTONOMOUS_AGENT_SECURITY.md`.

## Testing (only what was executed)

Full suite: **2710 passed, 634 skipped, 0 failed** (Phase 20 ended at 2615 / 618). The 16 new skips are the PostgreSQL variants of new database-backed tests (they cannot run here). `scripts/secret_scan.py`: clean. `tests/autonomy`: 111 tests (95 run + 16 PostgreSQL variants skipped).

| Test set | Tests | Result |
|---|---|---|
| `test_planner_router_analysis.py`: planning (single, multi-step, research, YouTube, sub-goals, context, missing info → question, refusals), risk propagation and preview, API-before-browser, proposal validation (unknown/forbidden tools, schema, references, hidden dangerous steps priced by code, malformed, too long, unavailable capabilities, cannot pre-confirm), router registry/schemas/risk, typed references, README/technology/section/official-result analysis, hostile README commands, findings from real runs | 42 | pass |
| `test_execution_recovery_security.py`: **the 10 scenarios** (simple, multi-step, research, YouTube, recovery, cancellation incl. voice Stop, sensitive upload, prompt injection, loop, browser crash), ambiguity and clarification resume, confirmations (yes/no/unclear/timeout), download verification, prompt injection from a website / README / search results / typed references, credential and shell refusals, replanning (API failure → browser; control changed; error-but-done), limits (steps, tool calls, duration, consecutive failures, time budget), pause/resume/status, history redaction, shutdown, metrics and no-LLM, real-run findings pinned | 48 | pass |
| `test_wiring_api_tray_config.py`: configuration and limits, builder, launcher wiring and router order, `/tasks` auth/fields/controls/stop, dashboard panel, tray controls, external content cannot start a task, description injection, static security scans | 17 | pass |
| `test_real_world.py` (**REAL_WORLD_TESTS**, real headless Edge, local site): portfolio multi-step check, download waits for confirmation then verified on disk, hostile page is data (no interaction at all), browser disconnect before a task recovers, unreachable page fails honestly, cancellation | 6 | pass |

### Real-world validation (executed)

`python scripts/autonomy_real_check.py` (real headless Edge, throw-away profile, live internet, no GitHub token):

| Goal | Result |
|---|---|
| "Search YouTube for Blinding Lights, play the official video and set the volume to 30%." | 5 steps completed in 17 s: "An ad is playing first; The Weeknd - Blinding Lights (Official Video)… Volume is 30 percent." (first run **failed honestly**: the independent observation found the ad still buffering; fixed) |
| "Search the web for PostgreSQL documentation and open the most relevant official result" | completed in 6.8 s, opened "PostgreSQL: Documentation" (first run asked which result: a look-alike site scored too close; fixed) |
| "Open GitHub and find my Virtual Campus repository." (no API) | completed in 8–10 s via GitHub's public search; answered "GitHub isn't connected, so I searched GitHub publicly. The top match… is andresjesse/prototipo-campus-virtual. I can't tell from a public search whether it's yours" (first run wrongly said "your repository"; fixed) |

`python scripts/e2e_launcher_check.py --privacy active`: real process, real models; new checks `/tasks` idle and configured, controls honest when idle, panel in the dashboard, plus all earlier checks (browser closed until asked, opened on request, closed on shutdown): all pass, exit code 0, 0.45 % CPU / 498 MB idle. (The log shows two "LLM request failed: Could not reach Ollama" lines: the live microphone heard real speech during the run and the conversation reached the unavailable model; unrelated to tasks.)

### Performance (measured)

Planning ≈1.4–2 ms; observation ≈14–27 ms (real browser), <1 ms (fake); verification ≈0.02 ms; per-step action time is the browser's (navigation ≈3.5 s, actions ≈2.5–3 s on live sites; browser start ≈0.7 s). Total task time on live sites: 7–17 s. Deterministic fake-web tasks: milliseconds. No model calls anywhere in planning or execution.

## Known limitations

1. **Not exercised end to end by voice on live sites.** Voice → task was tested with a scripted microphone/STT against the fake web (and Stop by voice); the live runs drove the tool layer directly. No human spoke a goal.
2. **The grammar is finite.** Unusual phrasing is not recognised as a goal (it falls to the Phase 20 router or the assistant). There is no model-based planner wired in (only the validated `from_proposal` hook).
3. **GitHub:** the README/repository steps were tested against a mocked GitHub API and, for the browser fallback, against public GitHub pages; a real token/OAuth-connected run was not possible here. A logged-out browser search cannot tell which repository is "mine" (the answer says so).
4. **README/technology analysis is heuristic** (section headings, keyword list), not understanding; "check the Projects section" reads the page as flat text and says it cannot prove containment.
5. **The dashboard, tray menu clicks and headed mode** were tested through the API/menu objects and headless runs, not visually.
6. **One task at a time**; no scheduled or background-triggered tasks; tasks never resume after a restart (by design).
7. **Visual/screenshot fallback and desktop-application control were not built** (the boundary is the browser and integrations; `ComputerState` still only has a browser part). No unlabeled-control targeting.
8. PostgreSQL variants (634 skips in total) were not run.

## Documentation

Created: `AUTONOMOUS_AGENT_ARCHITECTURE.md`, `AUTONOMOUS_AGENT_SECURITY.md`, `AUTONOMOUS_TASKS.md`, this report. Updated: `BROWSER_ARCHITECTURE.md`, `BROWSER_TOOLS.md`, `VOICE_ARCHITECTURE.md`, `CONFIGURATION.md`, `IMPLEMENTATION_LOG.md`, `architecture.md`, `README.md`.

## Recommended manual validation

1. `python -m desktop.launcher`, then say: "Hey JARVIS, search YouTube for Blinding Lights, play the official video and set the volume to 30%", then "Stop" mid-way on another goal.
2. Connect GitHub (`JARVIS_GITHUB_ENABLED`, token), then "Find my Virtual Campus repository and summarize its README" and "Find my latest repository and tell me what technology it uses".
3. Put a harmless test page with a download link behind a local server (or use `python scripts/browser_real_check.py`) and try "Find the report and download it": you should hear the choice question (if several) and then the confirmation; say "no" once and "yes" once.
4. Watch the dashboard *Autonomous task* panel and the tray while a task runs; try Pause/Resume/Stop.
5. Run `python scripts/autonomy_real_check.py --headed` and read the step table.
