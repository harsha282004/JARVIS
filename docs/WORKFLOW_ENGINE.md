# Workflow engine (Phase 22)

How a request becomes a checked, resumable sequence of steps. Read `PERSONAL_OPERATOR_ARCHITECTURE.md` first for the overall picture.

## 1. Planning

`WorkflowPlanner.plan(text, session, last_fact=None, requested_by="user")` returns a `PlanOutcome`:

| kind | meaning |
|---|---|
| `plan` | a validated `Workflow` in `READY` with risk, scope and a preview |
| `ask` | a clarifying question ("Which email should I look at?") — nothing was started |
| `refuse` | for example a request to send, reply to or forward email |
| `unavailable` | a required system is not connected, off, or lacks the permission; the message names it |
| `none` | not a workflow request: other routers handle it ("what time is it", "add a task to buy milk", "check my emails") |

The grammar is regular expressions over the user's own words: template selection ("email" + "deadline" + "task"/"remind" → a deadline template), parameters (topic from "the *internship* email", "*two days* before", "the day before", "a week before"), a follow-up on the previous fact ("turn that into a reminder"), scope and options ("and notify me"). It is offline and needs no model. Ordinary single requests are deliberately *not* matched so the existing task, reminder, Gmail, calendar and browser routers keep handling them.

Validation (`validate`) — applied to grammar plans, model proposals and rebuilt checkpoints alike:

- at least one step, at most `max_steps`; step ids unique; at most `max_systems` integrations;
- every tool exists in the operator registry (or is one of six browser tools) and is not on the forbidden list (`send_email`, `reply_email`, `forward_email`, `execute_shell`, `delete_file`, `read_file`, `submit_form`, `purchase`, …);
- argument names belong to the tool's schema (`extra=forbid`); literal values pass the schema; references (`From`) may only point to **earlier, declared dependencies** — which also makes the graph acyclic; a `$from` in a proposal must match `s\d+.path` and nothing else;
- a step's `source` must be the system the tool really belongs to and be inside the workflow's scope.

Then, by code: `risk` and `permission` per step (browser tools through `ToolRouter.risk_of`: a "Submit"-like button is EXTERNAL_EFFECT/SENSITIVE whatever the plan calls it), the workflow's risk = the maximum, `side_effect`, the retry policy (`safe` only for reads), and the preview ("Plan: 1. … I'll ask you before I submit the application.").

## 2. Execution

`WorkflowRunner.run()` walks the steps in order (validation guarantees dependencies come first):

1. **Alive?** cancel flag, pause, time limit, tool-call limit.
2. **Dependencies.** A failed/blocked dependency → the step is `BLOCKED` ("blocked because 'Pick the one dependable deadline' didn't work"). A skipped dependency whose data the step needs → `SKIPPED` (unless the reference is `optional` — the briefing degrades instead — or has a `fallback`).
3. **Resolve arguments** from earlier outputs (`url` values are re-validated: private, non-http and credentialed addresses are refused).
4. **Suggestion-only** workflows skip side-effect steps.
5. **Confirmation gate.** A step at EXTERNAL_EFFECT or above needs the user's yes. All pending gated steps are grouped into one question built from what was actually read ("I've opened the page on jobs.example.com and read it. The documents say it needs: …. I'm ready to submit the application. This can't be undone. Do you want me to go ahead?"). The request goes through the shared `ConfirmationEngine` (strict yes/no, single use, bound to the exact steps, external text cannot answer). The workflow status becomes `WAITING_FOR_CONFIRMATION` only after the request is registered. Declined → those steps are skipped and the rest continues; timeout → the workflow fails with "I didn't get your confirmation in time, so I did not do it"; no confirmation channel → consequential steps never run.
6. **Execute** through `OperatorRouter.call` (never raises). Reads that fail `TEMPORARY` are retried up to `min(step retries, WORKFLOW_MAX_RETRIES)`. A side-effect step holds the effects lock across the cancel check and the write, so a stop cannot interleave with a half-made effect.
7. **Verify.** The tool's own read-back (`verified`), plus the step's rules: `readback`, `has:<key>`. A "successful" write that cannot be read back is recorded as `VERIFICATION_FAILED`, not success.
8. **Record.** DONE / SKIPPED (optional failure → warning) / FAILED; ambiguity with choices → `WAITING_FOR_USER` with a numbered question; checkpoint + audit line after every step.

Ending: `COMPLETED` if nothing failed; `WAITING_FOR_DATA` for external, retryable causes when no write was done; otherwise `FAILED` with the first failure's reason; `CANCELLED` on stop.

## 3. Answers, controls and confirmation from any surface

- **Answers.** While a workflow is `WAITING_FOR_USER` the router matches the reply to the choices ("the email", "the second one", "2"); the step is completed with that output and the runner continues in a new thread. For a date conflict the chosen fact overrides the verified one (`From(compare, "fact", fallback=From(verify, "fact"))`), and is marked VERIFIED with a note that the user chose it.
- **Cancel.** "Stop.", "Cancel this.", "Never mind.", "Cancel the workflow." (and voice Stop, the tray, `POST /workflows/{id}/cancel`): future steps do not run, a pending confirmation is cleared, the browser's current action is stopped, completed effects stay (they are reported, not undone), history is kept, and nothing resumes by itself. `IntelligenceRouter.handle` calls `intercept_cancel` **before** the confirmation engine so a "cancel" while a question is open ends the workflow instead of being read as a "no" that lets the rest continue.
- **Pause / resume.** Pause takes effect at the next step boundary; recovered workflows start paused; "try again"/"resume" continues (writes are adopted from the ledger, never repeated).
- **Confirm/Decline** from the dashboard call the same engine with a "yes"/"no" for that workflow's session.

## 4. Limits and concurrency

`WORKFLOW_MAX_DURATION_SECONDS` (240), `_MAX_STEPS` (14), `_MAX_TOOL_CALLS` (30), `_MAX_RETRIES` (2, reads only), `_MAX_SYSTEMS` (5), `_MAX_CONCURRENT` (2 running; at most three times that many unfinished). Exceeding a limit is `RESOURCE_LIMIT`: the workflow stops and says so. Each workflow has its own runner, outputs and checkpoint; the task/reminder creation lock and the browser lock serialize what cannot run concurrently; two workflows that need to ask in the same session queue their questions.

## 5. State on disk (`<state>/workflows/`)

| File | Content |
|---|---|
| `workflow_checkpoints.json` | minimal safe checkpoints (see architecture) |
| `workflow_effects.json` | the effect ledger: key → `{state: begun|done, kind, object_id, workflow_id}` |
| `workflow_links.json` | entity links with reason, confidence, source |
| `workflow_history.json` | redacted summaries of finished workflows |
| `workflow_audit.jsonl` | one redacted line per event: `workflow_planned/start/end`, `step_start/done/failed/blocked`, `confirmation_requested/approved/declined/timeout`, `user_answer`, `workflow_cancelled`, `workflow_recovered` (workflow, step, tool, source, risk, result, verification, timestamp — never a payload) |

None of these contains email, page, document or message text, tokens, or the evidence sentences of facts.
