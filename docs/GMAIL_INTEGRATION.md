# Gmail integration (hub)

Builds on Phase 10 (`docs/gmail-intelligence.md`): read-only `gmail.readonly`, OAuth desktop flow, PermissionManager-gated tools. Phase 18 adds the adapter, analysis and synchronization.

## Capabilities

| | How |
|---|---|
| Search | "Find emails about hackathons", "What emails do I have about the project?" → `search_email` (live, bounded; falls back to synchronized copies **and says so** if Gmail is unreachable) |
| Read | `read_email`: sanitized, ≤2000 characters, marked untrusted, with topic/importance/extracted dates and `injection_suspected` |
| Topic | college, work, internship, hackathon, competition, conference, interview, finance, personal, newsletter, other (`integrations/gmail/analysis.py`; the rule that fired is kept) |
| Importance | CRITICAL / IMPORTANT / NORMAL / LOW with reasons. CRITICAL: an interview within 48 h, something dated within 24 h, or "urgent" with a date within 72 h. IMPORTANT: needs action / dated within a week / interview, hackathon, internship, college with a date / confirmed registration. LOW: promotional. A suspicious email is capped at NORMAL. |
| Deadlines | "Final project submission is due October 5." → DEADLINE item: title, due (end of day), kind (`submission_deadline`), status `pending`, evidence sentence, confidence, `message_id` |
| Events | interviews, hackathons, competitions, conferences, meetings, project reviews → EVENT items with time, location (`Location:`/`Venue:`/"will be held at"), confidence |
| Registrations | "Your registration for XYZ Hackathon is confirmed." → EVENT item `registration: completed` (`high` confidence) or `mentioned` (`low`) |
| Attachments | metadata in the email item (name, type, size). Download: `GmailAdapter.download_attachment` — needs `READ_ATTACHMENT` (opt-in), documents only (PDF/DOCX/TXT/MD), ≤10 MB, sanitized file name, saved under `.jarvis/attachments/`. Nothing is downloaded automatically. |

## Notifications (do not announce every email)

New emails become bus events (`EMAIL_RECEIVED`). Only **CRITICAL** ones notify (through the NotificationCenter: quiet hours, de-duplication, acknowledgement). IMPORTANT ones are stored for the briefing without sound or popup; NORMAL/LOW are never announced.

## Synchronization

Event-driven where possible, otherwise incremental polling (see `SYNC_ENGINE.md`): first sync reads `JARVIS_GMAIL_SYNC_INITIAL_DAYS` (14) back; later syncs send `in:inbox after:<epoch of the newest message seen>`, so only new mail is read. **Gmail push (Pub/Sub `watch`) is not implemented**: it requires a Google Cloud Pub/Sub topic and a publicly reachable HTTPS endpoint, which a local desktop assistant does not have. Each integration has its own interval (Gmail: 10 minutes; the loop that checks what is due runs every `JARVIS_SYNC_LOOP_SECONDS`), with backoff on failure.

## Status of the "Definition of done" items

OAuth works — **not executable here** (needs your Google consent; the flow, refresh and revocation paths are tested with mocks/real `Credentials` objects). Search/read/classification/importance/deadline/event/registration extraction/sync/token expiry — implemented and tested with synthetic mail.

## Limits

Rule-based English extraction; ambiguous dates are asked about, never guessed. A message's HTML is reduced to text; large mailboxes are read in bounded pages (≤50 messages per sync run, continued next time via a page cursor). Only the inbox is synchronized.
