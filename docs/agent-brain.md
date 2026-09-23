# Agent Brain (Phase 4)

Phase 4 adds the first reasoning layer. For each user request the Agent
Brain decides what is being asked, what kind of request it is, whether it
needs an action, what a plan would look like, and what JARVIS should say.
It produces a **structured decision, not an action**: nothing in Phase 4
executes anything, and no real tools exist.

## Architecture

```
VoiceEngine                 audio / wake word / STT / TTS
    v  text
ConversationEngine          session, history, context window (sole owner of history)
    v  request + recent history
AgentBrain                  intent, decision, plan, tool selection by name
    |-- Planner             deterministic plan from the validated decision
    v
LLMProvider -> OllamaProvider     model abstraction (json_mode hint)

Future, not built:  decision -> PermissionManager -> Tool -> external system
```

| Module | Responsibility |
|--------|----------------|
| `agent/brain/brain.py` | `AgentBrain.decide(request)`: prompts the LLM, validates, builds the decision |
| `agent/brain/models.py` | `Intent`, `AgentRequest`, `AgentDecision`, `ToolSelection`, `AgentError` (Pydantic) |
| `agent/brain/parsing.py` | Strict parsing/validation of the LLM's JSON |
| `agent/brain/prompts.py` | Decision prompt and tool catalog text |
| `agent/planner/` | `Plan`, `PlanStep`, `Planner` |
| `agent/tools/base.py` | Existing `Tool` interface, plus `ToolDescriptor` and `Tool.descriptor()` |
| `backend/core/conversation/engine.py` | Optionally delegates the reply to an `AgentBrain` |

`ConversationEngine` calls the brain (`agent=` argument, wired in
`voice/bootstrap.py` when `JARVIS_AGENT_ENABLED=true`); `VoiceEngine` is
unchanged. The brain keeps no history: it receives the recent messages from
`ConversationEngine` on every call. With the agent disabled (or absent),
`ConversationEngine` behaves exactly as in Phase 3.

## Intent categories

Kept deliberately small (`Intent`):

| Intent | Meaning | `action_required` | Reply comes from |
|--------|---------|-------------------|------------------|
| `conversation` | greetings, thanks, small talk | false | the LLM (`response`) |
| `information_request` | a question answerable from general knowledge | false | the LLM (`response`) |
| `action_request` | the user wants JARVIS to *do* something | true | fixed safe text |
| `clarification_required` | too vague to act on, even with the conversation | false | the LLM's question |
| `unsupported_request` | something JARVIS cannot or should not do | false | fixed safe text |

Classification is done by the LLM (one JSON call, so the direct answer costs
no extra round trip); the code validates it and never trusts it further than
that.

## Decision model

`AgentDecision`: `intent`, `action_required`, `plan` (action requests only),
`response`, `selected_tools`, `requires_permission`, `confidence` (0-1),
`reasoning_summary` (one short sentence, capped at 200 chars, never
step-by-step reasoning), and `error` (a structured `AgentError`, set only when
the output was invalid). Invariants are enforced by validation: only
`action_request` may carry a plan, tools or a permission need, and `response`
is never empty.

For action requests and unsupported requests the spoken `response` is a
fixed template, never the model's text, so JARVIS cannot claim "done, email
sent". In Phase 4 that text says it can't carry out actions yet.

## Planning

`Planner.build_plan` is deterministic:
`prepare steps (model-suggested, cleaned, de-duplicated)` -> `permission step`
(if permission is required) -> one `execute` step per tool. It is capped by
`JARVIS_AGENT_MAX_PLAN_STEPS`; when trimming, permission/execute steps are kept
over preparation detail. Example for "send an email to John":
identify the recipient, prepare the message, ask for permission, use the
`email` tool. The plan describes work; **nothing runs it**.

## Tool selection

The brain is given `ToolDescriptor`s (`name`, `description`, `input_schema`,
`requires_permission`), never `Tool` objects, so it cannot call `run`. Tools
named by the model are matched by name (case-insensitively) against the
catalog. Each `ToolSelection` records `available` and `requires_permission`.
A name with no matching tool is kept as `available=False` (see
`decision.missing_tools`) and treated as needing permission. Phase 4 ships
with an empty catalog, so today every action request has a missing tool: the
decision correctly says "an email tool would be needed" without one existing.

`requires_permission` is true unless every selected tool is known and
declares it does not need permission (no tools named also means true).
`decision.permission_requests()` builds the Phase 0 `PermissionRequest`s a
future executor must submit to the `PermissionManager`; building them
authorizes nothing (the manager still denies by default).

## Safety boundary

```
LLM -> (text) -> parse/validate -> AgentDecision (data) -> [future] PermissionManager -> Tool -> external system
```

- LLM output is only ever parsed as JSON data into a strict model. It is never
  evaluated, imported, or passed to a shell; unknown keys (including any
  "code" or "command" fields) are ignored.
- The brain holds descriptors, not tools, and the `agent/brain` and
  `agent/planner` packages import no execution or network primitives (a test
  scans them for `subprocess`, `os.system`, `eval`, `exec`, `importlib`,
  `socket`, `httpx`, `requests`, `.run(` ...).
- Tool names from the model are inert strings; hostile names are just
  reported as missing tools.
- The brain does not call the `PermissionManager` because it executes nothing.
  Since Phase 5 the manager is a full authorization layer and `ConversationEngine`
  turns action decisions into permission requests through it (see
  `docs/security-and-permissions.md`); the brain itself still has no reference to it.

## Structured output and errors

The LLM is asked for JSON (`LLMProvider.chat(..., json_mode=True)`; Ollama
maps this to `format: "json"`), but the output is still validated: JSON is
extracted (code fences and surrounding prose tolerated), then checked against
a strict model (valid intent, `confidence` in 0-1, list types, non-empty
`response` for direct intents).

- Invalid output gets **one** repair attempt (the model is told to reply with
  JSON only). If it is still invalid, the brain returns a safe fallback
  decision (`conversation`, no tools, no plan, apology text, `confidence` 0)
  with `error.code = invalid_output`. Nothing is executed. The error detail
  names the problem (e.g. `intent: Input should be ...`) and never echoes the
  model's output.
- LLM unavailable (`LLMProviderError`): raised, not fabricated. Because
  `ConversationEngine` only records a turn after success, the history is
  untouched and the next turn works; the Phase 2 runtime records it as
  `last_error` and keeps listening, as before.
- Unexpected exceptions (bugs) are not swallowed; they reach the runtime,
  which shows ERROR.

## Configuration

| Setting | Default | Meaning |
|---------|---------|---------|
| `JARVIS_AGENT_ENABLED` | `true` | `false`: plain LLM answers as in Phase 3 |
| `JARVIS_AGENT_MAX_PLAN_STEPS` | `8` | Maximum steps in a generated plan (min 1) |

## Privacy and logging

Logs contain intent, confidence, tool names, counts and the short reasoning
summary (e.g. "User wants to send an email."), not the user's words,
transcripts, plan text or model output. The brain stores nothing; the plan and
decision live only in memory (`ConversationEngine.last_decision` holds the
latest decision). No chain-of-thought is requested, stored or logged.

## Testing

Automated, with a scripted fake LLM (`tests/test_agent_models_planner.py`,
`tests/test_agent_brain.py`, `tests/test_conversation_agent.py`): request/decision
validation, each of the five intents, plan generation and limits, tool
selection (registered, missing, mixed, hostile names), permission flags and
denial by `PermissionManager`, malformed/invalid/oversized output, retry and
fallback, LLM failure (including on retry), context propagation ("Find
information about Python" then "Now explain decorators"), no tool execution,
and the no-execution-primitives scan. These prove the handling logic, not how
well a real model classifies.

Integration (`tests/integration`, self-skipping):
`test_real_ollama_agent_brain_classifies_info_and_action_requests` runs the
brain against a real Ollama for "What is Python?" and "Send an email to John
saying hello." (it only inspects the decision; nothing is sent).

## Limitations

- Classification quality depends on the local model; small models may return
  bad JSON (handled by retry/fallback) or misclassify. Nothing more than the
  intent, tool names and plan text is trusted from the model.
- A model can still put a false claim in the `response` of a `conversation` /
  `information_request` (e.g. "I've sent it"); only action/unsupported replies
  are template-guarded. The system prompt forbids it, but that is a prompt-level
  safeguard.
- No tools, no execution, no permission workflow or UI (Phase 5), no memory, no
  multi-step autonomy: every action request currently ends in "I can't carry
  out actions like that yet".
- Each turn now costs one LLM call (two when the output must be repaired).
