# Autonomous tasks: what you can ask and what happens (Phase 21)

## Goals it understands

| Say | It does |
|---|---|
| "Open GitHub and find my Virtual Campus repository." | opens GitHub, finds the repository through the GitHub integration, opens it, checks the address |
| "Open the repository and summarize its README." | (repository already open: skipped) reads the README through the API, summarizes |
| "Open GitHub, find my Virtual Campus repository, open the README and summarize the setup requirements." | the above plus a summary of the requirements/installation sections ("Python 3.11; PostgreSQL 15 with pgvector; Node.js 20; Ollama…") |
| "Find my latest repository and tell me what technology it uses." | latest pushed repository, README, technologies found |
| "Search YouTube for Blinding Lights, play the official video and set the volume to 30%." | search, chooses the official video (asks if unclear), plays it, verifies playback, sets and verifies the volume |
| "Search the web for PostgreSQL documentation and open the most relevant official result." | search, ranks results by whether the site is the technology's own, opens it (asks if not clear) |
| "Open my portfolio at example.com, check whether the Projects section contains my Virtual Campus project, and tell me what you find." | opens, reads the page, checks for the heading and the name, reports honestly what it can and cannot tell |
| "Find the report and download it." (site open) | finds matches; several → asks which; then asks your confirmation before downloading; verifies the file |
| "Open https://jobs.example.com/apply, upload resume.pdf and submit it." | shows a preview; attaching the file asks your confirmation; submitting asks again |

Single commands ("Open GitHub", "Pause", "Play the official one") are handled by the Phase 20 router as before. If something needed is missing it asks ("What's the address of your portfolio website?") and then continues the same request.

## While a task runs

* "What are you doing?" / "Why did that fail?": status and reason.
* "Pause the task" / "Resume": between steps.
* **"Stop"**, "Cancel", "Abort", "Never mind", "Stop the task": cancels; nothing further runs. Steps already done (opening a page) are not undone.
* A question waiting for you ("Which one do you mean?", "Shall I go ahead?") is answered in your next sentence; "no" cancels, an unclear answer does nothing.
* JARVIS does not narrate every step. A quick task answers directly; otherwise "Got it… I'm working on it", at most two progress lines, then the result. The dashboard *Autonomous task* panel shows progress `3 / 6`, the current action, verification, risk and recent history; the tray shows the task and Pause/Resume/Stop.

## How it ends

`COMPLETED` (with the answer), `FAILED` (with the real reason: "I found Satellite report 2025, but the download failed: …"; "I couldn't open GitHub: That took too long."), `CANCELLED`, `BLOCKED` (needs you: sign-in, CAPTCHA, or no confirmation in time), never a "Done" that was not verified.

## Limits (all in `.env`, see `CONFIGURATION.md`)

`AUTONOMY_MAX_DURATION_SECONDS` 180 · `AUTONOMY_MAX_STEPS` 25 · `AUTONOMY_MAX_TOOL_CALLS` 40 · `AUTONOMY_MAX_RETRIES` 2 · `AUTONOMY_MAX_REPLANS` 3 · `AUTONOMY_LOOP_THRESHOLD` 3 · `AUTONOMY_MAX_CONSECUTIVE_FAILURES` 3 · `AUTONOMY_OBSERVATION_TIMEOUT_SECONDS` 10 · `AUTONOMY_CONFIRMATION_TIMEOUT_SECONDS` 120 · `AUTONOMY_BROWSER_TASK_TIMEOUT_SECONDS` 60 · `AUTONOMY_INLINE_WAIT_SECONDS` 25 · `AUTONOMY_VOICE_PROGRESS` true · `AUTONOMY_HISTORY_SIZE` 30 · `BROWSER_MAX_TABS`.

## Test map

`tests/autonomy/test_planner_router_analysis.py` (planning, validation, router, refs, analysis) · `test_execution_recovery_security.py` (the 10 scenarios, recovery, replanning, multi-turn, confirmations, cancellation, limits, loops, injection, persistence, shutdown, performance) · `test_wiring_api_tray_config.py` · `test_real_world.py` (**REAL_WORLD**: real headless Edge against a local site) · `scripts/autonomy_real_check.py` (live YouTube/Bing/GitHub, not in CI). Deterministic tests never touch a live website.

## Personal workflows (Phase 22)

Multi-*system* requests — email → deadline → task → reminder, briefings, meeting preparation, GitHub activity → tasks, "apply from the email" — are handled by the Personal Operator, not by the task engine above. See `WORKFLOW_TEMPLATES.md` for what it understands and `WORKFLOW_ENGINE.md` for how it runs. The controls are the same words ("Stop", "Cancel this", "Never mind", "Cancel the workflow", "pause", "resume", "what are you doing?" / "what's the workflow status?"); "Stop" stops both. A workflow never sends or replies to email, asks before anything that submits, uploads, deletes or publishes, and says "Done" only when it verified every change.
