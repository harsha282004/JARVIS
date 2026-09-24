# Security and Permissions (Phase 5)

Phase 5 turns the Phase 0 deny-by-default `PermissionManager` into a real
authorization layer. It builds the security boundary **before** any tool
exists: there are still no real tools, and Phase 5 executes nothing and has no
external side effects.

> **Since Phase 9** the first concrete tools exist (local task and reminder tools); see "Task and reminder tools
> and this boundary (Phase 9)" below. Everything else is unchanged.

## Threat model

What we defend against, and what we do not:

| Threat | Defence |
|--------|---------|
| The LLM (or prompt injection in text it reads) asks for an action | The brain only produces data; only the PermissionManager can approve; a person approves out of band |
| The LLM claims approval ("approved": true, "Approved and sent!") | Ignored: model output is never an input to authorization |
| The LLM invents a tool or a hostile identifier (`email; rm -rf /`) | Tools are recognised only from the manager's own registry; anything else is DENIED and inert |
| An approval for action A is reused for action B | Approval is bound to a digest of (tool, action, parameters); B hashes differently, so DENY |
| A stale approval is reused | Expiry, one-time consumption, session end |
| A forged or edited request object is presented | The manager reads status/binding from its own record, never from the presented object |
| Security code fails or is missing | Every failure path returns DENY |

Out of scope: a compromised local process or a user with control of the
Python process (they can call `approve` directly; this is an in-process
guard, not an OS sandbox), attacks on the model itself, and anything about the
audit trail surviving a crash (it is in memory).

## Security boundary

```
User -> VoiceEngine -> ConversationEngine -> AgentBrain -> AgentDecision (data)
                                                  |
                       request_permissions()      v
                                 PermissionManager  --(explicit approve/deny by a person, out of band)
                                                  |
                                     Tool.execute() -> check() -> Tool.run() -> external system   [no real tools yet]
```

- `AgentBrain` holds descriptors, not tools or the manager, so it cannot
  approve or run anything.
- `agent/brain/permissions.py::request_permissions` only *asks* the manager
  for a request per tool the decision names. It ignores the decision's own
  claims (`available`, `requires_permission`).
- `Tool.execute(permissions, request_id, **parameters)` is the sanctioned way
  to run a tool: it calls `PermissionManager.check` with the real parameters and
  raises `PermissionDenied` unless that returns ALLOW (also when the manager is
  missing or raises). Nothing in JARVIS calls it yet. Python cannot stop code
  that calls `run()` directly; that is a convention plus review/test
  discipline until tools exist, and the brain never has a Tool to call.

## Permission model

`PermissionRequest` (immutable): `request_id` (UUID hex), `tool_name`,
`action`, `description` (human readable, code-generated), `risk`, `scope`,
`session_id`, `action_digest`, `created_at`, `expires_at`, `requested_by`,
`status`. Phase 0 code that built `PermissionRequest(tool_name=..., action=...)`
still works with safe defaults (HIGH risk, ONE_TIME, PENDING).

Statuses: `PENDING`, `APPROVED`, `DENIED`, `EXPIRED`, `CANCELLED`, and
`CONSUMED` (a ONE_TIME approval that was used).

```
PENDING  -> APPROVED | DENIED | EXPIRED | CANCELLED
APPROVED -> EXPIRED | CANCELLED | CONSUMED (one-time, on first successful check)
DENIED / EXPIRED / CANCELLED / CONSUMED are terminal
```

A request returned to a caller is a snapshot; the authoritative status lives
inside the manager. Timestamps must be timezone-aware (naive ones are rejected).

## Risk levels

`RiskLevel`: LOW (read-only local info), MEDIUM (create/modify an external
resource), HIGH (send an email/message), CRITICAL (destructive or irreversible).
Tools declare their own risk (`Tool.risk`, `ToolDescriptor.risk`); the default
is HIGH so an undeclared tool is treated cautiously. The LLM never sets risk.

## Scopes

- `ONE_TIME` (default): authorizes one execution, then becomes CONSUMED.
- `SESSION`: reusable within one conversation session. Must carry a
  `session_id`; when `ConversationEngine` ends that session (timeout or reset)
  the manager expires every open request bound to it.
- `PERSISTENT`: representable, but never granted automatically: approving it
  needs `approve(..., confirm_persistent=True)`, it only lapses if an expiry
  is given, and it is stored only in memory (there is **no** persistence or
  management UI in Phase 5, so it does not survive a restart).

A tool lists its `allowed_scopes` (default ONE_TIME only), and the policy
denies SESSION/PERSISTENT for HIGH and CRITICAL risk.

## Policy (deny by default)

Decided only by `PermissionPolicy` plus the tool's registered security info:

| Situation | Outcome |
|-----------|---------|
| Tool not registered | DENY (`unknown_tool`) |
| Scope not allowed by the tool, or reusable scope on HIGH/CRITICAL | DENY (`scope_not_allowed`) |
| LOW risk and tool says no permission needed | ALLOW automatically (recorded as approved by policy) |
| Anything else | REQUIRE_APPROVAL (request stays PENDING) |

A tool claiming `requires_permission=False` above LOW risk still needs a
person. "Unknown -> deny" is an invariant, deliberately not a setting.

## Approval model

`approve / deny / cancel / expire(request)` are the only ways to reach those
states. Each verifies that the request exists, matches the stored record
(tool, action, digest, scope, session), is in a state that allows the change,
and has not expired. Approval after expiry, of a different action, of a
non-pending request, or of an unknown tool is rejected with a
`PermissionStateError`. There is no graphical UI (the permission center is
Phase 21); the API is programmatic. Nothing in JARVIS calls `approve` yet.

Never counted as permission: an LLM "yes", model JSON claiming approval, a tool
name being present, an unrelated earlier approval, a malformed request, a
missing record.

## Authorization check

`PermissionManager.check(request_id, tool_name=, action=, parameters=, session_id=)`
returns an `AuthorizationResult(allowed, code, reason)` and never raises for a
denial. It denies unless: the id is known, the tool is registered, the
(tool, action, parameter digest) equals the approved one, the session matches
(when bound), the request is APPROVED and not expired, and the policy still
allows it. A ONE_TIME approval is consumed by the first ALLOW. Phase 0's
`authorize(request) -> bool` and `require(request)` still exist on top of it.
Reason codes: `ok, unknown_tool, unknown_permission, not_approved, expired,
already_used, action_mismatch, session_mismatch, session_ended, missing_scope,
scope_not_allowed, policy_denied, malformed, manager_unavailable, manager_error`.

## Action binding and integrity

`action_digest = SHA-256(canonical JSON of {tool, action, parameters})`
(sorted keys, compact separators, ASCII). At request time the digest is stored
(not the parameters); at check time the parameters about to be used are hashed
again and compared. This detects a different action being swapped in after
approval. It is **not** a signature or a secret token: it protects against
substitution inside this process only, and a low-entropy parameter set could
be guessed from its digest (the digest is never logged or sent anywhere). Phase 4
decisions carry no arguments yet, so today the parameters are empty.

## Expiration

Requests default to `JARVIS_PERMISSION_DEFAULT_EXPIRY_SECONDS` (300 s) from
creation, applied to both waiting for approval and using an approval. Expiry is
applied lazily on every access, plus `expire_due()`. Expired requests cannot be
approved or used.

## Audit

`SecurityEventType`: `PERMISSION_REQUESTED, PERMISSION_APPROVED,
PERMISSION_DENIED, PERMISSION_EXPIRED, PERMISSION_CANCELLED,
AUTHORIZATION_ALLOWED, AUTHORIZATION_DENIED`. A `SecurityEvent` carries
timestamp, type, request id, tool, action, result, reason code, session id and
actor. Events go to a bounded in-memory `AuditLog` (`PermissionManager.audit`,
1000 events) and to the central logger as `SECURITY_EVENT ...` lines. They never
contain parameters, digests, message bodies, credentials or audio; tool names
from the model are stripped to one printable line of at most 64 characters (no
log injection). `JARVIS_PERMISSION_AUDIT_ENABLED=false` turns both off. There is
no persistent audit store or dashboard (Phase 21).

## Privacy

Everything stays local and in memory. Nothing is sent anywhere, action contents
are not stored (only the digest), and the LLM is not shown risk levels, scopes
or any security state.

## Personal memory and this boundary (Phase 6)

Stored memory is untrusted data appended to the prompt inside a delimited block
(`docs/personal-memory.md`). It cannot approve a request, select or run a tool, or
change policy, and memory writes only happen from code on the user's own words, never
from model output. Ordinary memory reads/writes are internal operations and are not
permission-gated.

## Personal RAG and this boundary (Phase 7)

Retrieved document text is untrusted data in a delimited block (`docs/personal-rag.md`).
It is never given to the agent's decision call, the document-answer path has no tools, and
it cannot ingest/delete documents, write memory, approve requests or alter policy.

## Knowledge graph and this boundary (Phase 8)

Graph facts are untrusted data in a delimited block (`docs/knowledge-graph.md`). Model output
never writes to the graph directly (validated typed facts through `GraphService` only), the agent
holds no graph reference, and graph content cannot approve requests, run tools or alter policy.

## Event tools and this boundary (Phase 11)

Eight local event/deadline tools are registered with the manager (ONE_TIME scope, parameters bound): reads,
`event_create` and `event_complete` are LOW (no approval); `event_update`, `event_cancel` and `event_extract` are MEDIUM and need
the user's spoken "yes", read by code from the next message. `event_extract` reads an email, a document or a memory only after
that yes. The model supplies words only; ids, statuses, confidences, sources, URLs, tokens, paths, commands and SQL make the
action invalid. Source text is untrusted data: pattern-matched, sanitized, stored as a short evidence sentence, never shown to
the brain, and replies built from it are replaced by a placeholder in the history. There is no calendar, notification or network
code in the events layer. See `docs/event-and-deadline-intelligence.md`.

## Gmail tools and this boundary (Phase 10)

Five read-only Gmail tools (`gmail_search`, `gmail_get_message`, `gmail_get_thread`, `gmail_summarize`,
`gmail_classify`) are registered LOW risk, no approval, ONE_TIME scope, bound to the exact parameters; no Gmail
request happens before authorization. The OAuth scope is `gmail.readonly`, so even a bug could not send or modify
mail. Send/delete/modify tools do not exist and are denied as unknown. The model supplies only a validated search
query and flags: ids, URLs, tokens, methods, paths and commands make the action invalid. Email content is untrusted:
it is only given, sanitized and delimited, to a tool-less summarizer, never to the AgentBrain, and Gmail replies
are replaced by a placeholder in the conversation history. See `docs/gmail-intelligence.md`.

## Task and reminder tools and this boundary (Phase 9)

The first concrete tools are local: `create_task`, `create_reminder`, `list_tasks`, `list_reminders`,
`complete_task` (LOW risk, no approval: they add to, read or reversibly change the user's own local data)
and `cancel_task`, `cancel_reminder` (MEDIUM, approval required: cancelling cannot be undone). All are
registered with the manager, ONE_TIME scope only, bound to the exact resolved parameters. The model proposes
a validated action with words only (never an id or SQL); code resolves the target and asks when it is
ambiguous. Approval of a cancellation is the user's spoken "yes", read by code from the next message and
passed to `PermissionManager.approve(actor="user")`; the model never sees or produces it. Every other tool
name is still denied as unknown. See `docs/tasks-and-reminders.md`.

## Configuration

| Setting | Default |
|---------|---------|
| `JARVIS_PERMISSION_DEFAULT_EXPIRY_SECONDS` | `300` |
| `JARVIS_PERMISSION_AUDIT_ENABLED` | `true` |

## Anti-bypass rules (each covered by tests)

- AgentBrain and LLM output cannot execute or approve anything.
- A tool cannot run without an authorization for exactly that call, and not when
  the manager is missing or broken.
- An unknown tool never becomes authorized because the LLM selected it or claims
  it is safe.
- A request cannot be altered after approval; forged "approved" objects are
  ignored.
- Expired, consumed, cancelled, denied and other-session approvals are unusable.

## Limitations

- In-process guard only; no OS-level isolation, and `run()` can still be called
  directly by code that skips `execute`.
- In-memory: requests, approvals, PERSISTENT permissions and the audit trail are
  lost on exit or runtime restart.
- No approval UI or dashboard flow. The only approval path is the spoken yes/no for cancelling a task or
  reminder (Phase 9); every other action request ends DENIED (unknown tool) or PENDING and expires.
- The digest is a substitution check, not cryptographic authentication.
- The manager is thread-safe but single-process.

## Future integration model

A later phase that adds a tool: (1) declares risk/scopes/`requires_permission`,
(2) registers `tool.descriptor().security_info()` with the manager, (3) an
orchestrator turns a plan into `request_permissions(...)`, obtains a person's
approval through a UI, then (4) calls `tool.execute(manager, request_id, session_id=..., **params)`.
