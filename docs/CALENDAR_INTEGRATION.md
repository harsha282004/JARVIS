# Calendar integration (hub)

Builds on Phase 12 (`docs/google-calendar-integration.md`): scopes `calendar.events` + `calendar.calendarlist.readonly`, per-call confirmation bound to the exact parameters.

## Read / search / schedule

`search_calendar` (query, or a start/end range), "What's my schedule today?" / "…tomorrow" / "…this week", "Do I have anything at 4 PM?" — answered from the live calendar; an unreachable calendar is said to be unreachable, never "nothing scheduled".

## Normalization

`NormalizedItem(kind=EVENT, source="calendar", source_id="<calendar_id>/<event_id>", external_id="<event_id>", timestamp=start)` with metadata: end, all-day, timezone, location, status, calendar id, participant **names** (not addresses; up to 10), blocks-time, recurring, etag. The external id is kept because a safe update/delete needs it.

## Conflicts and duplicates (facts only, nothing modified)

`calendar_issues`: overlaps ("You have two events scheduled at 4 PM: …"), duplicates (same title and start), task deadlines falling inside an event. The Phase 17 findings additionally report overlaps proactively.

## Write operations

* **Create**: "Create a meeting tomorrow at 6 PM" → needs the `CREATE_EVENT` permission (opt-in: "Allow JARVIS to create calendar events", or the dashboard toggle) → JARVIS states the exact event and asks → a clear "yes" → create (deterministic event id: a repeated yes cannot duplicate) → **read back** and compare title and times → only then "Done. I added 'Meeting' tomorrow at 6 PM to your calendar and confirmed it's there." A failure, or an outcome that cannot be confirmed, is reported as such.
* **Update / delete**: through the Phase 12 tools (voice, confirmation bound to the exact event). The hub adapter also provides `update_verified` / `delete_verified` (read-back verification; tested) but they are **not exposed as spoken hub tools**.
* Nobody is ever invited; nothing is changed without a yes.

## Cross-references

* **Email ↔ calendar**: the Phase 17 engine merges an email-mentioned event into the calendar event of the same name and day; if none exists it says "An email mentions … but I don't see a matching calendar event. Would you like me to add it?" and only adds after your confirmation (tested end to end).
* **Calendar ↔ task**: evidence-based only (a task title wholly contained in an event name); a shared generic word is never enough.

## Synchronization

A bounded window (7 days back, 60 ahead, ≤100 events). The cursor holds the ids and start times seen last time; an event no longer returned (and still inside the window) is marked removed; changed events update the same row. **Google Calendar push channels are not implemented** (they need a public HTTPS endpoint); `syncToken` incremental listing is not used because the existing client lists a window. Interval 15 minutes with backoff.
