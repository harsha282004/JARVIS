# Data provenance

Every derived fact carries a `Provenance`:

| Field | Meaning |
|---|---|
| `source_type` | email, calendar, task, reminder, memory, document, event_record, user, derived |
| `source_id` | the id in that system (Gmail message id, `calendar_id/event_id`, task id, ...) |
| `label` | short human label, e.g. `email 'JARVIS project review'` |
| `source_timestamp` | when the source item was written (an email's received time, a task's creation time) |
| `extracted_at` | when JARVIS read it |
| `confidence` | LOW / MEDIUM / HIGH (extraction certainty, not factual certainty) |
| `entity_id` | the entity it supports |
| `reference` | a short sanitized evidence sentence (extractions) |

Where it appears: entities and relationships (`ContextGraph`), deadlines (`Deadline.source`, original text, normalized date + timezone, confidence, associated entity, status),
task proposals, findings (`Finding.evidence()`), plan blocks (`PlanBlock.reason`), notifications (`refs`), the dashboard's recommendation "Evidence" list.

"Where did you get that?" reads the recorded provenance of the last answer: *"That came from your task list (task '...', dated Sep 24) and your calendar (calendar event '...')."*
If nothing was recorded it says so; it never fabricates a source. Memories keep their own source/timestamp/confidence fields (Phase 6), verified to survive a restart.

Privacy of provenance: labels and references are sanitized (no angle brackets, control characters, or email addresses in spoken output); message bodies are never stored
(the timeline stores subjects/titles only; the audit log stores redacted, bounded fields).
