# Proactive Intelligence (Phase 14)

Until now JARVIS waited for you to speak first. With proactive intelligence it can also **tell you** when something
already in your authorized sources deserves attention: a task is due soon or overdue, a deadline or interview or calendar
meeting is approaching, two calendar events overlap, or an email seems to need action.

The principle is **OBSERVE -> ANALYZE -> DECIDE -> NOTIFY**, never *observe -> act*. The engine only reads, and its only
outward effect is one short factual sentence handed to the notification channels JARVIS already has (the Windows tray and
the voice announcement queue). It never changes a task, event, calendar entry or email, never sends anything, never calls
a tool and never uses a language model.

(Phase 15's daily briefing is a separate, on-request feature: it produces briefing content and does not notify; see `docs/daily-briefing-productivity.md`.)

It is **off by default** (`JARVIS_PROACTIVE_ENABLED=false`). Reminders and tasks work exactly as before whether it is on or off.

## Architecture

```
 Tasks (Ph.9)   Events (Ph.11)   Google Calendar (Ph.12)   Gmail (Ph.10, opt-in)      <- existing services, read-only
      \              |                    |                       /
       +-------- SignalSource adapters (bounded, throttled) ------+
                                   |   ProactiveSignal
                          message templates (no LLM)
                                   |   NotificationCandidate
                          NotificationPolicy  (deterministic)
                                   |   DELIVER / DEFER / SUPPRESS / EXPIRE
                     NotificationRepository (atomic claim, history)
                                   |
        existing NotificationService channels:  DesktopNotifier (tray)   VoiceNotifier (announcement queue)
```

| Module (`agent/proactive/`) | Role |
|---|---|
| `models.py` | `ProactiveSignal`, `NotificationCandidate`, `SignalType`, `Urgency`, `CandidateStatus`, `PolicyDecision`, `HistoryRecord` |
| `sources.py` | `TaskSignalSource`, `EventSignalSource`, `CalendarSignalSource`, `GmailSignalSource` |
| `policy.py` | `NotificationPolicy`: the single place that decides whether to notify |
| `messages.py` | signal -> a short, calm, factual sentence (deterministic templates) |
| `repository.py` | the notification history and the atomic claim (`proactive_notifications` table) |
| `engine.py` | `ProactiveEngine.run_once()`: one pass, never raises |
| `intents.py`, `tools.py` | the read-only `proactive_explain` action ("why did you notify me?") |

**No second scheduler and no second notifier.** The engine runs as an *extra pass* of the existing `ReminderScheduler`
thread (`extra_passes`), and delivers through the existing `DesktopNotifier` / `VoiceNotifier` and `AnnouncementQueue`.
The engine throttles itself to `JARVIS_PROACTIVE_POLL_SECONDS`, so the scheduler's shorter poll does not make it check
more often. If reminders are disabled but proactive is enabled, the same thread still runs (with no reminder pass).

## Signal sources and signal types

| Signal type | Source | When |
|---|---|---|
| `task_due` | Phase 9 tasks | an open task is due within the lookahead (24 h) or within an hour |
| `task_overdue` | Phase 9 tasks | an open task passed its due time (only if overdue for less than the lookahead / 24 h) |
| `event_approaching` | Phase 11 events, Google Calendar | an event starts within the lookahead / within an hour (calendar: 1 hour and 15 minutes) |
| `deadline_approaching` | Phase 11 deadlines | a deadline/assignment/application is due within the lookahead / within an hour |
| `deadline_overdue` | Phase 11 deadlines | an open deadline passed |
| `calendar_conflict` | Google Calendar | two timed events overlap before they start (all-day entries are ignored) |
| `important_email`, `action_required_email` | Gmail (opt-in) | an unread inbox email from the last 2 days that the deterministic Phase 10 classifier calls important / action-required. Ordinary mail never notifies. At most 3 per refresh |

**Deliberately not produced**, because no honest signal exists:

- *Reminders*: Phase 9's scheduler already delivers them; a second path would notify twice. The engine never touches reminders.
- *Messaging*: Phase 13 has no unread/new-message signal (a Telegram bot cannot say what is unread), and background message reading was forbidden there.
- *Memory*: personal memories have no time relationship to signal.

Events linked to a task are skipped (the task's signal covers them), and a Phase 12 mirror of a Google Calendar event is skipped
when the calendar source is active (it covers it directly), so one situation gives one signal.

## The signal model

`ProactiveSignal`: `signal_id` (the stable dedupe key), `signal_type`, `source_type` (`task`/`event`/`calendar`/`gmail`),
`source_id`, `source_reference` (human-readable: "your task list"), `title`, `description`, `priority` (the Phase 9
`TaskPriority`), `urgency`, `confidence` (the Phase 6 `Confidence`), `detected_at`, `relevant_at`, `expires_at`, `tier`,
`metadata`. External text (an email subject, a calendar title) is sanitized (no angle brackets or control characters) and only ever quoted.

## Notification candidate model

`NotificationCandidate`: `candidate_id`, `signal_id`, `message`, `reason`, `priority`, `urgency`, `created_at`, `expires_at`,
`delivery_channels`, `suppression_reason`, `status` (`pending`, `delivered`, `suppressed`, `expired`, `failed`). Signal
detection is separate from delivery: the policy decides whether a candidate is actually shown.

## Priority and urgency

- **Priority** reuses `TaskPriority` (low/medium/high/critical), taken from the source: a task's own priority; an event's own
  priority or, if none, a fixed mapping by type (interview, exam, deadline, assignment, application -> high; meeting,
  appointment -> medium; others -> low); calendar events and conflicts -> medium; emails -> medium. Nothing is "critical"
  unless the user set it. A language model never assigns priority.
- **Urgency** comes from time alone: overdue or within 15 minutes = *immediate*; within an hour = *soon*; within the lookahead =
  *upcoming*; further away = *normal* (never notified). Emails have no time relationship, so they are fixed at *upcoming*.

## Time windows, tiers and stable keys

Each source notifies when a thing **enters a tier**: tasks and deadlines at the lookahead (default 24 h) and 1 hour; calendar events
at 1 hour and 15 minutes; overdue once. Only the tightest tier already entered produces a signal, so starting JARVIS 30 minutes
before a deadline gives one notification, not one per skipped tier. The signal key is a hash of *(type, source, source id, tier,
due/start time)*: the same situation always has the same key (so it notifies at most once, even across restarts), and a meaningful
change (a moved due date, a new tier) has a new key.

## Notification policy (deterministic and explainable)

The first rule that applies wins, and every decision carries a short reason:

1. **Expired**: the thing has already started/is due -> expire.
2. **Duplicate**: this exact signal was already delivered or is being delivered -> suppress.
3. **Confidence**: below medium -> suppress. 4. **Priority**: low priority only when soon or immediate -> suppress.
5. **Quiet hours** (below). 6. **Cooldown** (below). 7. **Hourly limit**: at most `JARVIS_PROACTIVE_MAX_PER_HOUR` per hour, except
   immediate ones -> defer. 8. **Channels**: the tray always; voice only for soon/immediate or high/critical priority (and as the
   fallback when there is no tray). No channel -> defer.

Nothing looks at wording, and no model takes part, so a model can never talk its way past a rule.

### Quiet hours

`JARVIS_PROACTIVE_QUIET_HOURS_ENABLED`, `..._QUIET_START` (default 23:00) and `..._QUIET_END` (07:00), in `JARVIS_TIMEZONE`, may cross
midnight. During quiet hours everything is **deferred** (it stays pending and is decided again on later cycles, so it is delivered
when quiet hours end if it is still relevant, and dropped if it expired). The documented exception: a signal that is both **critical
priority and immediate urgency** is delivered, to the **tray only, never voice**. Nothing else, however it is worded, can break through.

### Cooldown and de-duplication

- The stable key plus the history table guarantee a signal notifies once, including after a restart.
- **Cooldown** (`JARVIS_PROACTIVE_COOLDOWN_MINUTES`, default 60): the same source is not notified again within the cooldown, *unless* the
  urgency escalated (e.g. "due tomorrow" then "due in an hour") or the thing meaningfully changed (its due/start time moved).

## Delivery, channels and concurrency

- **Tray**: the existing `DesktopNotifier` on the existing tray icon (title "JARVIS"). If the tray is disabled or fails, voice or a deferral
  is used; nothing crashes.
- **Voice**: the existing `VoiceNotifier` puts the sentence on the existing `AnnouncementQueue`. The engine never touches audio; the
  VoiceEngine speaks queued text on its own thread **only between conversations**, so JARVIS never talks over you or over a request in
  progress. If the voice engine is not running the channel simply fails and the tray still delivers.
- **Atomic claim**: delivery first *claims* the signal by inserting its unique key into `proactive_notifications`. Exactly one caller
  (scheduler, second engine, second process) wins; a stale claim (a crashed worker, older than 2 minutes) can be taken over by exactly
  one caller. A signal is recorded **delivered only after a channel accepted it**. A failed delivery stays retryable with backoff
  (1, 2, 4, 8, 15 minutes) up to 5 attempts, then it is given up.
- **Failures are contained**: a source that raises is skipped for 10 minutes while the others keep working; a database failure ends
  that pass and the next one recovers; a channel exception never stops the other channel. Logs contain counts and exception types only.

## Source traceability and "Why did you notify me?"

Every notification is stored with its source type, source id, a human-readable source, and a short factual reason. Ask "why did you
notify me?" (optionally with words from it) and JARVIS answers from that record, for example: *"I told you 'Your task 'Submit
internship application' is due in about 40 minutes.' today at 2:30 PM because your task 'Submit internship application' is due March 4 at 3:10 PM.
The source is your task list: a task (reference: ...)."* This is the `proactive_explain` action: read-only, LOW risk, no approval, and its
reply is kept out of the conversation history. It exposes only the stored reason, never internal reasoning.

## Configuration (`.env`, see `.env.example`)

| Setting | Default | Meaning |
|---|---|---|
| `JARVIS_PROACTIVE_ENABLED` | `false` | master switch; when false the engine produces nothing (reminders/tasks unaffected) |
| `JARVIS_PROACTIVE_POLL_SECONDS` | `60` | how often the engine looks (10-3600) |
| `JARVIS_PROACTIVE_LOOKAHEAD_MINUTES` | `1440` | the first tier for tasks/deadlines (15-10080) |
| `JARVIS_PROACTIVE_COOLDOWN_MINUTES` | `60` | per-source cooldown (0-1440) |
| `JARVIS_PROACTIVE_QUIET_HOURS_ENABLED` / `_START` / `_END` | `true` / `23:00` / `07:00` | quiet hours, `HH:MM` |
| `JARVIS_PROACTIVE_MAX_PER_HOUR` | `6` | hourly cap (immediate ones exempt) |
| `JARVIS_PROACTIVE_EXTERNAL_POLL_MINUTES` | `10` | Calendar/Gmail are read at most this often (5-240) |
| `JARVIS_PROACTIVE_CALENDAR` | `true` | observe Google Calendar (needs `JARVIS_CALENDAR_ENABLED` and its setup) |
| `JARVIS_PROACTIVE_GMAIL` | `false` | observe Gmail (needs `JARVIS_GMAIL_ENABLED`; opt-in because it reads your inbox in the background) |

Channels reuse `JARVIS_REMINDER_DESKTOP_NOTIFICATIONS` and `JARVIS_REMINDER_VOICE_NOTIFICATIONS`. The section 6 and section 24 names in the
Phase 14 brief differed (`NOTIFICATION_` vs `PROACTIVE_`); the `JARVIS_PROACTIVE_*` names are used throughout. Run
`alembic -c database/alembic.ini upgrade head` once (migration `0006_proactive`). There is no settings UI (Phase 21).

## Persistence

One table, `proactive_notifications` (migration `0006_proactive`, reversible: downgrade drops only this table): id, unique `dedupe_key`,
signal type, source type/id/reference, JARVIS's own notification sentence and reason, priority, urgency, status, channels, attempts,
`relevant_at`, and created/claimed/delivered/failed times. **No email or message bodies are stored**: for email signals only a generic
sentence is kept ("An email may need your attention (details are in Gmail)"), and calendar/task/event titles are the user's own
data already stored by their own phases. Finished history older than 30 days is pruned daily.

## Security

- **A signal is information, not authorization.** The engine has no PermissionManager, no tool executor and no way to reach a mutating
  method of any service; it cannot create, complete, cancel, update or delete a task, event, calendar entry or email, or send anything.
  Tests replace every such method with a failing stub and run the engine over hostile data.
- An email that says "send money now" is at most reported (quoted, sanitized, length-bounded) and never acted on. Calendar titles and
  email subjects cannot close a quote, add markup or inject a newline, and the notification sentence is built from fixed templates.
- No language model is used anywhere in the engine, so prompt injection has nothing to steer. The only model-facing surface is
  `proactive_explain`, whose arguments are validated words (no ids, no settings, no priorities, no channels), and whose output the model never reads.
- Quiet hours, limits, priority and channels are decided by code from source data; the model cannot change them, and no action can change
  notification settings.

## Privacy

Nothing is logged except counts and exception types (no titles, subjects, senders, bodies or tokens). External sources are read at most
every `JARVIS_PROACTIVE_EXTERNAL_POLL_MINUTES` and only their small results are cached, in memory, until the next refresh.

## Testing

- `tests/test_proactive_signals_policy.py`: signals per source, tiers and keys, wording and sanitizing, the policy (quiet hours, cooldown, duplicates, priority, urgency, expiry, hourly limit, channels).
- `tests/test_proactive_engine_notifications.py`: end to end over real task/event services and a real history: delivery, duplicates, restart, quiet hours, cooldown/escalation, channels (tray, voice queue, unavailable), failure and bounded retry, source isolation, and concurrency (atomic claim, real threads, multiple engines).
- `tests/test_proactive_security_actions.py`: `proactive_explain`, permissions, prompt injection, "cannot act" guarantees, privacy (logs, stored history), static guards.
- `tests/test_proactive_runtime.py`: settings, bootstrap, scheduler reuse (no second thread), reversible migration.
- `tests/integration/test_proactive_real.py`: **real** scheduler thread + engine + SQLite file + announcement queue (runs by default); a **real Windows tray** notification (only with `JARVIS_REAL_TRAY_TEST=1`).

## Limitations

- Off by default and English-only wording; times use `JARVIS_TIMEZONE`.
- Gmail and Calendar are read at most every 10 minutes (configurable), so an event created moments ago may be noticed a few minutes late.
- The engine only runs while JARVIS runs (inside the launcher); it cannot notify while JARVIS is closed. Items missed meanwhile are handled by the tier rule (one notification for the tier that applies on start) and long-overdue items (over 24 h) are not announced (a briefing's job, a later phase).
- Emails have no time relationship, so their urgency is fixed and they use the tray only.
- No snooze, per-item mute or settings UI; there is no way to change what it observes except configuration.
- Voice announcements are spoken only between conversations, so one queued while you talk is heard after; the queue holds at most 10.
- Tray notifications can be hidden by Windows Focus Assist; that cannot be detected, and the notification is recorded as delivered once the tray accepted it.
- Notification history is local; reminders, messaging and memory are not observed (see above).
