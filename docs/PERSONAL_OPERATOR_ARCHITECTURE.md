# Personal Operator architecture (Phase 22)

The Personal Operator coordinates the user's own systems — Gmail, Google Calendar, Tasks, Reminders, GitHub, indexed Documents, Personal Memory, the controlled browser, Notifications and Voice — to complete **end-to-end workflows** such as:

- "Find the internship email, identify the deadline, create a task for it, and remind me two days before."
- "Check tomorrow's calendar and related emails, then prepare my morning briefing."
- "Find my GitHub activity from this week and add anything important to my task list."
- "Check my upcoming deadlines and tell me what I need to finish this week."

It is a **controlled operator, not an autonomous agent**. It cannot send, reply, forward, delete, publish, purchase, upload or change an account (no such tool exists), it never calls an integration directly, and text that comes from an email, page, document, issue or calendar description is data — never an instruction.

```
voice / text / API
  -> ConversationEngine -> IntelligenceRouter
       -> confirmations.respond   (a "yes"/"no" for a waiting question; "Stop/Cancel" is intercepted first, see below)
       -> OperatorIntentRouter    answers to a waiting workflow | controls | new workflow request
       -> AutonomyRouter -> HubRouter -> BrowserRouter                       (Phases 21 / 18 / 20, unchanged)
  PersonalOperator  (workflows.operator)  owns workflows: start, answer, pause, resume, cancel, confirm, recover, proactive suggestions
    WorkflowPlanner (workflows.planner)   deterministic grammar -> template -> availability -> validate -> risk/permission by code -> preview
    WorkflowRunner  (workflows.runner)    one thread per workflow: dependency-ordered steps, data flow, confirmation gate, retries (reads only), checkpoints, result
    OperatorRouter  (workflows.tools)     the only door:  operator tools  |  browser tools via the Phase 21 ToolRouter -> BrowserTools -> PermissionManager
      operator tools -> Integration Hub tools (registry gate: enabled, connected, permission) | TaskService / ReminderService | Memory | NotificationCenter
    WorkflowStore   (workflows.store)     checkpoints, effect ledger, entity links, history, redacted audit log (all minimal, no message text)
```

The mandated chain is **Operator → Task Engine (workflow runner) → Tool Router → Permission Manager → Tool → Verification**. A guard test (`test_the_operator_never_imports_an_integration_directly`) fails if any file in `workflows/` imports an integration package, a browser driver, `subprocess`, `eval` or `exec`.

There is **no model call** in planning, execution, verification or the briefing. Every template works offline (a test asserts zero LLM calls). A model-proposed plan can be submitted with `PersonalOperator.submit_proposal`; it goes through exactly the same validation (§ Planner). When a compound request matches no template and no model is configured the operator says: "I can handle that workflow, but the conversational model is currently unavailable."

## Modules (`workflows/`)

| File | Role |
|---|---|
| `models.py` | `Workflow`, `WStep`, `Fact` (with provenance), `From` (typed reference), `WStatus`, `SStatus`, `FailureKind`, `FactStatus`, `WorkflowResult` |
| `facts.py` | fact classification (VERIFIED … UNVERIFIED), conflict detection, source priority |
| `priority.py` | daily priority synthesis with reasons; the spoken briefing wording |
| `store.py` | `WorkflowStore`: checkpoints, write-ahead effect ledger, entity links, history, audit |
| `tools.py` | operator tool registry (pydantic schemas, `extra=forbid`), `OperatorRouter`, hub-error → failure classification |
| `templates.py` | 13 workflow templates and the report wording |
| `planner.py` | grammar, availability detection, scope, validation, risk/permission, preview, `from_proposal` |
| `runner.py` | the dependency-aware runner, confirmation gate, limits, result assembly |
| `operator.py` | `PersonalOperator` (lifecycle, recovery, proactive) and `OperatorIntentRouter` (conversation front door) |
| `build.py` | production wiring from settings |

Changed elsewhere: `GitHubAdapter.identity()` (which account the token belongs to); `IntelligenceRouter` (operator router first; `intercept_cancel` before the confirmation engine; `cancel_task`/`task_active` cover workflows); composition, CLI cleanup, `AppContext.operator`; `/workflows*` API; the "Personal Operator" dashboard panel; tray items (Pause/Resume/Stop apply to whichever of task or workflow is running); 11 `WORKFLOW*` settings.

## Workflow model

`WStatus`: `DRAFT`, `PLANNING`, `READY`, `RUNNING`, `WAITING_FOR_DATA` (a required system is unreachable or signed out; nothing was changed; "try again" re-runs), `WAITING_FOR_USER` (a question: which date, which link), `WAITING_FOR_CONFIRMATION`, `PAUSED`, `FAILED`, `COMPLETED`, `CANCELLED`.

A step (`WStep`) has: `step_id`, `description`, `tool`, `arguments`, `dependencies`, `expected_result`, `verification` (rule names), `source` (the system it touches), `retry_policy`, and — computed by code, never taken from a plan — `risk`, `permission`, `side_effect`. Status, output, note, failure kind and idempotency key are runtime state.

## Structured data flow and provenance

Values move between steps only as structured objects through `From(step, path)` references to steps the step declares as dependencies. The validator refuses a reference to an undeclared step, and a `url` argument is re-validated when it is resolved. Steps never take a free string from an email, page or document as an argument except where the value *is* the user-visible data (a task title taken from the extracted sentence, sanitized and bounded).

A `Fact` records `source`, `source_id` (Gmail message id / document id / `repo#issue`), `timestamp`, `confidence`, the sentence it came from (`original_text`, kept in memory only), and a **status**:

| Status | Meaning | May trigger a task/reminder? |
|---|---|---|
| `VERIFIED` | explicit date, unhedged, high extraction confidence, not suspicious; or read directly from an authoritative API | yes |
| `HIGH_CONFIDENCE` | high confidence, relative/partial date ("by Friday") | yes |
| `LOW_CONFIDENCE` | medium/low extraction confidence | no — "I found a possible deadline, but I'm not confident enough in it to act on it." |
| `AMBIGUOUS` | hedged wording ("probably", "sometime in mid October", "TBD") or conflicting sources | no — "I found a possible deadline, but the email doesn't state it clearly." |
| `UNVERIFIED` | the text looks like instructions to an assistant, or there is no evidence sentence | no — "…the text looks like it is trying to give me instructions, so I treated it as content only." |

The write tools (`task_create`, `reminder_create`, `task_create_batch`) re-check the status themselves, so a mis-planned or hand-built call still cannot act on an uncertain fact. Provenance is stored on the created object (`metadata.provenance`, `metadata.idempotency_key`, `metadata.workflow_id`).

**Source priority** when sources disagree: current verified source > recent integration data > trusted document > personal memory > inference. Memory is context only: it never overrides a current source and never lowers a confirmation requirement.

**Conflicts are surfaced, never resolved silently.** If the email says October 15 and the calendar has "Internship application deadline" on October 17, the workflow stops in `WAITING_FOR_USER`: "I found conflicting dates: the email says October 15, while your calendar has October 17 for 'Internship application deadline'. Which is right?" The user's answer becomes the step's output; the calendar entry is never modified. Cross-system links require an explicit basis (same message id, created-from), never vague similarity.

## Multi-integration coordination

Availability is detected at plan time from the registry gate (`enabled`, `configured/connected`, `permission granted`). A missing *required* system refuses the plan honestly ("I can't do that yet: Gmail is not connected"); a missing *optional* system is left out and disclosed in the warnings and the spoken result ("I couldn't check your calendar, so that isn't included"). GitHub results are never called "yours" unless the connected token's identity was read first (`github_identity`), and a request to a workflow without GitHub connected performs no GitHub request at all.

Workflow **scope** is derived from the request: "check my important emails" is Gmail only (a test asserts the calendar and GitHub are never touched). The one implicit read is the calendar cross-check inside deadline workflows (read-only, listed in the scope, disclosed when skipped).

## Deduplication and idempotency

- Tasks and reminders are keyed by source + source id + date + kind. A **write-ahead ledger** (`workflow_effects.json`) records `begun` before and `done` (with the object id) after each creation. A second run, a resume after a crash, or eight concurrent threads all find the ledger entry or an existing object with the same idempotency key/title/due and adopt it ("The task 'X' already exists, so I didn't add a duplicate").
- Browser side effects (a confirmed click) are ledgered per `workflow:step`; after a restart the runner refuses to repeat one that may already have happened.
- Notifications carry a dedupe key (six hours); identical running workflows in one session are one workflow.

## Recovery

Checkpoints (`workflow_checkpoints.json`) hold only: workflow id, goal (redacted), template, status, session, requested-by, risk, scope, primitive params, pending step, updated-at, completed step ids, current step, and per step `{id, status, whitelisted output keys, note, idempotency key}` plus facts **without** their evidence sentences. On start-up `PersonalOperator.recover()` rebuilds each unfinished workflow from its template and params, validates it like any plan, overlays the saved step state, and loads it **`PAUSED`** — nothing runs, and the user is told ("I found an unfinished workflow from before… Say resume to continue it, or cancel"). On resume, reads whose output was not persisted are simply re-read; writes are never re-run, they are adopted from the ledger. A cancelled workflow is never recovered again. A damaged or hand-edited checkpoint that fails validation is discarded and marked cancelled.

## Failure classification

`TEMPORARY`, `AUTHENTICATION`, `PERMISSION`, `DATA_MISSING`, `AMBIGUOUS`, `VERIFICATION_FAILED`, `EXTERNAL_SERVICE`, `SECURITY_BLOCK`, `USER_CANCELLED`, `RESOURCE_LIMIT`. Hub errors map deterministically (`AUTH_ERROR`/`CONFIGURATION_ERROR` → AUTHENTICATION, `PERMISSION_ERROR` → PERMISSION, network/rate-limit/server → TEMPORARY, `NOT_FOUND` → DATA_MISSING, invalid → SECURITY_BLOCK). Behaviour: TEMPORARY reads are retried (at most `WORKFLOW_MAX_RETRIES`); a write is never retried; TEMPORARY/AUTHENTICATION/EXTERNAL_SERVICE with no write done → `WAITING_FOR_DATA` (retry with "try again"); everything else ends `FAILED` with the reason. An optional step that fails is a warning and its dependants that need its data are skipped; a required failure blocks its dependants ("Step 4 must not execute if Step 3 failed") and the result lists what was blocked and why.

## Results

`WorkflowResult`: `status`, `summary`, `actions_completed`, `actions_skipped`, `actions_blocked`, `sources`, `warnings`, `duration_s`, `suggestions`. **"Done" is only said when every side-effect step was verified by reading the object back.** A partial outcome is described as partial ("I did part of that…", "Already done: … Not done because of that: …").

## Voice and notifications

The operator's text replies are the voice replies. "Good morning. You have three meetings today, two high-priority emails, and one upcoming deadline." is produced from counts of the synthesized items; "Tell me more" (within 30 minutes of a briefing) speaks the reasons behind the top items from the same synthesis. Long-running workflows deliver their result through the announcement queue (Do-Not-Disturb and priority handled by the Phase 19 policy). Voice never lowers a requirement: a spoken "yes" goes through the same `ConfirmationEngine`, single use, bound to the exact steps.

## Proactive intelligence

On `DEADLINE_DETECTED` from Gmail the operator (if `WORKFLOW_PROACTIVE_ENABLED`) runs a **suggestion-only** review — throttled (10 minutes), never while the user's own workflow is running. Write steps of a proactive workflow are skipped and reported as suggestions; tools additionally refuse writes from a non-user workflow. The user hears once per deadline: "I noticed a deadline in an email: … Say 'turn that into a task' if you want me to add it." — and that follow-up runs the normal workflow from the remembered fact.

## Performance

Measured through `metrics`: `workflow.planning_ms`, `workflow.step_ms`, `workflow.total_ms`, `operator.tool.<name>_ms`; counters `workflow.started/completed/failed/cancelled`. See `PHASE_22_IMPLEMENTATION.md` for measured numbers. There is no model call and no per-step database scan beyond the integration reads the step exists for.

## Not in scope

No email/message sending, replying, forwarding; no calendar creation or edits (the existing confirmed calendar actions are unchanged); no unattended browser sessions; no model planning unless a proposer is plugged in. See the limitations in `PHASE_22_IMPLEMENTATION.md`.
