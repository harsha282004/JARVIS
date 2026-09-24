# Google Calendar Integration (Phase 12)

JARVIS can connect to your Google Calendar: list your calendars, read and search events, show an event's details,
create, update and cancel events, through the existing
VoiceEngine -> ConversationEngine -> AgentBrain -> PermissionManager -> Tool path. Every change asks you first.

It does not implement messaging, proactive alerts, a daily briefing, autonomous scheduling or rescheduling, a
dashboard or any browser/desktop automation. Nobody is ever emailed or invited by JARVIS.

## Architecture

```
VoiceEngine -> ConversationEngine -> AgentBrain
                     |                  |  validated CalendarAction (words only; data, never executed by the brain)
                     v                  v
             TaskActionExecutor  (the same executor as Phases 9-11)
                     1. tool.resolve()   validation; for update/cancel a bounded, read-only (GET) lookup finds the exact event
                     2. PermissionManager.request_permission(), bound to the exact parameters (calendar id, event id, etag, values)
                     3. Tool.execute()   re-checks the permission, then run():
                             CalendarService -> CalendarClient (interface) -> HttpCalendarClient -> Google Calendar REST
                             CalendarEventSync -> Phase 11 EventService (bounded read-through mapping)
```

| Module (`integrations/calendar/`) | Role |
|---|---|
| `models.py` | `CalendarEvent`, `CalendarInfo`, `CalendarEventDraft/Patch`, and the `CalendarError` hierarchy (each with a speakable, content-free `user_message`) |
| `base.py` | `CalendarClient` interface, `CalendarIntegration` (registry entry) |
| `auth.py` | `CalendarAuthenticator` (on top of the shared `integrations/google_oauth.py`, also used by Gmail) |
| `client.py`, `parser.py` | `HttpCalendarClient` (fixed host, bounded retries, etag, idempotent create) and Google JSON -> JARVIS models |
| `rrule.py` | builds RRULE text from Phase 9 recurrence, in code (the model never writes one) |
| `service.py` | calendar cache, selection, bounded reading, conflict detection using Phase 11 rules |
| `sync.py` | the bounded Phase 11 mapping (see below) |
| `intents.py`, `tools.py` | the `CalendarAction` the model may propose, and the seven tools |

The rest of JARVIS depends on `CalendarClient`/`CalendarService`, not on Google or HTTP. No Google SDK is used
beyond `google-auth` and `google-auth-oauthlib` (already used by Gmail); requests use `httpx`. Nothing was added to
`requirements.txt`.

## Google Cloud setup (manual, once)

1. Create or select a project at <https://console.cloud.google.com/>.
2. **APIs & Services -> Library**: enable the **Google Calendar API**.
3. **OAuth consent screen**: *External* (or *Internal* for Workspace), fill in the app name and your email and add
   yourself under **Test users**. In *Testing* mode refresh tokens expire after 7 days; run `auth` again.
4. **Credentials -> Create credentials -> OAuth client ID -> Desktop app.**
5. Give JARVIS the client, in **either** way (never paste it into chat, never commit it):
   - save the downloaded JSON as `.jarvis/calendar/credentials.json` (or the path in `JARVIS_CALENDAR_CREDENTIALS_PATH`), **or**
   - put `CALENDAR_CLIENT_ID` / `CALENDAR_CLIENT_SECRET` in your local `.env`. If they are empty, the `GMAIL_CLIENT_ID` /
     `GMAIL_CLIENT_SECRET` values are used (one Desktop client can serve both APIs; the tokens stay separate).
6. Set `JARVIS_CALENDAR_ENABLED=true` in `.env`.
7. Sign in once: `python scripts/calendar_cli.py auth` (opens your browser). The token is saved to
   `.jarvis/calendar/token.json`, git-ignored and owner-only where the OS supports it.
8. Check: `python scripts/calendar_cli.py status` (local), `check` (online: lists your calendar names),
   `events --days 3` (titles and times only). The CLI only reads.

Scopes requested (the narrowest that works): `calendar.events` (read/write events) and
`calendar.calendarlist.readonly` (list your calendars). JARVIS cannot create, delete, share or change calendars, or
change calendar settings or access control.

## Configuration (`.env`, see `.env.example`)

| Setting | Default | Meaning |
|---|---|---|
| `JARVIS_CALENDAR_ENABLED` | `false` | registers the calendar tools |
| `JARVIS_CALENDAR_CREDENTIALS_PATH` | `.jarvis/calendar/credentials.json` | OAuth client JSON |
| `JARVIS_CALENDAR_TOKEN_PATH` | `.jarvis/calendar/token.json` | saved token (never commit) |
| `JARVIS_CALENDAR_MAX_RESULTS` | `20` (1-100) | most events read or spoken per request |
| `CALENDAR_CLIENT_ID` / `CALENDAR_CLIENT_SECRET` | empty | alternative to the JSON file (secret is a `SecretStr`) |

## What you can say

- "Which calendars do I have?" / "What's on my calendar today?" / "What do I have tomorrow?" / "Anything this Friday?" /
  "Show my meetings this week" / "Anything tomorrow morning?"
- "Find my next interview." / "Show events containing project." / "Tell me about my meeting with John."
- "Create a meeting tomorrow at 10 AM." (JARVIS asks "How long should the meeting be?": it never invents a length.)
- "Schedule my project review Friday at 10 AM until 11:30 in Lab 202." / "Add my college fest on October 10." (all day)
- "Every Monday at 10 AM, schedule a project meeting." (asks for confirmation and says it has no end date)
- "Move my project meeting to 4 PM." / "Rename the interview." / "Change the location to Lab 202."
- "Cancel Thursday's interview."
- "Do I have any conflicts tomorrow afternoon?"

## Permissions

| Tool | Risk | Approval |
|---|---|---|
| `calendar_list`, `calendar_events`, `calendar_search`, `calendar_get_event` | LOW | none (read-only, bounded) |
| `calendar_create_event`, `calendar_update_event`, `calendar_cancel_event` | MEDIUM | you say "yes" first (one time) |

Unknown tool names are denied. The approval is bound to the exact resolved parameters: calendar id, event id, etag
and the new values. An approval for one event cannot be used for another, or with changed values, and is used once.
The "yes" is read by code (`classify_confirmation`), never by the model. The identification lookups for update and
cancel are read-only GETs (which LOW-risk reads would allow without approval anyway); nothing is created, changed or
deleted before your "yes". Event ids come only from Google's responses, never from the model: the model describes an
event in words and code finds it. If several events match, or none, JARVIS asks or says so.

## Behaviour

- **Duration and times**: a meeting without a length is asked about. "at 8" (AM or PM), "next week", passed dates and
  end-before-start are asked about, never guessed. All-day events only when you say so (end date exclusive in Google).
- **Recurrence**: built from the Phase 9 recurrence parser as an RRULE in code, with an optional count or end date. An
  endless series is stated aloud before you confirm. A repeating event's update/cancel affects only the occurrence
  meant unless you say "every occurrence"; changing the time of a whole series is refused for now. Repeating all-day
  events are not supported yet.
- **Attendees**: only explicit, valid email addresses you give (max 10). Google is asked not to send anything
  (`sendUpdates=none`), so guests are recorded but **no invitation, update or cancellation email is sent**; JARVIS says so.
- **Video meeting links**: JARVIS does not create Google Meet links. Existing links are reported ("has a video meeting
  link") but not read aloud.
- **Time zones**: your `JARVIS_TIMEZONE` (or a zone you name for an event); each event keeps its own zone, DST is
  handled by the zone rules.
- **Conflicts** (Phase 11 rules: overlap, all-day days, declined/free events ignored): reported before creating or moving.
  JARVIS never moves or deletes anything to resolve a clash; it says "Tell me a different time, or say 'create it anyway'".
- **Errors** (not set up, token revoked/expired, rate limited, offline, no permission, event changed meanwhile, event
  gone) each get a clear spoken message; JARVIS keeps running. A write whose result cannot be confirmed is reported as
  unconfirmed, never as done. Creation uses a client-generated event id, so a retry cannot create a duplicate. Updates
  and deletes use the event's etag, so an event changed elsewhere is not overwritten.

## Phase 11 integration

Google Calendar is the source of truth for calendar events; nothing is mirrored wholesale into PostgreSQL.

- Reading the calendar stores nothing.
- An event JARVIS creates or updates gets **one** Phase 11 `Event` (`source_type=GOOGLE_CALENDAR`, `source_id` =
  `calendar_id/event_id`), so deadline intelligence knows about it and a later change or cancellation finds the same
  record (no duplicates). Recurring series are not mapped (one record cannot represent a series).
- `reconcile()` runs lazily, at most every 10 minutes and for at most 20 mapped records per run: an event deleted on
  Google cancels its Phase 11 record and a moved/renamed one is updated. An outage cancels nothing. It is not a
  continuous sync.
- Mapping is best-effort: if the local database fails, the calendar operation still succeeds.
- No migration: `source_type` is a plain string column.
- Conflict detection reuses the Phase 11 overlap rules through a temporary, never-stored `Event`.

## Privacy and security

- Tokens and secrets are never logged, printed, sent to the model or committed (`.jarvis/`, `calendar_token*.json`,
  `credentials*.json`, `.env` are git-ignored). Logs contain ids and error types only, never titles, descriptions or
  attendees. Errors log the exception type only.
- The model can only propose a validated `CalendarAction` made of words. Any of `event_id`, `calendar_id`, `etag`, URLs,
  methods, headers, params, tokens, paths, commands, SQL, RRULEs, `sendUpdates` or conference data in its arguments
  rejects the action. The client talks only to the fixed Google Calendar host, with GET/POST/PATCH/DELETE, and ids are
  validated and percent-encoded.
- Event titles, descriptions, locations and guests are untrusted (they can come from a stranger's invitation): they are
  sanitized, never shown to the model, and replies containing them (and the confirmation questions naming an existing
  event) are replaced by a placeholder in the conversation history. Text inside an event can never trigger a tool call.
- Only calendars you have selected in Google are read (at most 10). Read-only calendars (holidays) are never written.

## Testing

- `tests/test_calendar_auth_client_parser.py`: OAuth (refresh, revoked, unreachable, no secret leakage), HTTP client
  (real `httpx.MockTransport`: paging, retries, error mapping, etag, idempotency), parser, RRULE.
- `tests/test_calendar_actions_engine.py`: AgentBrain -> permission -> tool -> service with an in-memory client double:
  validation, reading, creation (duration, all-day, recurrence, attendees, time zones), conflicts, update, cancel,
  permission binding, the Phase 11 mapping and reconcile, prompt-injection defence, static code guards.
- `tests/test_calendar_runtime.py`: settings, bootstrap, CLI, Git-ignore and secret hygiene.
- `tests/integration/test_calendar_real.py`: real Google, **skipped** unless a token exists. Its create/update/delete
  test additionally needs `JARVIS_TEST_CALENDAR_ID` (a dedicated, non-primary test calendar) and only touches events it
  creates itself ("JARVIS-TEST ...", deleted at the end). Run: `pytest -m integration tests/integration/test_calendar_real.py`.

## Limitations

- English only; times are resolved by the Phase 9/11 rule-based parsers.
- Recurring events: single occurrence changes and whole-series title/location/notes only; no series time changes, no
  repeating all-day events, no exceptions editing.
- No guest invitations, RSVP handling, Meet link creation, reminders/notification settings, calendar sharing or
  creation. No push/webhook sync. No attachments.
- Google's free/busy of *other people* is not consulted; conflicts are against your selected calendars.
- Verified in development against a Google-API-shaped test double and mocked HTTP; real Google is tested only when a
  token (and, for changes, a test calendar) exists.
