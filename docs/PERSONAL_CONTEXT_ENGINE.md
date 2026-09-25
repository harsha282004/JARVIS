# Personal Context Engine

`agent/intelligence/context_engine.py`. Turns one `Snapshot` (what the user's own sources say right now) into a `ContextGraph`.

## Inputs (read-only)

`SnapshotCollector` reads through the public read methods of TaskService, ReminderService, EventService (saved deadlines), CalendarService,
GmailService (last 7 days, bounded), MemoryService and RagService (indexed documents, bounded excerpts). Each source is isolated:

| Source state | Meaning | What JARVIS says |
|---|---|---|
| `ok` | read successfully | uses it |
| `not_configured` | integration not set up | leaves it out; never claims anything about it |
| `unavailable` | set up but failed (or offline mode) | "I couldn't check your calendar just now"; never treats it as "nothing there" |

## Entities and relationships

Entity kinds: user, project, task, event, deadline, meeting, person, organization, document, email, message, hackathon, internship, job,
course, assignment, project_review, interview, exam. Relationships: `belongs_to` (task -> project), `applies_to` (deadline -> task),
`references` (email -> event/person), `has_deadline` (event -> deadline), `relates_to` (event/document/task -> project or event),
`depends_on` (task -> task), `same_as` (kept when a merge would hide a disagreement). Every relationship stores a **reason** and
**provenance**; a test asserts none exists without both.

Rules that create a relationship (all evidence-based):
* a project name appears as a whole word in a task/event/document title -> `belongs_to`/`relates_to` (HIGH); in task notes or repeatedly in a document (MEDIUM);
* a task title, once verbs and supporting nouns ("slides", "checklist") are removed, is wholly contained in an event's name and shares at least two words or one distinctive word -> `relates_to` (MEDIUM);
* an email sentence "before the review" is tied to the review event found in the same email -> `has_deadline` + `applies_to`;
* an explicit user statement "B depends on A" -> `depends_on` (HIGH).
A single shared generic word ("project") never creates one (tested).

## Resolution (same thing, several sources)

`textnorm.same_thing_score`: tokens are normalized (stop words, weekdays, months, times, "meeting"-type words removed; plural stems); the score
mixes Jaccard and overlap coefficient; a match needs a distinctive shared word or two shared generic words; titles whose distinctive words
are disjoint ("JARVIS review" vs "Capstone review") never match. Merge threshold 0.75. Sources are processed in the order calendar >
saved events > email > document > memory. Two records of the *same* source (a recurring stand-up) are never merged.

* same name, **same day** -> merged into one entity carrying all provenance (the calendar is the source of truth for the time);
* same name, **different day** -> **not merged**; a `DateConflict` is recorded and both entities remain; JARVIS reports both and picks neither;
* same day but times differing by >= 30 minutes -> a "time" conflict.

## Idempotence and duplicate prevention

Entity ids derive from source ids or normalized names, so rebuilding from the same sources yields the same graph (tested). Extraction is
cached per (source, id, text hash). Task proposals exist only when no similar open task exists.

## Limits

Computed on demand and kept in memory for ~20 s; not persisted into the Phase 8 PostgreSQL graph (see `KNOWLEDGE_GRAPH.md`).
