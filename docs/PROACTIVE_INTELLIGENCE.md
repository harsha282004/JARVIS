# Proactive intelligence (Phase 17 layer)

Not to be confused with Phase 14 (`docs/proactive-intelligence.md`), which notifies about due tasks, approaching events and email that needs action. The
Phase 17 layer adds **cross-source** findings and routes everything through the shared `NotificationCenter`. It is **off by default**
(`JARVIS_INTELLIGENCE_PROACTIVE=false`); the answers to questions work regardless.

## Detection (deterministic findings)

approaching deadlines with unfinished tasks (+ per-user lead times such as "one day before project deadlines"), overdue tasks, an email-mentioned event
missing from the calendar, calendar overlaps, several deadlines on one day, blocked tasks, a task due later than an email asks for it,
memory/calendar conflicts, reminder collisions, missed reminders, tasks an email asks for that do not exist, preparation needed for an interview/exam/review/hackathon
within 48 h, suspicious content. Each finding is facts first, then an optional suggestion, with evidence attached.

## Delivery rules (`backend/core/notifications.py`)

| Level | Behavior |
|---|---|
| CRITICAL | immediate, desktop + voice, even in quiet hours |
| IMPORTANT | desktop (+ voice unless past the voice cutoff / quiet hours); in quiet hours it waits and is released afterwards; always in the briefing |
| NORMAL | stored for the briefing |
| LOW | recorded silently |

De-duplication: same key and same content is never delivered twice (until acknowledged + cooldown, or the content changed, or it escalated). Muted categories
are suppressed except CRITICAL. A user preference can promote a keyword (e.g. hackathon) to IMPORTANT. PRIVATE mode limits everything to CRITICAL. A failed delivery is kept and retried.
History and acknowledgements persist across restarts (tested: a restarted JARVIS does not repeat what was already said).

## Runner (`IntelligenceRunner`)

Wakes on bus events (`EMAIL_RECEIVED`, `CALENDAR_UPDATED`, `TASK_CREATED/COMPLETED`, `SYSTEM_RESUME`, `INTEGRATION_RECOVERED`, an executed action) after a 2 s debounce,
otherwise every `JARVIS_INTELLIGENCE_INTERVAL_SECONDS`. Skips the evaluation when the sources are unchanged (time-based rules are still re-evaluated every 10 min).
Does nothing in PRIVATE mode. A failing run backs off and never affects the rest of JARVIS. Optional automatic task creation from a clear, unsuspicious email
(`JARVIS_AUTO_CREATE_TASKS` or "create tasks from my emails automatically"): once per proposal, audited, never for flagged content.

## Recommendations never act

"I can create a two-hour work block tonight if you'd like" is a suggestion; the only path to a change is the confirmation engine. "Why are you telling me this?" answers
from the stored evidence (`ExplanationLog`).
