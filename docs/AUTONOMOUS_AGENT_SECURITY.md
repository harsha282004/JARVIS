# Autonomous agent security (Phase 21)

Autonomy multiplies the consequences of a single mistake, so the design has one rule: **more steps never means more authority.** The agent can only do what one step could do through the same doors, each step is checked again, and consequential steps need the user's own yes at the moment they are reached. Read together with `BROWSER_SECURITY.md` and `security.md`.

## Boundaries

| Control | How |
|---|---|
| No new capability | tools = Phase 20 browser tools + read-only GitHub API tools + deterministic text analysis. No shell, file, credential, script, cookie or OS tool exists (tested by scanning the package and by refusing the names) |
| The tool router is the only door | planner and runner never import the browser driver, engine or hub; `ToolRouter.call` is the single caller of `BrowserTools.call`; unknown tools and extra arguments are refused |
| Permission Manager stays authoritative | browser steps run through `BrowserTools.call` (five categories, PermissionManager, `check_authorization`); the autonomy risk gate is *additional* |
| Risk is computed by code | from the tool, the arguments, the plan's wording, and again from the real values after resolution; a proposer cannot set `risk`, `permission`, `confirmed` or `status` |
| Task risk is the maximum | a harmless start does not make a sensitive finish low-risk; the preview lists every gated step |
| Confirmation | shared Phase 17 engine: single use, strict yes/no, bound to the step; unclear answers never confirm; declined = cancelled; timeout = blocked; the Phase 19 low-confidence voice guard applies |
| Sensitive things stay off-limits | passwords, cookies, MFA, CAPTCHA, arbitrary uploads/deletes, shell, security settings, system prompt: refused at planning (`refuse`) and again by the tools |
| Cancellation | voice Stop/Cancel, "stop the task", dashboard, tray, API, shutdown: the loop checks between every step and inside waits; the browser is asked to stop; nothing further runs (tested: no tool call after the stop) |
| Limits | duration, steps, tool calls, retries, replans, consecutive failures, loop threshold: configurable, all end the task safely |
| Retry safety | only steps marked safe (loading, reading, finding) are retried; a click, type, upload, submit or download is never repeated after an error, not even after a browser crash |
| Persistence | redacted summaries only; no blackboard, page or README text, credentials or history of actions; tasks never resume after a restart |

## External content is information, never authority

Websites, README files, search results, repository descriptions and (through the same rules) emails/documents are untrusted:

* **They cannot start a task.** Only the user's own words reach `AutonomyRouter`; `IntelligenceRouter.handle` ignores every non-user trust level (tested with `TrustLevel.EXTERNAL`).
* **They cannot choose tools or arguments.** Plans come from a grammar over the user's words. A value from a page can enter an argument only through a typed reference whose type is checked at run time: `repo` must be `owner/name` (no `..`), `url` must pass the browser URL policy (no `file:`, `javascript:`, private addresses), `role` must be a known control type, `int` an integer, `data`/`list` the right container; anything else is refused.
* **They cannot raise or hide risk.** Names read from a page can only *increase* a step's risk (a control named "Buy now" makes the step sensitive and pauses it).
* **They are never repeated as instructions.** The README summarizer drops every command line (`curl … | sh`, `powershell`, `iwr`, `sudo`, `rm -rf`, `Invoke-*`, redirections) and says "it also contains some shell commands that I did not repeat or run"; a README or page whose text looks like instructions to an assistant is flagged, the summary says so and nothing else changes; repository descriptions that look like instructions are not spoken.
* **They only produce answers.** The blackboard feeds deterministic extractors and the final wording; there is no model in the loop that could be persuaded.

Tested with hostile content from a website (in fake and real Edge), a README, search results (including `file:` and `127.0.0.1` links) and a repository description: the tool calls were exactly the planned ones, no upload/typing/key press/click/script/download happened, no key or prompt text was spoken, the private and `file:` results were never opened.

## Audit (Phase 21 §53)

| Area | Finding | Result |
|---|---|---|
| Tool authorization | one door; unknown/forbidden names, extra args refused | tested |
| Permission bypass | `confirmed` is set only by the runner after the confirmation engine; not a plan field (a hostile proposal setting it is ignored and priced by code) | tested |
| Prompt injection | see above | tested |
| SSRF | typed `url` refs and `open_url` use the URL policy; **found and fixed:** `same_site` compared only registrable domains, so "127.0.0.1:A" and "127.0.0.1:B" counted as the same site (a task reported an unreachable page as already open); ports/IPs now compare exactly | fixed + tested |
| Credential leakage | history/logs redact; dashboard shows step descriptions only; refused goals are stored redacted | tested |
| File access | none from the agent; uploads only via the confirmed `upload_file` tool from the approved folder (its `filename` schema now forbids path characters: **found and fixed** in review) | fixed + tested |
| Downloads | contained, recorded, never executed (Phase 20); task-level confirmation first | tested |
| Browser session isolation | dedicated profile; one browser thread | unchanged |
| Task persistence | summaries only, no auto-resume | tested |
| Cancellation | between steps and inside waits; browser stop requested; shutdown cancels | tested |
| Race conditions | one task at a time under a lock (two callers racing to start); state reads under a lock; the confirmation engine is keyed by session and touched from the voice thread and the task thread only through single dict operations (CPython-atomic) | reviewed |
| Retry safety | unsafe steps never blindly repeated; loops bounded | tested |
| Deception | a public GitHub search result is not called "your repository"; failures are never reported as done | **found by the real run, fixed + tested** |

## Residual risks

* The category of an unlabeled, icon-only control cannot be recognised by words (Phase 20 limitation): consequential steps are only as visible as their labels.
* One session, one active confirmation: a new confirmation request in the same session supersedes the pending one (the task then treats it as declined: safe).
* Anyone who can speak the wake word can start a task and answer its confirmation (no speaker verification): use Private mode when others are around.
* Deterministic grammar means unusual wording is not understood (it falls back to the Phase 20 router or the assistant): the safe direction.
