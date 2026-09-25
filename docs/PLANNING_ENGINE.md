# Planning engine

`agent/intelligence/planner.py` (plan), `plan_executor.py` + `confirmation.py` (execute). **PLAN and EXECUTE are separate steps.**

## Plan (automatic, changes nothing)

1. Busy time: calendar events that block time, saved events not on the calendar, and events an email mentions that are not on the calendar yet
   (kept free so the plan does not double-book them), each padded by `JARVIS_PLAN_BUFFER_MINUTES`.
2. Free time: the working day (`JARVIS_WORKDAY_START/END`, changeable by voice) minus busy time; for today, from now (rounded up to 15 min). Slots under 25 min are ignored.
3. Tasks: open and READY. A task blocked by an unfinished dependency is not planned and the plan says why. Order: overdue (100), due today (90), tomorrow (70),
   <= 3 days (50), <= 7 days (30), + 8 x priority, + 10 if started, + 25 if related to an event within 3 days; ties by due date then title.
4. Placement: first fit, blocks of at most 2 hours (longer tasks are split into parts), never after the task's own due time.
   No estimate: `JARVIS_DEFAULT_TASK_MINUTES` is used **and the explanation says it was assumed**.
5. Anything that does not fit is listed with the reason ("it is due at 9 AM, before there is any free time that day"). Nothing is dropped silently.
   Each block stores the evidence for its placement (`why did you schedule that`).

Tested properties: blocks stay inside the working day, never overlap calendar events (or their buffers), never overlap each other, never end after the
task's due time, and calendar mutations stay at zero until a confirmed "yes".

## Execute (confirmed)

"Add it to my calendar" -> `PlanExecutor.propose_plan` picks a writable calendar and asks: *"I'll add 2 work blocks to your calendar 'Personal' for today:
9 AM to 10 AM 'Finish JARVIS documentation'; ... Nobody will be invited. Shall I go ahead?"* Only a clear yes to that prompt runs it
(see `PROMPT_INJECTION_SECURITY.md` for the confirmation rules). Each event gets a deterministic id derived from the plan and block, so a repeated yes or a retry
cannot duplicate. After every create the event is **read back** and compared (title and start/end within 1 minute). Reported outcomes:
"Done ... confirmed each one" only when all read-backs match; otherwise which blocks failed and why, or "I couldn't confirm whether X was created, so please
check your calendar". Nothing existing is modified or deleted.

## Conflicts and cross-checks

Calendar overlaps, several deadlines on one day, reminder collisions, a task due later than an email asks for it, memory-vs-calendar disagreement, and an
event named in an email that is not on the calendar (with an offer, never an automatic add). All reported as facts.

## Limits

Weekends are skipped when *searching* for a work-block slot but a plan for a weekend day can be requested explicitly. Task estimates come only from
task metadata `estimate_minutes`; there is no learning of how long tasks take.
