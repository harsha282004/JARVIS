"""Cross-source findings: the things worth telling the user, each with facts first, evidence attached, and only then an optional suggestion.

Each rule is deterministic and explains itself with `Statement`s (FACT / EXTRACTED / INFERENCE / SUGGESTION). A finding never changes
anything: an `Offer` describes exactly what JARVIS *could* do (add this event to the calendar, create this task); it is carried out
only after the user confirms that exact action. Conflicts are reported side by side and never resolved silently.

Rules: email event missing from the calendar, calendar overlaps, approaching deadlines with unfinished tasks, several deadlines on
one day, blocked tasks, task due later than an email asks for it, memory/calendar conflicts, reminder collisions, tasks an email
asks for that do not exist yet, preparation needed for an upcoming interview/exam/review, overdue tasks, and suspicious content.
"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import IntEnum, StrEnum

from agent.intelligence.context_engine import ContextResult, DateConflict
from agent.intelligence.dependencies import DepStatus, DependencyStore
from agent.intelligence.models import (
    EVENT_LIKE,
    EntityKind,
    Provenance,
    RelationKind,
    Snapshot,
    SourceKind,
    SourceState,
    Statement,
    extracted,
    fact,
    inference,
    suggestion,
)
from agent.intelligence.phrasing import clock, day_word, join_and, quoted, status_word, when_phrase
from agent.memory.models import Confidence
from backend.core.preferences import PreferenceStore

PREP_KINDS = frozenset({EntityKind.INTERVIEW, EntityKind.EXAM, EntityKind.PROJECT_REVIEW, EntityKind.HACKATHON})


class FindingKind(StrEnum):
    EMAIL_EVENT_NOT_ON_CALENDAR = "email_event_not_on_calendar"
    CALENDAR_OVERLAP = "calendar_overlap"
    DEADLINE_PENDING_TASK = "deadline_pending_task"
    OVERDUE_TASK = "overdue_task"
    MULTIPLE_DEADLINES = "multiple_deadlines"
    BLOCKED_TASK = "blocked_task"
    TASK_LATER_THAN_REQUESTED = "task_later_than_requested"
    MEMORY_CONFLICT = "memory_conflict"
    SOURCE_CONFLICT = "source_conflict"
    REMINDER_COLLISION = "reminder_collision"
    MISSED_REMINDER = "missed_reminder"
    TASK_PROPOSAL = "task_proposal"
    PREPARATION_NEEDED = "preparation_needed"
    SUSPICIOUS_CONTENT = "suspicious_content"


class Urgency(IntEnum):
    INFO = 1
    NOTICE = 2
    IMPORTANT = 3
    CRITICAL = 4


@dataclass(frozen=True)
class Offer:
    """Something JARVIS could do if the user says yes. `prompt` names the exact action."""

    kind: str  # "calendar_event" | "create_task"
    title: str
    start: datetime | None
    end: datetime | None
    all_day: bool
    prompt: str
    proposal_id: str | None = None


@dataclass(frozen=True)
class Finding:
    key: str  # stable identity for de-duplication ("same finding again" is the same key)
    kind: FindingKind
    urgency: Urgency
    category: str  # notification category: deadline | calendar | task | email | conflict | reminder | preparation | security
    title: str
    facts: tuple[Statement, ...]
    suggestion: Statement | None = None
    offer: Offer | None = None
    when: datetime | None = None
    entity_ids: tuple[str, ...] = ()

    def spoken(self, with_suggestion: bool = True) -> str:
        parts = [s.text for s in self.facts]
        if with_suggestion and self.suggestion is not None:
            parts.append(self.suggestion.text)
        return " ".join(parts)

    def evidence(self) -> list[Provenance]:
        seen, out = set(), []
        for s in (*self.facts, *([self.suggestion] if self.suggestion else [])):
            for p in s.provenance:
                if (p.source_type, p.source_id) not in seen:
                    seen.add((p.source_type, p.source_id))
                    out.append(p)
        return out


BlockFinder = Callable[[str, int], tuple[datetime, datetime] | None]


class FindingsEngine:
    def __init__(self, zone, prefs: PreferenceStore | None = None, deps: DependencyStore | None = None, find_block: BlockFinder | None = None,
                 lookahead_days: int = 7):
        self._zone = zone
        self._prefs = prefs
        self._deps = deps
        self._find_block = find_block
        self._lookahead = timedelta(days=lookahead_days)

    def evaluate(self, snap: Snapshot, result: ContextResult, find_block: BlockFinder | None = None) -> list[Finding]:
        if find_block is not None:
            self._find_block = find_block
        findings: list[Finding] = []
        for rule in (self._calendar_gaps, self._overlaps, self._deadline_tasks, self._multiple_deadlines, self._blocked, self._task_later,
                     self._conflicts, self._reminders, self._proposals, self._preparation, self._suspicious):
            findings.extend(rule(snap, result))
        findings.sort(key=lambda f: (-int(f.urgency), f.when is None, f.when or snap.now, f.key))
        return findings

    # ---- rules -------------------------------------------------------------------------------------------------------------

    def _calendar_gaps(self, snap: Snapshot, result: ContextResult) -> list[Finding]:
        out: list[Finding] = []
        for e in result.graph.entities.values():
            if e.kind not in EVENT_LIKE or not e.attributes.get("candidate") and not e.attributes.get("mentioned_by"):
                continue
            if e.attributes.get("on_calendar") or e.when is None or e.when < snap.now or e.when > snap.now + timedelta(days=30):
                continue
            if e.attributes.get("flagged"):
                continue
            mention = e.provenance[0]
            when = when_phrase(e.when, snap.now, self._zone, e.all_day)
            what = quoted(e.name)
            state = snap.states.get("calendar")
            source_phrase = mention.phrase
            if state is SourceState.OK:
                facts = (extracted(f"{source_phrase.capitalize()} mentions {what} {when}, but I don't see a matching calendar event.", mention),)
                offer = Offer("calendar_event", e.name, e.when, e.when + timedelta(hours=1) if not e.all_day else None, e.all_day,
                              f"Would you like me to add {what} on {when} to your calendar?")
                out.append(Finding(f"gap:{e.entity_id}", FindingKind.EMAIL_EVENT_NOT_ON_CALENDAR, Urgency.IMPORTANT if e.kind in PREP_KINDS else Urgency.NOTICE,
                                   "calendar", f"{e.name} is not on your calendar", facts, suggestion(offer.prompt, mention), offer, e.when, (e.entity_id,)))
            elif state is SourceState.UNAVAILABLE:
                out.append(Finding(f"gap-unchecked:{e.entity_id}", FindingKind.EMAIL_EVENT_NOT_ON_CALENDAR, Urgency.INFO, "calendar", f"{e.name}: calendar not checked",
                                   (extracted(f"{source_phrase.capitalize()} mentions {what} {when}, but I couldn't check your calendar just now.", mention),), None, None, e.when, (e.entity_id,)))
        return out

    def _overlaps(self, snap: Snapshot, result: ContextResult) -> list[Finding]:
        items = sorted((c for c in snap.calendar if c.blocks_time and not c.all_day and snap.now - timedelta(hours=1) <= c.end and c.start <= snap.now + self._lookahead),
                       key=lambda c: c.start)
        out: list[Finding] = []
        seen: set[frozenset[str]] = set()
        for i, a in enumerate(items):
            for b in items[i + 1:]:
                if b.start >= a.end:
                    break
                pair = frozenset({a.event_id, b.event_id})
                if pair in seen:
                    continue
                seen.add(pair)
                prov = (Provenance(SourceKind.CALENDAR, f"{a.calendar_id}/{a.event_id}", f"calendar event {quoted(a.title)}", None, snap.now, Confidence.HIGH),
                        Provenance(SourceKind.CALENDAR, f"{b.calendar_id}/{b.event_id}", f"calendar event {quoted(b.title)}", None, snap.now, Confidence.HIGH))
                if a.start == b.start:
                    text = f"You have two calendar events scheduled for {when_phrase(a.start, snap.now, self._zone)}: {quoted(a.title)} and {quoted(b.title)}."
                else:
                    text = (f"Your calendar shows {quoted(a.title)} ({clock(a.start, self._zone)} to {clock(a.end, self._zone)}) overlapping "
                            f"{quoted(b.title)} ({clock(b.start, self._zone)} to {clock(b.end, self._zone)}) {day_word(b.start, snap.now, self._zone)}.")
                urgency = Urgency.IMPORTANT if b.start - snap.now <= timedelta(hours=48) else Urgency.NOTICE
                out.append(Finding(f"overlap:{min(pair)}:{max(pair)}", FindingKind.CALENDAR_OVERLAP, urgency, "conflict", "Calendar overlap",
                                   (fact(text, *prov),), None, None, b.start, ()))
        return out

    def _deadline_tasks(self, snap: Snapshot, result: ContextResult) -> list[Finding]:
        out: list[Finding] = []
        for t in snap.tasks:
            if not t.is_open or t.due_at is None:
                continue
            prov = Provenance(SourceKind.TASK, t.task_id, f"task {quoted(t.title)}", t.created_at, snap.now, Confidence.HIGH, f"task:{t.task_id}")
            hours = (t.due_at - snap.now).total_seconds() / 3600
            if hours < 0:
                out.append(Finding(f"overdue:{t.task_id}", FindingKind.OVERDUE_TASK, Urgency.IMPORTANT if hours > -48 else Urgency.NOTICE, "task", f"{t.title} is overdue",
                                   (fact(f"Your task {quoted(t.title)} was due {when_phrase(t.due_at, snap.now, self._zone, self._all_day(t.due_at))} and is still {status_word(t.status)}.", prov),),
                                   None, None, t.due_at, (f"task:{t.task_id}",)))
                continue
            lead = self._prefs.reminder_lead_for(t.title, "deadline") if self._prefs else None
            window_hours = max(72.0, (lead or 0) / 60)
            if hours > window_hours:
                continue
            urgency = Urgency.CRITICAL if hours <= 3 else Urgency.IMPORTANT if hours <= 24 or (lead is not None and hours * 60 <= lead) else Urgency.NOTICE
            text = f"Your task {quoted(t.title)} is due {when_phrase(t.due_at, snap.now, self._zone, self._all_day(t.due_at))} and is currently {status_word(t.status)}."
            sug = None
            if self._find_block is not None and t.status == "pending":
                slot = self._find_block(t.title, t.estimate_minutes or 120)
                if slot is not None:
                    sug = suggestion(f"You have free time {day_word(slot[0], snap.now, self._zone)} from {clock(slot[0], self._zone)} to {clock(slot[1], self._zone)}. I can create a work block for it if you'd like.", prov)
            out.append(Finding(f"deadline:{t.task_id}:{t.due_at.date().isoformat()}", FindingKind.DEADLINE_PENDING_TASK, urgency, "deadline",
                               f"{t.title} is due {day_word(t.due_at, snap.now, self._zone)}", (fact(text, prov),), sug, None, t.due_at, (f"task:{t.task_id}",)))
        return out

    def _multiple_deadlines(self, snap: Snapshot, result: ContextResult) -> list[Finding]:
        by_day: dict = {}
        for t in snap.tasks:
            if t.is_open and t.due_at is not None and snap.now <= t.due_at <= snap.now + self._lookahead:
                by_day.setdefault(t.due_at.astimezone(self._zone).date(), []).append(t)
        out = []
        for day, tasks in sorted(by_day.items()):
            if len(tasks) < 2:
                continue
            prov = tuple(Provenance(SourceKind.TASK, t.task_id, f"task {quoted(t.title)}", t.created_at, snap.now, Confidence.HIGH) for t in tasks)
            names = join_and([quoted(t.title) for t in tasks])
            out.append(Finding(f"multi-deadline:{day.isoformat()}", FindingKind.MULTIPLE_DEADLINES, Urgency.IMPORTANT if (day - snap.now.astimezone(self._zone).date()).days <= 2 else Urgency.NOTICE,
                               "deadline", f"{len(tasks)} deadlines {day_word(day, snap.now, self._zone)}",
                               (fact(f"You have {len(tasks)} deadlines {day_word(day, snap.now, self._zone)}: {names}.", *prov),), None, None, tasks[0].due_at,
                               tuple(f"task:{t.task_id}" for t in tasks)))
        return out

    def _blocked(self, snap: Snapshot, result: ContextResult) -> list[Finding]:
        if self._deps is None:
            return []
        by_id = {t.task_id: t for t in snap.tasks}
        out = []
        for t in snap.tasks:
            if not t.is_open or self._deps.status_for(t, by_id) is not DepStatus.BLOCKED:
                continue
            blockers = self._deps.blockers(t, by_id)
            due = f" and is due {when_phrase(t.due_at, snap.now, self._zone, self._all_day(t.due_at))}" if t.due_at else ""
            soon = t.due_at is not None and t.due_at - snap.now <= timedelta(days=3)
            prov = (Provenance(SourceKind.TASK, t.task_id, f"task {quoted(t.title)}", t.created_at, snap.now, Confidence.HIGH),
                    *(Provenance(SourceKind.TASK, b.task_id, f"task {quoted(b.title)}", b.created_at, snap.now, Confidence.HIGH) for b in blockers))
            out.append(Finding(f"blocked:{t.task_id}", FindingKind.BLOCKED_TASK, Urgency.IMPORTANT if soon else Urgency.INFO, "task", f"{t.title} is blocked",
                               (fact(f"Your task {quoted(t.title)} is blocked: it depends on {join_and([quoted(b.title) for b in blockers])}, which "
                                     f"{'isn' + chr(39) + 't' if len(blockers) == 1 else 'aren' + chr(39) + 't'} finished{due}.", *prov),),
                               None, None, t.due_at, (f"task:{t.task_id}",)))
        return out

    def _task_later(self, snap: Snapshot, result: ContextResult) -> list[Finding]:
        out = []
        for dl in result.graph.of_kind(EntityKind.DEADLINE):
            for rel in result.graph.outgoing(dl.entity_id, RelationKind.APPLIES_TO):
                task = result.graph.get(rel.object_id)
                if task is None or task.kind is not EntityKind.TASK or task.when is None or dl.when is None:
                    continue
                if not any(p.source_type is SourceKind.EMAIL for p in dl.provenance) or task.status in ("completed", "cancelled", "candidate"):
                    continue
                if task.when > dl.when + timedelta(minutes=1):
                    email = next(p for p in dl.provenance if p.source_type is SourceKind.EMAIL)
                    tprov = next(iter(task.provenance))
                    out.append(Finding(f"later:{task.entity_id}:{dl.entity_id}", FindingKind.TASK_LATER_THAN_REQUESTED, Urgency.IMPORTANT, "deadline",
                                       f"{task.name} may be late", (
                                           fact(f"Your task {quoted(task.name)} is due {when_phrase(task.when, snap.now, self._zone)}.", tprov),
                                           extracted(f"But an email asks for it {when_phrase(dl.when, snap.now, self._zone, dl.all_day)}.", email)),
                                       None, None, dl.when, (task.entity_id, dl.entity_id)))
        return out

    def _conflicts(self, snap: Snapshot, result: ContextResult) -> list[Finding]:
        out = []
        for c in result.conflicts:
            memory_involved = SourceKind.MEMORY in (c.left.source_type, c.right.source_type)
            if c.right.source_type is SourceKind.MEMORY and c.left.source_type is not SourceKind.MEMORY:
                c = DateConflict(c.title, c.kind, c.right, c.right_when, c.right_all_day, c.left, c.left_when, c.left_all_day)  # memory side first
            l, r = self._side(c, True, snap), self._side(c, False, snap)
            what = quoted(c.title)
            text = (f"I found conflicting information about {what}. {c.left.phrase.capitalize()} says {l}, while {c.right.phrase} shows {r}."
                    if memory_involved else
                    f"I found conflicting information about {what}. {c.left.phrase.capitalize()} says {l}, while {c.right.phrase} says {r}.")
            out.append(Finding(f"conflict:{c.left.source_type.value}:{c.right.source_type.value}:{c.title.lower()}:{c.left_when.date()}:{c.right_when.date()}",
                               FindingKind.MEMORY_CONFLICT if memory_involved else FindingKind.SOURCE_CONFLICT, Urgency.IMPORTANT, "conflict",
                               f"Conflicting information: {c.title}", (fact(text, c.left, c.right),), suggestion("Tell me which one is right and I'll help you fix the other.", c.left), None, min(c.left_when, c.right_when)))
        return out

    def _side(self, c: DateConflict, left: bool, snap: Snapshot) -> str:
        when, all_day = (c.left_when, c.left_all_day) if left else (c.right_when, c.right_all_day)
        return when_phrase(when, snap.now, self._zone, all_day) if c.kind == "time" else day_word(when, snap.now, self._zone)

    def _reminders(self, snap: Snapshot, result: ContextResult) -> list[Finding]:
        out = []
        upcoming = sorted((r for r in snap.reminders if r.status == "scheduled" and r.scheduled_at >= snap.now), key=lambda r: r.scheduled_at)
        buckets: dict[str, list] = {}
        for r in upcoming:
            buckets.setdefault(r.scheduled_at.astimezone(self._zone).strftime("%Y-%m-%d %H:%M"), []).append(r)
        for key, group in buckets.items():
            if len(group) > 1 and group[0].scheduled_at - snap.now <= self._lookahead:
                prov = tuple(Provenance(SourceKind.REMINDER, r.reminder_id, "a reminder", None, snap.now, Confidence.HIGH) for r in group)
                out.append(Finding(f"reminders:{key}", FindingKind.REMINDER_COLLISION, Urgency.INFO, "reminder", "Reminders at the same time",
                                   (fact(f"You have {len(group)} reminders at {when_phrase(group[0].scheduled_at, snap.now, self._zone)}: {join_and([quoted(r.message) for r in group])}.", *prov),),
                                   None, None, group[0].scheduled_at))
        for r in snap.reminders:
            if r.missed and snap.now - r.scheduled_at <= timedelta(days=2):
                out.append(Finding(f"missed-reminder:{r.reminder_id}", FindingKind.MISSED_REMINDER, Urgency.NOTICE, "reminder", "Missed reminder",
                                   (fact(f"A reminder {quoted(r.message)} for {when_phrase(r.scheduled_at, snap.now, self._zone)} was delivered late because JARVIS wasn't running.",
                                         Provenance(SourceKind.REMINDER, r.reminder_id, "a reminder", None, snap.now, Confidence.HIGH)),), None, None, r.scheduled_at))
        return out

    def _proposals(self, snap: Snapshot, result: ContextResult) -> list[Finding]:
        out = []
        for p in result.proposals:
            if p.flagged or p.confidence < Confidence.MEDIUM:
                continue
            due = f" {when_phrase(p.due_at, snap.now, self._zone, p.all_day)}" if p.due_at else ""
            prompt = f"Would you like me to create a task '{p.title}'" + (f" due {when_phrase(p.due_at, snap.now, self._zone, p.all_day)}" if p.due_at else "") + "?"
            urgent = p.due_at is not None and p.due_at - snap.now <= timedelta(days=2)
            out.append(Finding(f"proposal:{p.proposal_id}", FindingKind.TASK_PROPOSAL, Urgency.IMPORTANT if urgent else Urgency.NOTICE, "email",
                               f"Possible task: {p.title}", (extracted(f"{p.source.phrase.capitalize()} asks you to {p.title[:1].lower() + p.title[1:]}{' by' + due[3:] if due.startswith(' by') else (' ' + due.strip() if due else '')}.", p.source),),
                               suggestion(prompt, p.source), Offer("create_task", p.title, p.due_at, None, p.all_day, prompt, p.proposal_id), p.due_at))
        return out

    def _preparation(self, snap: Snapshot, result: ContextResult) -> list[Finding]:
        out = []
        graph = result.graph
        for e in graph.of_kind(*PREP_KINDS):
            if e.when is None or not (snap.now <= e.when <= snap.now + timedelta(hours=48)) or e.attributes.get("flagged"):
                continue
            related = []
            for rel, other in graph.related(e.entity_id):
                if other.kind is EntityKind.TASK and other.status not in ("completed", "cancelled", "candidate"):
                    related.append(other)
                elif other.kind is EntityKind.DEADLINE:
                    for r2 in graph.outgoing(other.entity_id, RelationKind.APPLIES_TO):
                        t = graph.get(r2.object_id)
                        if t is not None and t.kind is EntityKind.TASK and t.status not in ("completed", "cancelled", "candidate") and t not in related:
                            related.append(t)
            when = when_phrase(e.when, snap.now, self._zone, e.all_day)
            what = quoted(e.name)
            prov = tuple(e.provenance[:1])
            if related:
                names = join_and([f"{quoted(t.name)} ({status_word(t.status or 'pending')})" for t in related])
                facts = (fact(f"You have {what} {when}.", *prov),
                         fact(f"Related task{'s' if len(related) > 1 else ''}: {names}.", *[p for t in related for p in t.provenance[:1]]))
                prep = suggestion("You may want to finish it before then.", *prov)
                sug_block = None
                if self._find_block is not None:
                    slot = self._find_block(related[0].name, 120)
                    if slot is not None:
                        sug_block = suggestion(f"I can create a two-hour work block {day_word(slot[0], snap.now, self._zone)} at {clock(slot[0], self._zone)} if you'd like.", *prov)
                out.append(Finding(f"prep:{e.entity_id}", FindingKind.PREPARATION_NEEDED, Urgency.IMPORTANT, "preparation", f"Prepare for {e.name}", facts, sug_block or prep, None, e.when,
                                   (e.entity_id, *[t.entity_id for t in related])))
            else:
                out.append(Finding(f"prep:{e.entity_id}", FindingKind.PREPARATION_NEEDED, Urgency.NOTICE, "preparation", f"Prepare for {e.name}",
                                   (fact(f"You have {what} {when}.", *prov), inference("I don't see a task related to preparing for it.", *prov)),
                                   suggestion("You may want to add a preparation task.", *prov), None, e.when, (e.entity_id,)))
        return out

    def _suspicious(self, snap: Snapshot, result: ContextResult) -> list[Finding]:
        out = []
        for p in result.flagged_sources:
            out.append(Finding(f"suspicious:{p.source_type.value}:{p.source_id}", FindingKind.SUSPICIOUS_CONTENT, Urgency.NOTICE, "security", "Suspicious content ignored",
                               (fact(f"{p.phrase.capitalize()} ({p.label}) contains text that tries to give me instructions. I treated it as ordinary text, ignored the instructions, and won't act on anything in it.", p),),
                               None, None, None))
        return out

    def _all_day(self, due: datetime) -> bool:
        local = due.astimezone(self._zone)
        return local.hour == 23 and local.minute == 59
