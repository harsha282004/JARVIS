# Tasks and Reminders (Phase 9)

A persistent, local task and reminder engine. JARVIS can create tasks, schedule one-off and repeating
reminders, answer "what's on my list", complete and cancel things, and announce reminders when they are
due, across restarts. Everything is stored in your local PostgreSQL database.

It is only the local engine. It has no Google Calendar, Gmail, WhatsApp or other messaging, no proactive
suggestions or daily briefing, no browser or desktop automation, no remote access and no dashboard.

## Architecture

```
VoiceEngine -> ConversationEngine -> AgentBrain
                     |                   |  structured, validated TaskAction (data only)
                     |                   v
                     +--> TaskActionExecutor
                             1. tool.resolve(): parse times, identify the target from words (asks if unclear)
                             2. PermissionManager.request_permission() bound to the exact parameters
                             3. Tool.execute()  (re-checks the permission)
                                     |
                       TaskService / ReminderService --> TaskRepository --> PostgreSQL
                                                                              ^
Windows runtime --> ReminderScheduler (one thread) --> ReminderService -------+
                          |
                          v
                   NotificationService --> DesktopNotifier (tray balloon)
                                       --> VoiceNotifier   (queue -> VoiceEngine speaks it)
```

| Module (`agent/tasks/`) | Role |
|---|---|
| `models.py` | `Task`, `Reminder`, `Recurrence`, status/priority enums, the transition table, errors |
| `repository.py` | `TaskRepository`: persistence and atomic conditional updates only |
| `service.py` | `TaskService`, `ReminderService`: all rules, transitions, queries, triggering |
| `timeparse.py`, `recurrence.py`, `formatting.py`, `zone.py` | natural-language times, recurrence maths, speakable text, timezone |
| `matching.py` | identifies "my JARVIS documentation task" from words (never from an id) |
| `intents.py` | the validated `TaskAction` the model may propose (models only) |
| `tools.py`, `executor.py` | the tools and the permission-gated executor |
| `notifications.py`, `scheduler.py` | `NotificationService` implementations, `ReminderScheduler` |
| `backend/models/tasks.py` | `tasks` and `reminders` tables |
| `database/migrations/versions/0004_*` | Alembic migration |

It reuses the existing PostgreSQL/SQLAlchemy/Alembic setup, `Settings`, logging, the
`PermissionManager`, the `Tool` abstraction and the tray. There is no second database, no SQLite or JSON
store and no scheduler library.

## Task model

`task_id`, `title` (max 200), `notes` (max 2000), `status`, `priority`, `created_at`, `updated_at`,
`due_at`, `completed_at`, `cancelled_at`, `session_id` (the conversation session that created it),
`source` (`conversation` or `api`), `metadata` (holds the bounded status `history`).

**Statuses** (`TaskStatus`): `PENDING`, `IN_PROGRESS`, `COMPLETED`, `CANCELLED`, `OVERDUE`.
**Priorities** (`TaskPriority`): `LOW`, `MEDIUM` (default, `JARVIS_DEFAULT_TASK_PRIORITY`), `HIGH`, `CRITICAL`.
Nothing in the application uses bare strings for these.

### State transitions

| From | Allowed to |
|---|---|
| `PENDING` | `IN_PROGRESS`, `COMPLETED`, `CANCELLED`, `OVERDUE` |
| `IN_PROGRESS` | `COMPLETED`, `CANCELLED` |
| `OVERDUE` | `PENDING` (its due date was moved to the future), `IN_PROGRESS`, `COMPLETED`, `CANCELLED` |
| `COMPLETED`, `CANCELLED` | nothing, except the explicit `reopen_task()` (back to `PENDING`) |

Anything else raises `InvalidTransition`: `COMPLETED -> IN_PROGRESS` never happens silently, and completing
twice fails the second time. Every change is recorded in `metadata.history`, so completion and reopening
history is preserved. Completing or cancelling a task also cancels its scheduled reminders, in the same
transaction.

`OVERDUE` is a stored status set by `mark_overdue()` (the scheduler runs it every poll) for `PENDING` tasks
past their due time. Queries do not depend on that sweep: "overdue" always means *open and past due*, so
the answer is right even before the sweep runs. `IN_PROGRESS` tasks are left alone.

## Reminder model

`reminder_id`, `task_id` (optional; a reminder may belong to a task but does not need to), `message`
(max 300), `scheduled_at`, `status`, `timezone` (IANA name), `recurrence`, `created_at`, `triggered_at`
(last delivery), `cancelled_at`, `occurrences`, `delivery_attempts`, `claimed_at` (delivery lease),
`session_id`, `source`, `metadata`.

**Statuses** (`ReminderStatus`): `SCHEDULED`, `TRIGGERED`, `CANCELLED`, `EXPIRED`.

A task is work; a reminder is a scheduled notification. They are separate tables and services.
`create_task_with_reminder()` creates both in one transaction.

## Time and timezone

- The database stores **UTC** (`timestamptz`). The application converts to the user's timezone for parsing
  and speaking. Naive datetimes are rejected everywhere.
- `JARVIS_TIMEZONE` is an IANA name (`Asia/Kolkata`). **Default: empty, meaning this computer's timezone**,
  detected once at startup with `tzlocal`, written to the log and used as an explicit zone from then on. If
  detection fails JARVIS uses UTC and logs a warning. Set it explicitly to avoid any doubt. An invalid name
  stops startup with a clear configuration error.
- Recurrences and "today" are computed on the user's wall clock, so "every day at 8 AM" stays at 8 AM
  across daylight-saving changes. A wall-clock time that does not exist on a DST-gap day resolves to the
  instant just after the gap.

### Natural-language times

Parsed by code, never by the model. The model passes the phrase as the user said it. `dateparser` (a
maintained library) handles relative offsets and explicit dates; a small deterministic layer handles the
cases `dateparser` gets wrong (weekday names, "next Monday", bare times such as "at 5 pm").

| Phrase | Result |
|---|---|
| `tomorrow at 9 AM`, `today at 6 PM`, `day after tomorrow at noon`, `tonight at 8` | that wall time |
| `in 30 minutes`, `in 2 hours` | offset from now |
| `next Monday at 8 AM` | the first Monday strictly after today (never today) |
| `Monday at 8 am`, `on Friday at 3pm` | this coming Monday/Friday; if today is that day and the time passed, next week |
| `at 5 pm`, `17:30` | the next such time (today, else tomorrow) |
| `March 10 at 3 pm`, `2030-03-10 15:00` | that date and time |

Nothing is invented. Not understood: "I couldn't understand that time". Ambiguous (`at 8`: AM or PM?) or
incomplete (a date without a time, a recurrence without a time): JARVIS asks. A time in the past is
refused. A task's due date given without a time means the end of that day; a reminder always needs a time.

## Recurring reminders

Structured (`Recurrence`): `daily`, `weekly` (one or more weekdays, `every weekday` included) and `monthly`
(a day of the month; a day past the end of a short month, such as the 31st, runs on the last day). Not
supported: "every 2 weeks", "the second Tuesday of the month", "every N days".

A recurring reminder is **one row**. After it triggers, the same row moves to its next occurrence (strictly
after now, so occurrences missed while JARVIS was off are skipped, not queued). There is never a queue of
future rows. Cancelling it sets `CANCELLED`, which ends all future occurrences.

## Scheduler and duplicate protection

`ReminderScheduler` is one daemon thread. It wakes every `JARVIS_REMINDER_POLL_SECONDS`; the first pass runs
immediately at start-up. It is started by the Windows launcher after the tray and stopped at shutdown,
independently of the voice engine (a voice failure does not stop reminders and a scheduler failure does not
stop JARVIS). It never touches audio.

Each due reminder goes through a delivery lease:

1. `claim_delivery`: one atomic `UPDATE ... WHERE status = 'scheduled' AND scheduled_at <= now AND lease free`.
   Exactly one caller gets it, however many times or from however many threads/processes it is checked.
2. deliver through `NotificationService`.
3. `complete_delivery`: a conditional update that records the trigger (one-shot: `TRIGGERED`; recurring: next
   occurrence) only if this claim still owns the reminder.

If delivery fails the reminder is **not** recorded as delivered, the lease is released and it is retried on
the next polls; after 20 failed attempts (about five minutes at the default interval) it becomes `EXPIRED`
(recurring: it skips to the next occurrence) and the error is logged. A lease that is never completed (a
crash) expires after 120 s and the reminder is retried. Database errors are logged (exception type only) and
the scheduler backs off up to five minutes, then recovers by itself.

All state changes are conditional updates (`WHERE status = <expected>`), so the scheduler and a voice request
racing each other cannot double-complete a task, cancel and trigger the same reminder, or advance a
recurrence twice. `TaskRepository.unit_of_work()` makes multi-step changes atomic and is per thread.

One unavoidable race: cancelling a reminder in the instant after its notification was already handed to the
notifier cannot un-send it.

## Missed reminders

A reminder is *missed* if it is delivered more than two minutes after its scheduled time. This happens after
JARVIS was closed, the computer slept, or the database was down.

`JARVIS_MISSED_REMINDER_POLICY`:

- `notify` (default): deliver it once, late, worded as `Missed reminder from <when>: <message>`. It is never
  presented as if it had fired on time and never claimed as delivered while JARVIS was off.
- `expire`: do not deliver; mark it `EXPIRED` (recurring: skip to the next occurrence).

A recurring reminder that was missed several times is reported once, not once per missed occurrence.

## Notifications

`NotificationService.notify(message, metadata)` returns only if the message was handed to a channel, and
raises `NotificationError` otherwise. Implementations:

- `DesktopNotifier`: a Windows notification balloon from the tray icon (`TrayController.notify`, pystray).
  Local, no cloud. Needs the tray (`JARVIS_TRAY_ENABLED=true`).
- `VoiceNotifier`: puts the text on an `AnnouncementQueue`; the `VoiceEngine` speaks it **on its own thread,
  only while waiting for the wake word**, so the scheduler never touches the audio devices and a running
  conversation is never interrupted. It fails (so the reminder is not recorded as delivered) when the voice
  engine is not running (paused, stopped or crashed).
- `CompositeNotifier`: sends to every enabled channel; a reminder counts as delivered if at least one
  accepted it (`JARVIS_REMINDER_DESKTOP_NOTIFICATIONS`, `JARVIS_REMINDER_VOICE_NOTIFICATIONS`).

For the voice channel "delivered" means *accepted by a running voice engine*; it is spoken as soon as the
engine is between conversations. If JARVIS is killed in that moment it is not spoken. The desktop balloon is
the more reliable channel, which is why both are on by default. There is no notification UI and no
preference system, and no email/WhatsApp/push.

## AgentBrain integration

For an `action_request` that is a task or reminder operation, the model adds
`"action": {"name": "<tool>", "arguments": {...}}`:

| Tool | Arguments (all text; times exactly as the user said them) |
|---|---|
| `create_task` | `title`, `notes`, `due`, `priority`, `remind` (also remind at the due time) |
| `create_reminder` | `message`, `when`, `recurrence` |
| `list_tasks` | `scope`: `today`, `overdue`, `upcoming`, `incomplete` |
| `list_reminders` | `scope`: `today`, `tomorrow`, `upcoming`, `next` |
| `complete_task` | `query` (words describing the task) |
| `cancel_task` | `query` |
| `cancel_reminder` | `query`, `when` (its time of day, optional) |

The brain validates the action into a typed `TaskAction` (`intents.py`). An unknown action name, missing or
over-long arguments, or an argument that is not an object make the whole model output **invalid**: the brain
retries once and then returns its safe fallback, and nothing is executed. Unknown keys such as `task_id` or
`sql` are ignored, and there is no argument that names an id or a table. A valid action for a tool that is not
enabled is dropped (the reply is the Phase 4 "I can't carry out actions like that yet").

The `AgentBrain` still holds only tool *descriptors*. It imports the action models only, never the services,
the repository or the executor, and executes nothing. `ConversationEngine` hands a validated action to
`TaskActionExecutor`; the executor's reply is what JARVIS says, so it can only state what really happened.

Task actions need the agent (`JARVIS_AGENT_ENABLED=true`).

## Identifying tasks (no guessing)

The model never supplies an id. `complete_task`, `cancel_task` and `cancel_reminder` take words, and code
matches them against open titles/messages (whole words, or a prefix of four or more letters; an exact title
match wins over longer ones):

- one match: proceed;
- several: JARVIS reads the candidates and asks which one you mean, and changes nothing;
- none: "I couldn't find ...".

Ids come only from the database and go from `resolve()` to the tool as concrete parameters.

## Permission policy

Registered with the Phase 5 `PermissionManager`; every operation goes `request_permission -> execute`, bound to
the exact parameters, one-time scope, and anything not registered is denied (`email`, `delete_task`, `shell`,
... all denied as unknown tools).

| Tool | Risk | Approval | Why |
|---|---|---|---|
| `create_task`, `create_reminder` | LOW | automatic (policy) | adds to the user's own local data; reversible; the model cannot choose ids or tables |
| `list_tasks`, `list_reminders` | LOW | automatic | read-only, local, bounded |
| `complete_task` | LOW | automatic | reversible (`reopen_task`), and the target must be unique |
| `cancel_task`, `cancel_reminder` | MEDIUM | **required** | cannot be undone; cancelling a repeating reminder ends every future occurrence |

For the two that need approval, JARVIS says what it found ("Do you want me to cancel the reminder: submit my
assignment (tomorrow at 9:00 AM)? Say yes to confirm."). The next message is read **by code** (not the model):
a clear yes calls `PermissionManager.approve(actor="user")` and runs the tool; a clear no denies it; anything
else drops the question and is handled as a new request. The question expires after 60 s and one question is
open per conversation. `delete_task` exists only as a code API and no voice or model path reaches it. Speech
recognition can mishear "yes"; the prompt names exactly what will be cancelled.

## Failure behaviour

- Database down: JARVIS says "I couldn't do that because my task database isn't available, so nothing was
  changed." It never says it created something it did not, and the conversation and the runtime keep going.
- Date not understood: it asks; nothing is created.
- Notification failed: not recorded as delivered; retried (see above).
- Scheduler error: logged, backed off, recovered; JARVIS keeps running.
- Permission missing or a tool error: an honest refusal; nothing is changed.

## Privacy

Local PostgreSQL only; no cloud task service, no external messaging, no analytics. Logs contain ids,
statuses, counts and exception types, never titles, notes or reminder text (tests check this). Reminder text
is shown on the desktop and spoken, which is its purpose. Task text is not written to personal memory or the
knowledge graph: Phase 9 does not turn tasks into memories or graph relationships. Task and reminder contents
do sit in the conversation history (the replies JARVIS speaks) for the length of the session, like any other
answer. Do not put credentials in tasks.

## Database

Tables `tasks` and `reminders` (revision `0004_tasks_reminders`, after `0003_knowledge_graph`). `reminders.task_id`
is a foreign key to `tasks.id` with `ON DELETE CASCADE`. Indexes: `tasks (status, due_at)`, `tasks (due_at)`,
`reminders (status, scheduled_at)`, `reminders (task_id)`; they serve the pending/due/overdue task queries and
the "due reminders" poll. Apply and revert:

```powershell
alembic -c database/alembic.ini upgrade head
alembic -c database/alembic.ini downgrade 0003_knowledge_graph   # removes only the Phase 9 tables
```

Portable column types only, so the same models run on PostgreSQL and on SQLite in tests.

## Configuration

| Setting | Default | Meaning |
|---|---|---|
| `JARVIS_TASKS_ENABLED` | `true` | task tools |
| `JARVIS_REMINDERS_ENABLED` | `true` | reminder tools and the scheduler |
| `JARVIS_TIMEZONE` | empty (system timezone, detected once) | IANA zone for parsing/speaking times |
| `JARVIS_REMINDER_POLL_SECONDS` | `15` | scheduler interval (1-3600) |
| `JARVIS_MISSED_REMINDER_POLICY` | `notify` | `notify` or `expire` |
| `JARVIS_DEFAULT_TASK_PRIORITY` | `medium` | `low`, `medium`, `high`, `critical` |
| `JARVIS_REMINDER_DESKTOP_NOTIFICATIONS` | `true` | tray balloon |
| `JARVIS_REMINDER_VOICE_NOTIFICATIONS` | `true` | spoken by the voice engine |

Reminders fire only while JARVIS is running (`python -m desktop.launcher`, or `scripts/run_voice.py` for a
voice-only scheduler). If the tables do not exist yet, JARVIS still runs; task requests are answered with the
database-unavailable message and the scheduler logs errors until you run the migration.

## Default listing order

Tasks: overdue first (oldest due date first), then due date ascending (no due date last), then priority
(highest first), then creation time, then id. This is a plain, deterministic sort, not "smart" prioritisation.
Reminders: soonest first.

## Testing

`tests/test_task_*.py`, `tests/test_reminders_scheduler.py`. Repository and service tests run on an isolated
in-memory SQLite database with foreign keys enforced, and on a disposable PostgreSQL when
`JARVIS_TEST_DATABASE_URL` is set (skipped otherwise). Conversation tests use a scripted fake LLM. The
scheduler is tested with a fake clock (and once with a real thread). `tests/integration` has:

- a real-Ollama check that the model returns a valid reminder action (skips without Ollama);
- one real tray notification (`JARVIS_TEST_REAL_NOTIFICATION=1`; it shows a balloon on screen);
- a real-PostgreSQL create/restart/retrieve/complete/cancel/scheduler test (skips without
  `JARVIS_TEST_DATABASE_URL`, and only against a disposable database: it drops the two tables).

Passing on SQLite does not prove behaviour on your PostgreSQL.

## Limitations

- English only; a subset of recurrences (no "every 2 weeks", no "second Tuesday"); "tomorrow morning"-style
  vague times are not understood and JARVIS asks for a time.
- Reminders fire only while JARVIS is running; there is no Windows service. Missed ones follow the policy above.
- Voice delivery is best effort (accepted by a running engine; spoken between conversations); the desktop
  balloon needs the tray. If neither is available the reminder is retried, then expires, and is never claimed
  as delivered.
- Approval for cancelling is a spoken "yes"; there is no approval UI and no undo for a cancellation (a
  completed or cancelled task can be reopened in code, not by voice).
- A task's text can only be edited through the code API; there is no voice "rename" or "reschedule" command.
- Listing is capped (five items are read aloud, then "and N more").
- No task-to-memory or task-to-graph links, sharing, sub-tasks or productivity scoring.
- A missing database means requests fail with an explanation; nothing is queued for later.
