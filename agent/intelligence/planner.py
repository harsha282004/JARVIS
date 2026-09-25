"""DayPlanner: proposes how to spend a working day, from what is actually on the calendar and task list.

It only PROPOSES. Producing a plan changes nothing; adding it to the calendar is a separate step that needs the user's confirmation
(see plan_executor.py). The algorithm is deterministic and every choice can be explained:

  1. Busy time = calendar events that block time, saved events that are not on the calendar, and events an email mentions that are not
     on the calendar yet (kept free so the plan does not double-book them), each with a small buffer.
  2. Free time = the configured working day minus busy time (today: from now on). Slots shorter than 25 minutes are ignored.
  3. Tasks: open tasks that are READY (a task blocked by an unfinished dependency is not planned, and the plan says why).
     Order: overdue, due today, due soon, then by priority; a task tied to an upcoming event ranks higher; ties by due date, then title.
  4. Placement: first fit into the earliest free time, at most 2 hours at a stretch (longer tasks are split into parts), never after
     the task's own due time. A task with no estimate is planned with the configured default and the plan says it assumed that.
  5. Anything that did not fit is listed with the reason. Nothing is dropped silently.
"""

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta

from agent.intelligence.context_engine import ContextResult
from agent.intelligence.dependencies import DepStatus, DependencyStore
from agent.intelligence.models import EVENT_LIKE, EntityKind, Provenance, RelationKind, Snapshot, SourceKind, Statement, fact, inference, stable_id
from agent.intelligence.phrasing import clock, day_word, join_and, quoted
from agent.memory.models import Confidence

MIN_SLOT = timedelta(minutes=25)
MAX_BLOCK = timedelta(minutes=120)
SLOT_GAP = timedelta(minutes=5)
PRIORITY_WORDS = {1: "low", 2: "medium", 3: "high", 4: "critical"}


@dataclass(frozen=True)
class PlanBlock:
    start: datetime
    end: datetime
    title: str
    task_id: str | None
    reason: tuple[Statement, ...]
    part: int = 1
    parts: int = 1

    @property
    def minutes(self) -> int:
        return int((self.end - self.start).total_seconds() // 60)


@dataclass(frozen=True)
class FixedItem:
    start: datetime
    end: datetime
    title: str
    all_day: bool
    provenance: Provenance
    confirmed: bool = True  # False: only an email mentions it


@dataclass
class PlanProposal:
    plan_id: str
    day: date
    blocks: list[PlanBlock]
    fixed: list[FixedItem]
    unscheduled: list[tuple[str, str]]  # (task title, reason)
    notes: list[str]
    created_at: datetime
    day_start: datetime
    day_end: datetime
    status: str = "proposed"  # proposed -> (confirmation) -> added; never "added" without a verified calendar result


class DayPlanner:
    def __init__(self, zone, workday_start: time, workday_end: time, default_minutes: int = 60, buffer_minutes: int = 10):
        self._zone = zone
        self._ws, self._we = workday_start, workday_end
        self._default = timedelta(minutes=default_minutes)
        self._buffer = timedelta(minutes=buffer_minutes)

    def configure(self, workday_start: time, workday_end: time) -> None:
        self._ws, self._we = workday_start, workday_end

    # ---- free time ---------------------------------------------------------------------------------------------------

    def _day_bounds(self, day: date) -> tuple[datetime, datetime]:
        return (datetime.combine(day, self._ws, tzinfo=self._zone), datetime.combine(day, self._we, tzinfo=self._zone))

    def busy(self, snap: Snapshot, result: ContextResult | None, start: datetime, end: datetime) -> tuple[list[tuple[datetime, datetime]], list[FixedItem]]:
        intervals: list[tuple[datetime, datetime]] = []
        fixed: list[FixedItem] = []
        for c in snap.calendar:
            prov = Provenance(SourceKind.CALENDAR, f"{c.calendar_id}/{c.event_id}", f"calendar event {quoted(c.title)}", None, snap.now, Confidence.HIGH)
            if c.end <= start or c.start >= end:
                continue
            fixed.append(FixedItem(c.start, c.end, c.title, c.all_day, prov))
            if c.blocks_time and not c.all_day:
                intervals.append((c.start, c.end))
        if result is not None:
            for e in result.graph.entities.values():
                if e.kind not in EVENT_LIKE or e.when is None or e.attributes.get("on_calendar") or e.attributes.get("flagged") or e.all_day:
                    continue
                if not (start <= e.when < end) or e.confidence < Confidence.MEDIUM:
                    continue
                stop = e.until or e.when + timedelta(hours=1)
                fixed.append(FixedItem(e.when, stop, e.name, False, e.provenance[0], confirmed=False))
                intervals.append((e.when, stop))
        return intervals, sorted(fixed, key=lambda f: f.start)

    def free_slots(self, snap: Snapshot, result: ContextResult | None, day: date, *, from_time: datetime | None = None) -> list[tuple[datetime, datetime]]:
        start, end = self._day_bounds(day)
        if from_time is not None and from_time > start:
            start = self._round_up(from_time)
        if end - start < MIN_SLOT:
            return []
        intervals, _ = self.busy(snap, result, start - timedelta(hours=1), end + timedelta(hours=1))
        expanded = sorted((a - self._buffer, b + self._buffer) for a, b in intervals)
        slots, cursor = [], start
        for a, b in expanded:
            if b <= cursor:
                continue
            if a > cursor:
                slots.append((cursor, min(a, end)))
            cursor = max(cursor, b)
            if cursor >= end:
                break
        if cursor < end:
            slots.append((cursor, end))
        return [(a, b) for a, b in slots if b - a >= MIN_SLOT]

    @staticmethod
    def _round_up(moment: datetime, step: int = 15) -> datetime:
        moment = moment.replace(second=0, microsecond=0)
        extra = (-moment.minute) % step
        return moment + timedelta(minutes=extra)

    def next_free_slot(self, snap: Snapshot, result: ContextResult | None, minutes: int, days_ahead: int = 3) -> tuple[datetime, datetime] | None:
        """The earliest slot of at least min(minutes, 2h) in the next few working days (used to offer a work block)."""
        need = timedelta(minutes=min(max(minutes, 30), 120))
        today = snap.now.astimezone(self._zone).date()
        for offset in range(days_ahead + 1):
            day = today + timedelta(days=offset)
            if day.weekday() >= 5:
                continue
            for a, b in self.free_slots(snap, result, day, from_time=snap.now if offset == 0 else None):
                if b - a >= need:
                    return a, a + need
        return None

    # ---- planning ----------------------------------------------------------------------------------------------------

    def plan(self, snap: Snapshot, result: ContextResult, day: date, deps: DependencyStore | None = None) -> PlanProposal:
        today = snap.now.astimezone(self._zone).date()
        start, end = self._day_bounds(day)
        slots = self.free_slots(snap, result, day, from_time=snap.now if day == today else None)
        _, fixed = self.busy(snap, result, start, end)
        notes: list[str] = []
        by_id = {t.task_id: t for t in snap.tasks}
        candidates, unscheduled = [], []
        for t in snap.tasks:
            if not t.is_open:
                continue
            if deps is not None and deps.status_for(t, by_id) is DepStatus.BLOCKED:
                blockers = deps.blockers(t, by_id)
                unscheduled.append((t.title, f"blocked until {join_and([quoted(b.title) for b in blockers])} is finished"))
                continue
            score, why = self._score(t, snap, result, day)
            if score <= 0 and t.due_at is None and t.priority < 3:
                continue  # not due and not important: not part of a day plan
            candidates.append((score, t, why))
        candidates.sort(key=lambda x: (-x[0], x[1].due_at or datetime.max.replace(tzinfo=self._zone), x[1].title))

        blocks: list[PlanBlock] = []
        free = [list(s) for s in slots]
        for score, task, why in candidates:
            need = timedelta(minutes=task.estimate_minutes) if task.estimate_minutes else self._default
            assumed = task.estimate_minutes is None
            limit = task.due_at if task.due_at is not None and not self._all_day(task.due_at) and task.due_at.astimezone(self._zone).date() == day else None
            pieces: list[tuple[datetime, datetime]] = []
            remaining = need
            for slot in free:
                if remaining <= timedelta(0):
                    break
                a, b = slot
                if limit is not None:
                    b = min(b, limit)
                if b - a < MIN_SLOT:
                    continue
                take = min(remaining, b - a, MAX_BLOCK)
                pieces.append((a, a + take))
                slot[0] = a + take + SLOT_GAP
                remaining -= take
            if remaining > timedelta(0) and not pieces:
                if limit is not None and all(min(b, limit) - a < MIN_SLOT for a, b in slots):
                    reason = f"it is due at {clock(limit, self._zone)}, before there is any free time that day, so it would have to be done earlier"
                else:
                    reason = f"there is no free time left in your working day ({self._ws.strftime('%H:%M')}-{self._we.strftime('%H:%M')}) {day_word(day, snap.now, self._zone)}"
                unscheduled.append((task.title, reason))
                continue
            if remaining > timedelta(0):
                unscheduled.append((f"{task.title} (part)", f"only {int((need - remaining).total_seconds() // 60)} of {int(need.total_seconds() // 60)} minutes fit"))
            for i, (a, b) in enumerate(pieces, start=1):
                reason = list(why)
                if assumed:
                    reason.append(inference(f"You gave no estimate, so I assumed {int(self._default.total_seconds() // 60)} minutes.", *task_prov(task, snap)))
                blocks.append(PlanBlock(a, b, task.title, task.task_id, tuple(reason), i, len(pieces)))
        blocks.sort(key=lambda b: b.start)
        if not slots:
            notes.append("There is no free time in your working day" + (" left today." if day == today else "."))
        pid = stable_id(day.isoformat(), *[f"{b.task_id}{b.start.isoformat()}{b.end.isoformat()}" for b in blocks])
        return PlanProposal(pid, day, blocks, fixed, unscheduled, notes, snap.now, start, end)

    def _score(self, t, snap: Snapshot, result: ContextResult, day: date) -> tuple[int, list[Statement]]:
        prov = task_prov(t, snap)
        score, why = 0, []
        due = t.due_at
        if due is not None:
            local_due = due.astimezone(self._zone)
            days = (local_due.date() - day).days
            if due < snap.now:
                score += 100
                why.append(fact(f"It was due {day_word(due, snap.now, self._zone)} and is still {t.status.replace('_', ' ')}.", *prov))
            elif days <= 0:
                score += 90
                why.append(fact(f"It is due {day_word(due, snap.now, self._zone)}.", *prov))
            elif days == 1:
                score += 70
                why.append(fact(f"It is due {day_word(due, snap.now, self._zone)}.", *prov))
            elif days <= 3:
                score += 50
                why.append(fact(f"It is due {day_word(due, snap.now, self._zone)}.", *prov))
            elif days <= 7:
                score += 30
                why.append(fact(f"It is due {day_word(due, snap.now, self._zone)}.", *prov))
        score += t.priority * 8
        if t.priority >= 3:
            why.append(fact(f"You marked it {PRIORITY_WORDS.get(t.priority, 'high')} priority.", *prov))
        if t.status == "in_progress":
            score += 10
            why.append(fact("You've already started it.", *prov))
        graph = result.graph
        entity = graph.get(f"task:{t.task_id}")
        if entity is not None:
            for rel, other in graph.related(entity.entity_id):
                if other.kind in EVENT_LIKE and other.when is not None and snap.now <= other.when <= snap.now + timedelta(days=3) and rel.kind is RelationKind.RELATES_TO:
                    score += 25
                    why.append(inference(f"It appears related to {quoted(other.name)} {day_word(other.when, snap.now, self._zone)}.", *prov, *other.provenance[:1]))
                    break
        return score, why

    def _all_day(self, due: datetime) -> bool:
        local = due.astimezone(self._zone)
        return local.hour == 23 and local.minute == 59

    # ---- wording -----------------------------------------------------------------------------------------------------

    def describe(self, plan: PlanProposal, now: datetime) -> str:
        head = f"Here's a proposed plan for {day_word(plan.day, now, self._zone)}. I haven't changed your calendar."
        lines: list[tuple[datetime, str]] = []
        for b in plan.blocks:
            part = f" (part {b.part} of {b.parts})" if b.parts > 1 else ""
            lines.append((b.start, f"{clock(b.start, self._zone)} to {clock(b.end, self._zone)}: {b.title}{part}."))
        for f in plan.fixed:
            when = "All day" if f.all_day else clock(f.start, self._zone)
            note = "" if f.confirmed else " (mentioned in an email, not on your calendar)"
            lines.append((f.start, f"{when}: {f.title}{note}."))
        body = " ".join(text for _, text in sorted(lines, key=lambda x: x[0]))
        tail = []
        if plan.unscheduled:
            tail.append("I couldn't fit: " + "; ".join(f"{quoted(t)} ({why})" for t, why in plan.unscheduled) + ".")
        tail.extend(plan.notes)
        if plan.blocks:
            tail.append("Say 'add it to my calendar' if you'd like me to create these work blocks, or ask why I chose something.")
        elif not plan.unscheduled and not plan.notes:
            tail.append("I don't see any open task that needs time that day.")
        return " ".join([head, body, *tail]).strip()


def task_prov(t, snap: Snapshot) -> tuple[Provenance, ...]:
    return (Provenance(SourceKind.TASK, t.task_id, f"task {quoted(t.title)}", t.created_at, snap.now, Confidence.HIGH, f"task:{t.task_id}"),)
