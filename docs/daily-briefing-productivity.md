# Daily Briefing & Productivity Intelligence (Phase 15)

JARVIS can now answer "Good morning", "What do I have today?", "What should I focus on?", "What's my next meeting?",
"What did I miss yesterday?" and "What should I prepare for tomorrow?" by **combining what its existing services already know**:
tasks and reminders (Phase 9), events and deadlines (Phase 11), Google Calendar (Phase 12), important email (Phase 10) and, if a
provider is set up, messages (Phase 13).

It summarizes and prioritizes **existing** information. It never invents a task, event, deadline, email or priority, never
changes anything, never sends anything, and has no productivity score. The user stays the decision-maker.

## Architecture

```
tasks  reminders  events/deadlines  Google Calendar  Gmail  messaging        <- the EXISTING services, read-only
   \      |            |                  |            |        /
    ProductivityCollector  (isolates every source, bounded, no copies)        collection + normalization
                |  ProductivityContext  (items = title + time + level + reasons + source reference)
    PriorityAnalyzer (deterministic)   windows.py (local time)                temporal + priority analysis
                |
    Builder (conflicts, preparation, risks, focus, wording)                   conflict / risk analysis + briefing generation
                |  DailyBriefing.spoken  (bounded, voice-friendly)
    BriefingService  --(optional, verified)--> local LLM phrasing            voice/text response
                |
    briefing_generate / briefing_explain tools -> PermissionManager -> ConversationEngine
```

| Module (`agent/briefing/`) | Role |
|---|---|
| `models.py` | `ProductivityContext`, `DailyBriefing`, `BriefingItem`, `SourceRef`, windows/views/detail enums |
| `windows.py` | `TODAY`, `TOMORROW`, `THIS_WEEK`, `NEXT_7_DAYS` (+ look-back `YESTERDAY`, `LAST_24_HOURS`) on the existing timezone helpers |
| `collector.py` | reads the existing services, isolates failures, normalizes into items |
| `priority.py` | the transparent priority analysis |
| `builder.py` | conflicts, preparation, risks, focus and the spoken wording |
| `service.py` | `BriefingService`: the entry point, the optional grounded LLM step, and traceability (`explain`) |
| `intents.py`, `tools.py` | the two read-only actions the model may propose and their tools |

It creates **no second task/reminder/calendar/email system, no scheduler, no notifier and no database table** (no migration).

## ProductivityContext and DailyBriefing

`ProductivityContext`: current time, timezone, window; `tasks_overdue`, `tasks_due` (inside the window), `tasks_upcoming`, `tasks_high`
(open tasks marked high/critical, dated or not); `reminders`; `deadlines_overdue/due/upcoming`; `events` and `events_upcoming` (calendar and
Phase 11); `emails_action`, `emails_important`; `messages`; `conflicts`; `preparation`; `missed`; `completed_today` (a plain count);
and the state of every source (`ok`, `not_configured`, `disabled`, `unsupported`, `unavailable`). It holds **references and normalized
summaries** (a sanitized title up to 100 characters, a time, the analysis result and a `SourceRef`), never bodies or copies of records.

`DailyBriefing`: date, greeting (overview only, by local time of day), `sections` (schedule, tasks, deadlines, reminders, emails, messages,
conflicts, preparation, priorities, missed), `focus`, `risks`, `notes` (degraded-state sentences), `spoken`, and `items` (the items that
were actually presented, for traceability). **Only sections with real content exist**; there are no fake "nothing found" sections.

## Source aggregation

| Source | What is used | Bound |
|---|---|---|
| Tasks (Phase 9) | open tasks: overdue, due in the window, upcoming, and high/critical ones; completed-today count | scan of 200 |
| Reminders (Phase 9) | scheduled reminders still to come in the window. The briefing never triggers, claims, expires or re-delivers a reminder | 50 |
| Events (Phase 11) | events and deadlines by window, overdue deadlines, upcoming ones. Events linked to a task, unconfirmed guesses, and mirrors of Google events (when the calendar is read) are not double-counted | 100 |
| Google Calendar (Phase 12) | events in the window, upcoming events, overlaps; all-day entries kept but not counted as clashes | 50, then "at least N" |
| Gmail (Phase 10) | **only** unread inbox mail from the last 3 days that the deterministic classifier calls action-required or important. Ordinary mail is never summarized, no body is read aloud, no model is used | one search of `2 x JARVIS_BRIEFING_EMAIL_LIMIT` |
| Messaging (Phase 13) | only when a provider is set up: recent messages the deterministic classifier says ask something (max 3, sender name only, never the text) | one bounded read |

Not-set-up sources are left out silently. A source that is set up but failing is reported honestly and never breaks the rest. Reading
through Phase 11's `list_scope` refreshes time-derived event statuses (its existing read behaviour); nothing the user entered is changed.

## Priority analysis (deterministic, transparent)

Each item gets an internal integer from three explainable parts; no model or wording takes part:

- **What the source says**: an explicit priority (task/event: low 0, medium 10, high 20, critical 30); with none, a default by kind
  (interview, exam, deadline, assignment, application 20; meeting, appointment, calendar event, reminder 10; action email 12; important email 10; message 8).
- **How late or close**: overdue +25, within three hours +20, today +15, tomorrow +10, within three days +5.
- **Level**: 45+ `CRITICAL`, 30+ `HIGH`, 10+ `NORMAL`, else `LOW`.

So an overdue CRITICAL task is surfaced before a LOW task due next week, and a HIGH task due tomorrow reaches HIGH. The number is used only to
order items and pick a focus and is **never shown**; only the factual reasons are ("marked high priority", "due tomorrow", "overdue"). The
underlying task or event priority is never changed. Ties are broken by time, then id, so ordering is deterministic.

## Views, windows and detail

Views: overview (the morning briefing), schedule (incl. "my afternoon"), tasks, deadlines, priorities, focus, next, missed, prepare.
Windows: today, tomorrow, this week, next 7 days; look-back yesterday and last 24 hours (missed only). Detail: quick (one sentence of
counts), normal, detailed (up to `JARVIS_BRIEFING_MAX_ITEMS` per section). Combinations that make no sense ("my schedule yesterday") are asked about, not guessed.

## Deadlines, calendar, conflicts, preparation

- **Deadlines**: deterministic date maths (overdue, today, tomorrow, upcoming) over Phase 11 records; no deadline data is duplicated.
- **Calendar**: today's events with times, all-day events, the next event, and the events after it. If Calendar is unavailable JARVIS says so and still reports the rest.
- **Conflicts**: overlapping timed calendar events, and several high-priority deadlines within 24 hours of each other. They are only **reported**, with
  "I haven't changed anything"; nothing is rescheduled, cancelled or resolved.
- **Preparation**: for an interview, exam, appointment, meeting or a calendar event that says interview/exam/presentation/demo/review within
  48 hours, JARVIS mentions **existing** pending tasks that are due before it and are either linked to it (Phase 11 link), share a keyword with it, or
  start with a preparation verb ("Prepare resume"). It never invents a requirement and never creates a task.

## Focus, risks, "what's next", "what did I miss"

- **Focus** is a hedged suggestion from concrete data only: "Based on your deadlines and priorities, one reasonable focus is 'X', because it is marked
  high priority and is due tomorrow." With nothing concrete (a low-priority or distant item, or only meetings) there is no suggestion. It never says
  what is objectively best.
- **Risks** are neutral counts: overdue items, deadlines within 24 hours, unresolved high-priority tasks, conflicts, important unread email, and sources that could not be checked. No "crisis" language.
- **Next** names the next timed event and the tasks due today that follow it. **Missed** uses only what the services report: tasks that became overdue in the
  window, reminders that expired undelivered, deadlines that passed unmarked, action/important unread email from then, and how many calendar events there were.
  (Phase 11 exposes only overdue *deadlines*, not other missed events.)

## Voice optimization

Counts and grouping instead of lists ("You have four tasks due today: two high priority and two normal."; "You have 12 tasks to look at, including three
marked high priority."), spelled-out small numbers, natural times ("10 AM"), no ids, e-mail addresses are removed from titles ("an address"), a sender with only an
address is "a sender", links and long descriptions are never read, and every spoken answer has a hard cap (quick 320, normal 950, detailed 2400 characters, cut at a
sentence). Example: *"Good morning. You have three events today: 'Hackathon' all day, 'Project review' at 10 AM and 'Design sync' at 10:30 AM. You have four tasks due today: two high priority and two normal. ..."*

## The LLM's role

The structured data is the source of truth and the deterministic text is the default. With `JARVIS_BRIEFING_USE_LLM=true` the local model may
**rephrase** the finished text, in a tool-less call, with only the sanitized facts inside a delimited block that says titles are untrusted. The rewrite is accepted only
if it adds no number, quoted title, link, address or code, drops no quoted title, claims no action was taken, and is not much longer; otherwise (or on any error)
the deterministic text is used. The model can never invent, re-rank or alter an item, change a priority, or touch a system.

## Source traceability and explainability

Every item keeps a `SourceRef` (source, the source record's id, a human label such as "your Google Calendar"). After a briefing, "Where did you get that?" and "Why is this a
priority?" / "Why are you mentioning this?" call `briefing_explain`, which answers from the recorded source and reasons of the items that briefing actually presented:
*"'Submit report' comes from your task list: a task. It was mentioned because it is marked high priority and is due today."* Nothing else is exposed: no score, no reasoning.
The last briefing is kept in memory (references only); nothing is written to a database.

## Relationship to Phase 14 (proactive intelligence)

Separate systems. Phase 14 decides whether to notify (quiet hours, cooldown, deduplication, channels) and owns the notification history. Phase 15 produces briefing
**content** on request and imports only Phase 14's text sanitizer. It creates no notification path. `BriefingService.brief(...)` is the interface a later scheduled
briefing could call through Phase 14's policy; **no schedule is created here**, and there is no settings UI.

## Actions, permissions and configuration

Two tools, both LOW risk, read-only, one-time, no approval, bound to their exact parameters, and their replies are kept out of the conversation history
(they can quote email subjects and calendar titles): `briefing_generate` (view, window, detail, day part) and `briefing_explain`. The model cannot supply item ids, keys, priorities,
scores, source ids or anything to create, send, complete, modify or delete. Unknown tool names are denied.

| Setting | Default | Meaning |
|---|---|---|
| `JARVIS_BRIEFING_ENABLED` | `true` | registers the briefing tools (sources that are not set up are simply left out) |
| `JARVIS_BRIEFING_MAX_ITEMS` | `10` (1-50) | items named per section in a detailed briefing |
| `JARVIS_BRIEFING_LOOKAHEAD_DAYS` | `7` (1-30) | how far "upcoming" reaches beyond the window |
| `JARVIS_BRIEFING_EMAIL_LIMIT` | `5` (0-20) | most action/important emails considered; `0` leaves email out |
| `JARVIS_BRIEFING_USE_LLM` | `false` | allow the verified natural rephrasing |

## Failure handling

Each source is isolated: one unavailable source gives a note ("I couldn't check your Google Calendar just now, so this doesn't include your calendar.") and the rest of the
briefing is still given. If every configured source fails you get one honest sentence and a suggestion to retry. An unexpected internal error gives "I couldn't put a briefing
together just now." Nothing is ever fabricated for a missing source, and errors log only the exception type.

## Security and privacy

Read-only by construction: the package imports no mutating call (a static test forbids create/update/complete/cancel/delete/send/mark/claim/expire calls, network libraries, SQL,
threads and schedulers), and a test replaces every mutating method of every service with a failing stub and runs every view over hostile data. Email subjects, calendar titles, task
and reminder text and sender names are untrusted: sanitized (no angle brackets, control characters or addresses), length-bounded and only ever quoted; they never reach the
AgentBrain and are kept out of history. A briefing can never invoke a tool or grant a permission. Logs carry counts and exception types only, never titles, subjects, senders or bodies.

## Testing

`tests/test_briefing_context_priority.py` (windows, timezone, context, tasks, deadlines, reminders, priority, conflicts, preparation), `tests/test_briefing_generation.py`
(briefing, views, detail, voice, empty and degraded states, Calendar, Gmail, messaging, missed, next, focus, traceability, LLM grounding), `tests/test_briefing_security_actions.py`
(actions, permissions, injection, no external action, privacy, static guards), `tests/test_briefing_runtime.py` (settings, bootstrap), and
`tests/integration/test_briefing_real.py` (real SQLite file and, when configured, PostgreSQL, Google and Telegram; read-only; skipped otherwise).

## Limitations

- English wording and rule-based classification of email/messages; preparation matching is by verbs and keywords, so it can miss or over-match; focus needs concrete data.
- Calendar reads are bounded (50 per request, so very busy days say "at least N"); Gmail considers only recent unread mail; messaging reads only what the provider exposes.
- Missed events other than deadlines are not visible through Phase 11; "while I was away" is the last 24 hours (JARVIS does not track being away).
- No scheduled briefing and no notification: asking is the only trigger. No dashboard, no history of briefings.
- A briefing is a snapshot at the time it is asked; "where did you get that?" refers to the most recent briefing only.
