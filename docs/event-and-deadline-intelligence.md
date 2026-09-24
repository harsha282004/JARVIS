# Event & Deadline Intelligence (Phase 11)

JARVIS keeps its own structured record of the dates that matter to you: interviews, meetings, exams,
assignment and application deadlines, appointments. It learns them from what you say, and, when you ask, from
an email, an indexed document or something you told it earlier. It can answer "what's coming up", "when is my
next interview", "what's overdue" and "how many days until my exam", and it tells you where each date came from.

This is JARVIS's **internal** intelligence layer. It does not itself connect to any calendar: Google Calendar is a separate
integration (`docs/google-calendar-integration.md`, Phase 12) that maps events JARVIS creates or changes into this layer
(`source_type=GOOGLE_CALENDAR`). It does **not** notify or remind you on its own (Phase 14), and never reschedules anything.

## Architecture

```
VoiceEngine -> ConversationEngine -> AgentBrain -> validated EventAction (words only; data)
                     |
                     v
             TaskActionExecutor  (the same executor as tasks and Gmail)
               1. tool.resolve()   parse the time (Phase 9 TimeParser), identify the event/task by words, ask if unclear
               2. PermissionManager.request_permission(), bound to the exact parameters
               3. Tool.execute()   -> EventService -> EventRepository -> PostgreSQL (`events`)
                                    -> (extract only) Gmail / RAG / memory READ, then extraction, then storage
                                    -> (optional) EventGraphLinker -> Knowledge Graph
```

| Module (`agent/events/`) | Role |
|---|---|
| `models.py` | `Event`, `EventSource`, enums, transition table, errors, dedupe key |
| `repository.py` | `EventRepository`: persistence and conditional updates only |
| `service.py` | `EventService`: create/get/update/cancel/complete/confirm, scopes, search, conflicts, task link |
| `dates.py` | `resolve_when` (on the Phase 9 `TimeParser`), `event_times` |
| `temporal.py` | scopes ("this week"), countdowns, `days_until`, status timing |
| `conflicts.py` | informational overlap detection |
| `extraction.py` | rule-based extraction from untrusted text |
| `sources.py` | Gmail / document / memory -> stored events with provenance |
| `graph.py` | controlled Knowledge Graph links |
| `intents.py`, `tools.py` | the validated action models and the eight tools |
| `backend/models/events.py`, migration `0005_events` | the `events` table |

It reuses the existing PostgreSQL/SQLAlchemy/Alembic setup, `Settings`, logging, the Phase 9 time parser and
task service, the Phase 10 Gmail service, RAG, memory, the graph, the `Tool` abstraction, the `PermissionManager`
and the `ConversationEngine` executor path. There is no new dependency, database, scheduler or permission system.

## Event and deadline model

An event and a deadline are the same row, told apart by which timestamp is set: an **event** happens at a
`start_at` (optionally an `end_at`); a **deadline** must be done by a `due_at` and has no start. Fields:
`event_id`, `title`, `description` (a short evidence sentence or note, never a whole email), `event_type`,
`status`, `priority` (the Phase 9 scale, or none), `start_at`, `end_at`, `due_at`, `timezone`, `all_day`, `source`
(type, id, reference), `confidence`, `task_id`, timestamps, `metadata`.

- **Types:** `DEADLINE`, `MEETING`, `INTERVIEW`, `EXAM`, `ASSIGNMENT`, `APPLICATION`, `APPOINTMENT`, `EVENT`,
  `REMINDER`, `OTHER`. Deadline-like types (`DEADLINE`, `ASSIGNMENT`, `APPLICATION`) are stored with `due_at`,
  the others with `start_at`.
- **Statuses:** `UPCOMING`, `ACTIVE` (going on now), `COMPLETED`, `CANCELLED`, `MISSED` (its time passed without
  being marked completed; for a deadline this is "overdue"), `UNKNOWN` (unconfirmed, see Confidence).
- **Transitions:** UPCOMING -> ACTIVE / COMPLETED / CANCELLED / MISSED; ACTIVE -> COMPLETED / CANCELLED / MISSED;
  MISSED -> COMPLETED / CANCELLED / UPCOMING (rescheduled to the future); UNKNOWN -> UPCOMING / COMPLETED / CANCELLED;
  COMPLETED and CANCELLED are final. ACTIVE and MISSED are computed from the clock before every query, so answers
  are right without any background job.
- **Date-only precision:** a date with no time is `all_day`. A deadline then means the **end** of that local day
  (23:59); a timed event is an all-day event covering that whole local day. "Tomorrow afternoon" is a date with the
  part of the day noted; the time itself is not invented.
- **Times:** stored as UTC, understood and shown in `JARVIS_TIMEZONE` (which is also stored on the event).
- **Priority:** left empty unless you say one. Never guessed.

## Sources and provenance

Every event carries `source_type`, `source_id` and a human-readable `source_reference`:

| Source | Set when | `source_id` | `source_reference` example |
|---|---|---|---|
| `USER_EXPLICIT` | you told JARVIS ("my exam is December 12") | none | you told me |
| `GMAIL` | you asked it to extract from an email | message id (thread id in metadata) | email from Acme Recruiter, dated January 2, 2031 |
| `RAG_DOCUMENT` | you asked it to extract from a document | document id (chunk and page in metadata) | project_guidelines.pdf, page 4 |
| `MEMORY` | you asked it to use an explicit memory | memory id | a memory you told me |
| `TASK` | an event was made from a task's due date | task id | your task |
| `UNKNOWN` | no provenance available | none | (JARVIS says "I don't know where this came from") |

"Where did you get this deadline?" is answered from these fields. Provenance is never invented. `CONVERSATION` exists
in the enum for completeness; explicit statements use `USER_EXPLICIT`.

## Confidence

`LOW`, `MEDIUM`, `HIGH` express **extraction certainty, not fact**. The extractor scores one point for the event cue
(always present), plus one each for an absolute calendar date ("October 5"), a stated time ("at 11 AM"), and the
statement coming from the user themselves: 3 or more is HIGH, 2 MEDIUM, 1 LOW. An email saying "Interview on
January 15, 2031 at 11 AM" is HIGH; "Final deadline: March 20, 2030" is MEDIUM; "Submit before Friday" from an
email is LOW. A LOW-confidence result from Gmail, a document or memory is stored as **UNKNOWN (unconfirmed)**: it is
never listed as a firm deadline (lists only count it: "I also have 1 unconfirmed item"), never linked into the graph,
and never becomes anything else until you confirm it (`event_update` with confirm). Nothing in this phase creates
reminders or notifications, so a low-confidence date cannot silently become one.

## Date and time handling

Reuses the Phase 9 `TimeParser` (a maintained library plus a small deterministic layer). `resolve_when` adds
leading bounds, part-of-day words and vagueness detection. Supported: today, tomorrow, weekdays, next/this
weekday, `October 5`, `October 5 at 10 AM`, `2030-10-05`, `by Friday`, `before September 30`, `in two days`,
`tomorrow afternoon`, `tomorrow afternoon at 3` (3 PM). Results are aware datetimes in your timezone.

| Kind | Meaning |
|---|---|
| EXACT | date and time known |
| DATE_ONLY | a day is known, the time is not |
| AMBIGUOUS | JARVIS asks: "next week", "this weekend", "sometime", "October" (which day?), "the 15th" (which month?), "at 8" (8 AM or 8 PM?) |
| UNRESOLVED | not understood |

Nothing is guessed. Ambiguous or unresolved dates are asked about (from you) or returned as an "I found a date I
couldn't pin down" question (from a source) and **nothing is stored for them**. Dates already in the past are refused
when you create an event, and skipped (and counted) when extracted, so old emails do not flood your overdue list.
"Friday" resolves to the coming Friday from the reference moment (for an email: the day it was sent). A wall-clock
time follows daylight saving.

## Extraction

`extract_events` is deterministic (no LLM). A sentence becomes a candidate only if it has an **event cue**
(interview, exam, assignment, application, appointment, meeting, deadline, due, submit, submission, closes,
registration, webinar, ...) **and** a date phrase. Past-tense sentences ("was on ...") are ignored. It keeps a short
evidence sentence as the description and derives a title (the email subject for Gmail, else the sentence without its
date). At most 10 candidates per source, 300 sentences, 20,000 characters.

- **Gmail:** one email you identify (search words, or "the latest"; several matches make JARVIS ask). Untrusted text.
- **Documents:** one indexed document you name, at most 60 chunks; document id, filename, page and chunk are kept.
- **Memory:** only memories you stated explicitly; inferred memories are ignored. A remembered date never becomes an
  event unless you ask.

Nothing is extracted automatically on ingest, on receipt or on a schedule. The same source processed twice stores
nothing new (see Duplicates).

## Duplicates

`(source_type, source_id, dedupe_key)` is unique in the database, where the key is a hash of the normalized title, type
and time. The same email or document processed twice stores one event. Different sources, or different times, stay
separate: there is no semantic merging. If you add an event you cancelled earlier, it is revived; extraction never
resurrects a cancelled event.

## Tasks

A reference, not a copy. `events.task_id` points at an existing Phase 9 task (foreign key, set to NULL if the task is
deleted). Saying "add the deadline from my internship task" finds the task by words (ambiguity is asked about), and
takes its title and due date; **no task is ever created** by an event, and `event_for_task` is idempotent. A task's
due date is filled from a linked event only if the task has none; if both have dates and they differ JARVIS says so and
leaves both alone (`SyncOutcome.MISMATCH`).

## Knowledge Graph

The Phase 8 schema gained one entity type, `EVENT`, and one relationship, `HAS_DEADLINE`, plus two allowed
combinations; nothing else changed and no migration was needed (the columns are strings). Only these links exist:

- `PROJECT | GOAL --HAS_DEADLINE--> EVENT` to an **existing** entity you name ("project": "JARVIS");
- `EVENT --DOCUMENTED_IN--> DOCUMENT` for an event extracted from a document (direction follows the Phase 8 schema:
  the thing documented points at the document);
- `PERSON --RELATED_TO--> EVENT` to an **existing** person you name.

Unknown projects and people are reported ("no such project in the knowledge graph") and never created. At most three
links per event; the only entities created are the EVENT (named "title (date)") and its DOCUMENT. Each relationship keeps
provenance and confidence from the event's own source; unconfirmed events get no links; cancelling an event
deactivates its relationships. Tasks are not graph entities. Graph failures never affect events.

## Conflicts (informational)

Only events with a start that are UPCOMING or ACTIVE can conflict (deadlines, cancelled and unconfirmed ones cannot).
Timed events **overlap** when their ranges intersect (10:00-11:00 and 10:30-11:30 do; back-to-back events do not). An event
with no end is an instant: it conflicts only if it falls inside another's range or at the same instant. An all-day event
and anything else on the same **local** day are reported as "on the same day" (a softer kind). JARVIS adds "Heads up: A and
B overlap. I haven't changed anything." when you create or change an event, and on request ("do I have conflicts
tomorrow?"). It never reschedules or cancels anything and never touches an external calendar.

## Temporal reasoning

All on your local calendar. Scopes: `upcoming` (today for `JARVIS_EVENT_DEFAULT_LOOKAHEAD_DAYS`), `today`, `tomorrow`,
`this_week` (today until the end of Sunday), `next_week` (next Monday to Sunday), `next_7_days`, `overdue` (open deadlines
past due), `all` (everything open, plus unconfirmed items, labelled). Countdowns: "today", "in about 4 hours", "tomorrow",
"in 5 days", "3 days ago". An event without a usable time has no countdown.

## AgentBrain actions

The model adds `"action": {"name": ..., "arguments": {...}}` with words only. Times are copied as you said them.

| Tool | Arguments | Risk | Approval |
|---|---|---|---|
| `event_create` | `title`, `when`, `type`, `duration_minutes`, `priority`, `description`, `task_query`, `project`, `person` | LOW | automatic |
| `event_list` | `scope`, `type`, `next`, `conflicts` | LOW | automatic |
| `event_search` | `query`, `type`, `include_past` | LOW | automatic |
| `event_get` | `query` | LOW | automatic |
| `event_complete` | `query` | LOW | automatic |
| `event_update` | `query`, `when`, `title`, `type`, `duration_minutes`, `priority`, `description`, `task_query`, `confirm` | MEDIUM | your spoken "yes" |
| `event_cancel` | `query` | MEDIUM | your spoken "yes" |
| `event_extract` | `source` (gmail/document/memory), `query`, `latest` | MEDIUM | your spoken "yes" |

`event_extract` is registered only when at least one of Gmail, documents or memory is available. The model can **never**
supply an event, task, message, thread, document, memory or database id, a status, a confidence, a source, a URL, a token,
a path, a command or SQL: such a key makes the whole action invalid (the brain retries once, then uses its safe fallback).
Events, tasks, emails and documents are identified by code from words; several matches make JARVIS read the candidates and
ask, none is reported honestly.

## Permissions

Registered with the Phase 5 `PermissionManager` (ONE_TIME scope, parameters bound); unregistered tools (`calendar_create`,
`event_delete`, `notify_user`, ...) are denied. Reads, creating something you just said, and completing are LOW: they act
on your own local data from your own words and are reversible or additive. Updating, cancelling and extracting are MEDIUM:
they change or end persistent data, or store data derived from untrusted text, so JARVIS reads back what it will do
("Do you want me to look in your email matching 'from:acme' for dates and deadlines and save what I find?") and only your
next message, read by code, approves it. No Gmail, document or memory read happens before that "yes". Editing changes only
JARVIS's own record: nothing is rescheduled elsewhere.

## Security and privacy

- Email, document and memory text is **untrusted data**. It is only pattern-matched; nothing in it is executed, followed
  or turned into an instruction. It is never shown to the AgentBrain.
- Stored titles, descriptions and references are sanitized (angle brackets and control characters removed). Hostile text
  (SQL, shell, "ignore previous instructions") is stored as plain text and does nothing.
- Replies that contain stored event text are replaced by a placeholder in the conversation history, so text that originated
  in an email or document cannot steer later turns (including replies produced through a confirmation).
- Only a short evidence sentence is stored, never a whole email body. Logs contain ids, types, statuses and counts, never
  titles, descriptions, references or source text.
- No network code, no calendar library, no notification or scheduler code exists in `agent/events` (a test enforces it).

## Database

Migration `0005_events` (after `0004_tasks_reminders`): table `events` with timezone-aware columns, provenance columns,
`confidence`, foreign key `task_id -> tasks.id ON DELETE SET NULL`, unique `(source_type, source_id, dedupe_key)` (with
`source_id` never NULL so it also holds on PostgreSQL), and indexes `(status, start_at)`, `(status, due_at)`, `(task_id)`.
Additive only; reversible.

```powershell
alembic -c database/alembic.ini upgrade head
alembic -c database/alembic.ini downgrade 0004_tasks_reminders   # removes only the events table
```

## Configuration

| Setting | Default | Meaning |
|---|---|---|
| `JARVIS_EVENTS_ENABLED` | `true` | registers the event tools |
| `JARVIS_EVENT_DEFAULT_LOOKAHEAD_DAYS` | `7` | how far "what's coming up" looks (1-365) |
| `JARVIS_EVENT_MAX_RESULTS` | `20` | most events listed or searched at once (1-100) |

The timezone is the existing `JARVIS_TIMEZONE`. Event actions need the agent (`JARVIS_AGENT_ENABLED`). If the table does
not exist yet JARVIS still runs; event requests get "my event database isn't available".

## Testing

`tests/test_event_*.py` (models, service, dates, extraction, actions, permissions, sources, graph, security, static checks),
migration and config tests in `tests/test_memory_migration.py` and `tests/test_gmail_runtime.py`. Repository and service tests
run on isolated in-memory SQLite (foreign keys on), and on a disposable PostgreSQL when `JARVIS_TEST_DATABASE_URL` is set;
`tests/integration` has a real-PostgreSQL create/update/cancel/complete/search/restart/duplicate test that skips without it.
A scripted LLM, an in-memory Gmail client and small document/memory doubles stand in for Ollama, Gmail, RAG and memory.
Passing on SQLite does not prove behaviour on your PostgreSQL.

## Limitations

- English only. Extraction is rule-based: it needs an event cue and a date phrase in the same sentence and will miss
  paraphrases (and can mistake a sentence for an event); that is why uncertain results wait for your confirmation.
- Vague dates ("next week") are never stored; you must give an exact date.
- Numeric dates such as `05/10/2030` are not extracted (day/month order is ambiguous).
- One time per event: ranges ("October 5-7") and recurring events are not modelled (use Phase 9 reminders for repeating things).
- Extraction happens only when you ask, one email/document/memory query at a time; there is no automatic scan.
- MISSED is "the time passed and you did not mark it done"; a meeting you attended but did not complete shows as missed.
- The graph is optional and best-effort; unconfirmed events never reach it.
- No calendar sync, no reminders or notifications for events, no automatic rescheduling (later phases).
