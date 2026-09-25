# Security Model

## Core principle

The LLM must never have unrestricted access to the operating system, the
filesystem, the network, or any external account. All action flows through
a fixed chain:

```
LLM
 ↓
PermissionManager   (backend/core/security/)
 ↓
Tool                (agent/tools/base.py)
 ↓
External System     (integrations/, desktop/, ...)
```

The LLM proposes an action (e.g. "send this email"). The agent brain turns
that into a structured decision, the decision becomes a `PermissionRequest`
held by the manager, and a tool runs only after the manager authorizes exactly
that call (`Tool.execute` -> `PermissionManager.check`). A tool
implementation must never be called directly by LLM output, and must never
reach an external system without going through this check first.

## Phase 5 update

Phase 0's deny-everything placeholder has been replaced by a real permission
layer (typed requests, risk levels, scopes, policy, approval, expiry, action
binding, session scoping and an in-memory audit trail). It is still deny by
default: unknown tools, unknown/expired/mismatched/malformed requests and any
security failure are denied. See `docs/security-and-permissions.md`. The Phase 0
description below is kept for history.

## Phase 0 implementation

- `backend/core/security` (a module in Phase 0, now a package that still exports
  `PermissionManager`, `PermissionRequest` and `PermissionDenied`).
- The default policy is **deny by default**: in Phase 0 `authorize()` always
  returned `False`. (Phase 5 keeps that for anything without an approved,
  matching record.)
- No `Tool` subclasses exist yet, so nothing currently calls
  `PermissionManager`. The boundary is established ahead of the tools that
  will need it.

## Secrets and configuration

- All configuration, including future integration credentials, is sourced
  from environment variables via `backend/core/config.py` (`pydantic-settings`).
- Real secrets must live only in a local `.env` file, which is excluded by
  `.gitignore`. Only `.env.example`, containing placeholder values, is
  committed.
- A required configuration value with no safe default (e.g. `DATABASE_URL`)
  causes the application to fail at startup with a clear error rather than
  substituting a fabricated value.
- Logging (`backend/core/logging.py`) must never be passed secret values —
  log calls throughout the codebase should log identifiers and outcomes,
  not credentials or tokens.

## What is intentionally not implemented yet

- Consent prompts / a permission UI (Phase 21) and persistent audit storage.
  (Per-tool rules, scopes and an in-memory audit trail exist since Phase 5.)
- A credential vault. Gmail and Google Calendar keep OAuth tokens in local, git-ignored files (never logged or sent to the
  model; see docs/google-calendar-integration.md); messaging and other integrations do not exist yet.
- Sandboxing or process-level isolation for tool execution.

These are expected to be built out once concrete tools and integrations
exist in later phases, on top of the `PermissionManager` boundary
established here.

## Voice security (Phase 19)

Complements the sections above, `security-and-permissions.md`, `PROMPT_INJECTION_SECURITY.md` and `OAUTH_SECURITY.md`. This page states what the voice layer is and is not allowed to do, and the test that enforces each point.

**Voice input is untrusted text.** It is exactly as trusted as typed text from the user: no more (a spoken "yes" is not a signature) and no less.

| Guarantee | How | Enforced by |
|---|---|---|
| Voice reaches the agent through one call only | `VoiceEngine` calls `ConversationEngine.respond()` and nothing else; the agent decides, the Tool Router/PermissionManager/ConfirmationEngine execute | `test_engine_sends_recognized_text_only_to_the_conversation_engine` |
| The voice layer has no shell, filesystem, credential or integration access | `voice/` contains no `subprocess`, `os.system`, `eval/exec`, file writes, and imports no `integrations`, `agent.tools` or `backend.core.security` (only `bootstrap.py`, the composition root, wires services) | `test_voice_package_never_writes_audio_or_touches_the_shell_or_files`, `test_voice_layer_has_no_direct_access_to_tools_permissions_or_integrations` |
| Control words are not tasks | "Stop / Cancel / Wait" match only as the whole utterance (with optional "JARVIS"); "cancel my dentist appointment" is a normal request that still needs a confirmation | normalization + engine tests |
| Confirmations are not weakened by speech | destructive/external/sensitive actions still need the strict spoken yes from the confirmation engines (single use, bound to the exact action) | `test_cancelling_a_reminder_by_voice_still_needs_a_spoken_yes`, `test_a_spoken_yes_needs_a_real_pending_confirmation_and_a_confident_transcript` |
| A misheard yes/no cannot authorize or refuse | below `stt_min_confidence` the engine asks again and the text never reaches the confirmation | `test_a_low_confidence_yes_cannot_confirm_a_pending_action` |
| Corrections/clarifications use the same permission path | `answer_clarification` and `correct_reminder` call `resolve` → `PermissionManager.request_permission` → tool `execute`; a correction only touches a still-scheduled reminder created in this session | `test_conversation_natural.py` |
| A misheard answer cannot create something | a short reply completes a pending question only if it is not itself a new request; after 3 unusable answers the question is dropped | `test_a_new_request_is_not_mistaken_for_the_answer`, `test_repeated_unusable_answers_stop_the_questioning` |
| No fabrication | follow-ups answer only from the last real result and never guess "it"; unavailable integrations produce the hub's classified message; STT/LLM failures are spoken as failures | follow-up, scenario 8 and failure-injection tests |
| No raw audio stored | audio exists in memory for one utterance; nothing is written, logged or uploaded; the log and API carry text, states, timings only | `test_no_session_state_leaks_audio_to_disk`, `test_the_voice_log_is_structured_redacted_and_has_no_audio` |
| No secrets in logs or the dashboard | `VoiceLog` and `VoiceStatus` redact credential-shaped text (API keys, bearer tokens, `password=`…) | `test_voice_log_and_status_never_contain_credentials` |
| Microphone follows privacy mode | private/paused → the runtime releases the microphone; "Talk to JARVIS" never opens a closed microphone; the dashboard reports `MICROPHONE_CLOSED` | Phase 16 tests + `test_paused_runtime_reports_the_microphone_as_closed` |
| Voice API is local and authenticated | loopback Host check + per-run token; unknown/invalid settings rejected | `test_voice_api_needs_the_token_and_a_loopback_host`, `test_settings_can_be_changed_and_persist_and_bad_values_are_rejected` |
| DND/mute cannot be bypassed by low-priority text | priority comes from the sender (reminder scheduler, notification center), not from message text | policy + queue tests |

## Known limitations

* No speaker verification: anyone within earshot who says the wake word can talk to JARVIS. Anything that needs confirmation still asks, but the person answering could be someone else. Use Private mode when others are around.
* Speech recognition can mishear. The confidence guard covers confirmations; a misheard ordinary request is executed as heard and read back (e.g. the reminder time is spoken).
* `barge_in=vad` can be triggered by JARVIS's own voice through the speakers (no echo cancellation); the default `wake_word` mode avoids this.
* A wake word said by a TV can activate JARVIS; it then hears no speech (counted, health reports "noisy") or hears the TV (which is untrusted text like any other).
* Priority `critical` is only as trustworthy as its sender: Gmail-derived alerts are limited by the hub (only a critical-classified email produces one).
