"""Briefings and focus answers composed from the snapshot, the context graph and the findings.

Every sentence is built from stored data (nothing is estimated or invented). A source that could not be read is named as such
("I couldn't check your calendar"), never treated as empty. The default answer is short; the detail is kept for "tell me more".

  morning()   TODAY / PRIORITIES / DEADLINES / IMPORTANT / EVENTS / ATTENTION
  evening()   completed today, still open, tomorrow, approaching deadlines, unresolved items (real counts only, no productivity scores)
  focus()     facts first (what is due, what is scheduled), then an optional suggestion
  important() what matters on a given day
"""

from datetime import date, datetime, timedelta

from agent.intelligence.context_engine import ContextResult
from agent.intelligence.findings import Finding, FindingKind, Urgency
from agent.intelligence.models import Answer, EntityKind, EVENT_LIKE, Provenance, RelationKind, Snapshot, SourceKind, SourceState, Statement, extracted, fact, suggestion
from agent.intelligence.phrasing import clock, day_word, join_and, quoted, status_word, when_phrase
from agent.intelligence.planner import DayPlanner, task_prov
from agent.memory.models import Confidence

ATTENTION_KINDS = frozenset({
    FindingKind.CALENDAR_OVERLAP, FindingKind.EMAIL_EVENT_NOT_ON_CALENDAR, FindingKind.MEMORY_CONFLICT, FindingKind.SOURCE_CONFLICT,
    FindingKind.BLOCKED_TASK, FindingKind.OVERDUE_TASK, FindingKind.TASK_LATER_THAN_REQUESTED, FindingKind.MISSED_REMINDER,
    FindingKind.SUSPICIOUS_CONTENT, FindingKind.MULTIPLE_DEADLINES, FindingKind.TASK_PROPOSAL,
})
PREP_WORDS = {EntityKind.INTERVIEW, EntityKind.EXAM, EntityKind.HACKATHON, EntityKind.PROJECT_REVIEW}


def source_notes(snap: Snapshot) -> list[str]:
    """Honest notes about sources that could not be read, so silence is never mistaken for 'nothing there'."""
    notes = []
    labels = {"calendar": "your calendar", "gmail": "your email", "tasks": "your tasks", "events": "your saved events", "memory": "your memory", "reminders": "your reminders"}
    for name, state in snap.states.items():
        if state is SourceState.UNAVAILABLE and name in labels:
            notes.append(f"I couldn't check {labels[name]} just now, so anything there is missing from this.")
    return notes


class BriefingComposer:
    def __init__(self, zone, planner: DayPlanner, max_items: int = 5):
        self._zone = zone
        self._planner = planner
        self._max = max_items

    # ---- helpers -----------------------------------------------------------------------------------------------------

    def _day_events(self, snap: Snapshot, day: date):
        out = []
        for c in snap.calendar:
            if c.end <= datetime.combine(day, datetime.min.time(), tzinfo=self._zone):
                continue
            if c.start.astimezone(self._zone).date() == day or (c.all_day and c.start.astimezone(self._zone).date() <= day < c.end.astimezone(self._zone).date()):
                out.append(c)
        return sorted(out, key=lambda c: (not c.all_day, c.start))

    def _calendar_line(self, snap: Snapshot, day: date, events) -> tuple[str, list[Statement]]:
        state = snap.states.get("calendar")
        label = day_word(day, snap.now, self._zone)
        if state is SourceState.NOT_CONFIGURED:
            return "Google Calendar isn't connected, so I can't tell you what's scheduled.", []
        if state is SourceState.UNAVAILABLE:
            return "I couldn't check your calendar just now.", []
        if not events:
            return f"Your calendar shows nothing scheduled {label}.", [fact(f"Your calendar shows nothing scheduled {label}.", Provenance(SourceKind.CALENDAR, "calendar", "your calendar", None, snap.now, Confidence.HIGH))]
        stmts = [fact(f"Your calendar shows {quoted(c.title)} {'all day' if c.all_day else 'at ' + clock(c.start, self._zone)}.",
                      Provenance(SourceKind.CALENDAR, f"{c.calendar_id}/{c.event_id}", f"calendar event {quoted(c.title)}", None, snap.now, Confidence.HIGH)) for c in events]
        listed = join_and([f"{quoted(c.title)} {'all day' if c.all_day else 'at ' + clock(c.start, self._zone)}" for c in events[: self._max]])
        more = f" and {len(events) - self._max} more" if len(events) > self._max else ""
        return f"Your calendar shows {listed}{more}.", stmts

    def _priority_tasks(self, snap: Snapshot, result: ContextResult, day: date, limit: int):
        scored = []
        for t in snap.tasks:
            if not t.is_open:
                continue
            score, _ = self._planner._score(t, snap, result, day)  # noqa: SLF001 - one scoring rule for plan, focus and briefing
            if score > 0 and (t.due_at is not None or t.priority >= 3):
                scored.append((score, t))
        scored.sort(key=lambda x: (-x[0], x[1].due_at or datetime.max.replace(tzinfo=self._zone), x[1].title))
        return [t for _, t in scored[:limit]]

    def _task_statement(self, t, snap: Snapshot) -> Statement:
        due = f" due {when_phrase(t.due_at, snap.now, self._zone, self._all_day(t.due_at))}" if t.due_at else ""
        return fact(f"{quoted(t.title)}{due}, {status_word(t.status)}.", *task_prov(t, snap))

    def _all_day(self, due: datetime) -> bool:
        local = due.astimezone(self._zone)
        return local.hour == 23 and local.minute == 59

    # ---- morning ---------------------------------------------------------------------------------------------------------

    def morning(self, snap: Snapshot, result: ContextResult, findings: list[Finding], important_notifications: list[str] | None = None) -> Answer:
        today = snap.now.astimezone(self._zone).date()
        events = self._day_events(snap, today)
        cal_text, cal_stmts = self._calendar_line(snap, today, events)
        tasks = self._priority_tasks(snap, result, today, self._max)
        deadlines = [d for d in result.deadlines if d.status.value == "open" and snap.now <= d.due_at <= snap.now + timedelta(days=7)
                     and d.entity_id is not None and not (result.graph.get(d.entity_id) and result.graph.get(d.entity_id).kind is EntityKind.TASK and d.source.source_type is SourceKind.TASK)]
        soon_events = [e for e in result.graph.of_kind(*PREP_WORDS) if e.when and snap.now <= e.when <= snap.now + timedelta(days=7)]
        soon_events.sort(key=lambda e: e.when)  # type: ignore[arg-type,return-value]
        important_emails = [m for m in snap.emails if m.action_requested]
        attention = [f for f in findings if f.kind in ATTENTION_KINDS][: self._max]

        sections: list[tuple[str, str, list[Statement]]] = []
        sections.append(("TODAY", cal_text, cal_stmts))
        if tasks:
            sections.append(("PRIORITIES", "Top priorities: " + join_and([f"{quoted(t.title)}" + (f" due {when_phrase(t.due_at, snap.now, self._zone, self._all_day(t.due_at))}" if t.due_at else "") for t in tasks]) + ".",
                             [self._task_statement(t, snap) for t in tasks]))
        else:
            sections.append(("PRIORITIES", "You have no open tasks that are due or marked high priority.", []))
        if deadlines:
            sections.append(("DEADLINES", "Deadlines ahead: " + join_and([f"{quoted((d.title or d.original_text)[:60])} {when_phrase(d.due_at, snap.now, self._zone, d.all_day)}" for d in deadlines[:self._max]]) + ".",
                             [extracted(f"{d.source.phrase.capitalize()} mentions a {d.kind.value.replace('_', ' ')} {when_phrase(d.due_at, snap.now, self._zone, d.all_day)}.", d.source) for d in deadlines[:self._max]]))
        important = []
        if important_emails:
            important.append(f"{len(important_emails)} recent email{'s' if len(important_emails) != 1 else ''} appear{'s' if len(important_emails) == 1 else ''} to ask you to do something: "
                             + join_and([quoted(m.subject, 50) for m in important_emails[:3]]) + ".")
        for note in (important_notifications or [])[:3]:
            important.append(note)
        if important:
            sections.append(("IMPORTANT", " ".join(important), []))
        if soon_events:
            sections.append(("EVENTS", "Coming up: " + join_and([f"{quoted(e.name)} {when_phrase(e.when, snap.now, self._zone, e.all_day)}" for e in soon_events[:self._max]]) + ".",  # type: ignore[arg-type]
                             [fact(f"{quoted(e.name)} is on {when_phrase(e.when, snap.now, self._zone, e.all_day)}.", *e.provenance[:1]) for e in soon_events[:self._max]]))  # type: ignore[arg-type]
        if attention:
            sections.append(("ATTENTION", " ".join(f.spoken(with_suggestion=False) for f in attention), [s for f in attention for s in f.facts]))
        notes = source_notes(snap)

        short = ["Good morning."]
        if snap.states.get("calendar") is SourceState.OK:
            if events:
                first = events[0]
                short.append(f"Today you have {len(events)} calendar event{'s' if len(events) != 1 else ''}; the first is {quoted(first.title)} "
                             f"{'all day' if first.all_day else 'at ' + clock(first.start, self._zone)}.")
            else:
                short.append("Your calendar shows nothing scheduled today.")
        else:
            short.append(cal_text)
        if tasks:
            short.append(f"Your top priority is {quoted(tasks[0].title)}" + (f", due {when_phrase(tasks[0].due_at, snap.now, self._zone, self._all_day(tasks[0].due_at))}." if tasks[0].due_at else "."))
        if deadlines:
            short.append(f"There {'is' if len(deadlines) == 1 else 'are'} {len(deadlines)} deadline{'s' if len(deadlines) != 1 else ''} in the next week.")
        if attention:
            short.append(f"{len(attention)} thing{'s need' if len(attention) != 1 else ' needs'} your attention.")
        short.extend(notes[:1])
        short.append("Say 'tell me more' for details.")
        detail = "Here are the details. " + " ".join(f"{name.title()}: {text}" for name, text, _ in sections) + (" " + " ".join(notes) if notes else "")
        stmts = tuple(s for _, _, ss in sections for s in ss)
        return Answer(" ".join(short), stmts, detail=detail, subject="your morning briefing")

    # ---- evening ---------------------------------------------------------------------------------------------------------

    def evening(self, snap: Snapshot, result: ContextResult, findings: list[Finding]) -> Answer:
        today = snap.now.astimezone(self._zone).date()
        tomorrow = today + timedelta(days=1)
        done = [t for t in snap.tasks if t.completed_at and t.completed_at.astimezone(self._zone).date() == today]
        still_open = [t for t in snap.tasks if t.is_open and t.due_at is not None and t.due_at.astimezone(self._zone).date() <= today]
        tomorrow_events = self._day_events(snap, tomorrow)
        upcoming = [t for t in snap.tasks if t.is_open and t.due_at is not None and tomorrow < t.due_at.astimezone(self._zone).date() <= today + timedelta(days=3)]
        unresolved = [f for f in findings if f.kind in ATTENTION_KINDS][: self._max]
        parts, stmts = ["Here's your evening review."], []
        if snap.states.get("tasks") is SourceState.OK:
            parts.append(f"You completed {len(done)} task{'s' if len(done) != 1 else ''} today" + (": " + join_and([quoted(t.title) for t in done[:self._max]]) + "." if done else "."))
            stmts += [fact(f"You completed {quoted(t.title)} today.", *task_prov(t, snap)) for t in done[:self._max]]
            if still_open:
                parts.append(f"Still open from today or earlier: {join_and([quoted(t.title) for t in still_open[:self._max]])}.")
                stmts += [self._task_statement(t, snap) for t in still_open[:self._max]]
            else:
                parts.append("Nothing that was due today is still open.")
        else:
            parts.append("I couldn't read your tasks just now.")
        cal_text, cal_stmts = self._calendar_line(snap, tomorrow, tomorrow_events)
        parts.append(cal_text.replace("Your calendar shows", "For tomorrow, your calendar shows", 1) if tomorrow_events else cal_text)
        stmts += cal_stmts
        if upcoming:
            parts.append("Approaching deadlines: " + join_and([f"{quoted(t.title)} {when_phrase(t.due_at, snap.now, self._zone, self._all_day(t.due_at))}" for t in upcoming[:self._max]]) + ".")
        if unresolved:
            parts.append("Unresolved: " + " ".join(f.spoken(with_suggestion=False) for f in unresolved[:3]))
            stmts += [s for f in unresolved[:3] for s in f.facts]
        parts.extend(source_notes(snap)[:1])
        return Answer(" ".join(parts), tuple(stmts), subject="your evening review")

    # ---- focus -----------------------------------------------------------------------------------------------------------

    def focus(self, snap: Snapshot, result: ContextResult, findings: list[Finding]) -> Answer:
        today = snap.now.astimezone(self._zone).date()
        events = self._day_events(snap, today)
        cal_text, cal_stmts = self._calendar_line(snap, today, events)
        tasks = self._priority_tasks(snap, result, today, 3)
        facts_text, stmts = [cal_text], list(cal_stmts)
        for t in tasks:
            s = self._task_statement(t, snap)
            due = f" is due {when_phrase(t.due_at, snap.now, self._zone, self._all_day(t.due_at))} and" if t.due_at else ""
            facts_text.append(f"Your task {quoted(t.title)}{due} is currently {status_word(t.status)}.")
            stmts.append(s)
        near = [f for f in findings if f.kind in (FindingKind.CALENDAR_OVERLAP, FindingKind.BLOCKED_TASK, FindingKind.PREPARATION_NEEDED, FindingKind.MEMORY_CONFLICT, FindingKind.SOURCE_CONFLICT)][:2]
        for f in near:
            facts_text.append(f.spoken(with_suggestion=False))
            stmts.extend(f.facts)
        if not tasks:
            facts_text.append("You have no open tasks that are due or marked high priority.")
        sugg = []
        if tasks:
            slot = self._planner.next_free_slot(snap, result, tasks[0].estimate_minutes or 60, days_ahead=0)
            first = f"You may want to start with {quoted(tasks[0].title)}."
            sugg.append(suggestion(first, *task_prov(tasks[0], snap)))
            if slot is not None:
                sugg.append(suggestion(f"You have free time from {clock(slot[0], self._zone)} to {clock(slot[1], self._zone)}. I can create a work block if you'd like, or plan your whole day.", *task_prov(tasks[0], snap)))
        text = " ".join(facts_text + [s.text for s in sugg] + source_notes(snap)[:1])
        return Answer(text, tuple(stmts + sugg), tuple(f"task:{t.task_id}" for t in tasks), subject="what to focus on today")

    # ---- important on a day --------------------------------------------------------------------------------------------

    def important(self, snap: Snapshot, result: ContextResult, findings: list[Finding], day: date) -> Answer:
        label = day_word(day, snap.now, self._zone)
        events = self._day_events(snap, day)
        cal_text, stmts = self._calendar_line(snap, day, events)
        parts = [cal_text]
        entity_ids: list[str] = []
        due = [t for t in snap.tasks if t.is_open and t.due_at is not None and t.due_at.astimezone(self._zone).date() == day]
        merged: set[str] = set()
        for t in due:
            parts.append(f"Your task {quoted(t.title)} is due {when_phrase(t.due_at, snap.now, self._zone, self._all_day(t.due_at))} and is currently {status_word(t.status)}.")
            stmts.append(self._task_statement(t, snap))
            entity_ids.append(f"task:{t.task_id}")
            for d in result.deadlines:  # the same task asked for by another source: say so in one place instead of listing it twice
                if d.entity_id == f"task:{t.task_id}" and d.source.source_type is not SourceKind.TASK and d.status.value == "open":
                    text = f"{d.source.phrase.capitalize()} also asks for it {when_phrase(d.due_at, snap.now, self._zone, d.all_day)}."
                    parts.append(text)
                    stmts.append(extracted(text, d.source))
                    merged.add(d.deadline_id)
        for d in result.deadlines:
            if d.deadline_id in merged or d.due_at.astimezone(self._zone).date() != day or d.source.source_type is SourceKind.TASK or d.status.value != "open" or (d.entity_id or "").startswith("cand-task:"):
                continue
            text = f"{d.source.phrase.capitalize()} mentions a {d.kind.value.replace('_', ' ')} {when_phrase(d.due_at, snap.now, self._zone, d.all_day)}: {quoted((d.title or d.original_text)[:80])}."
            parts.append(text)
            stmts.append(extracted(text, d.source))
        related = [f for f in findings if f.when is not None and f.when.astimezone(self._zone).date() == day
                   and f.kind in (FindingKind.PREPARATION_NEEDED, FindingKind.CALENDAR_OVERLAP, FindingKind.EMAIL_EVENT_NOT_ON_CALENDAR, FindingKind.MEMORY_CONFLICT, FindingKind.SOURCE_CONFLICT, FindingKind.TASK_PROPOSAL)]
        for f in related[:3]:
            parts.append(f.spoken())
            stmts.extend(f.facts)
            entity_ids.extend(f.entity_ids)
        if len(parts) == 1 and not events:
            parts.append(f"I don't see any task, deadline or event that needs attention {label}.")
        parts.extend(source_notes(snap)[:1])
        return Answer(" ".join(parts), tuple(stmts), tuple(entity_ids), subject=f"what's important {label}")
