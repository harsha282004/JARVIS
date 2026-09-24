"""Event tools: the only code that turns a validated EventAction into event-service calls.

Same two-part shape as the task and Gmail tools:
  resolve(args) -> Clarify | Ready   parses times (with the Phase 9 parser), identifies the target event/task by
                                     matching words (never a model id) and produces concrete parameters. It may read the
                                     local database; it never calls Gmail, and it changes nothing.
  run(**params) -> str               the change or read, reached only through Tool.execute (after the PermissionManager
                                     authorized exactly these parameters). Gmail/documents/memory are read only here.

Permission policy (also in docs/event-and-deadline-intelligence.md):
  event_list, event_search, event_get     LOW, no approval   read-only, local, bounded
  event_create, event_complete            LOW, no approval   additive/reversible, from the user's own words, target unique
  event_update, event_cancel              MEDIUM, approval   change or end persistent data: the user says "yes" first
  event_extract                           MEDIUM, approval   stores data derived from untrusted text (an email, a document)
Replies may contain stored event text (which can originate in an email or document), so the conversation history keeps
only a placeholder for them: the model never reads attacker-written text as context.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel

from agent.events.conflicts import Conflict
from agent.events.dates import ResolvedWhen, WhenKind, event_times, resolve_when
from agent.events.extraction import infer_type
from agent.events.intents import (
    EventActionName,
    EventCancelArgs,
    EventCompleteArgs,
    EventCreateArgs,
    EventExtractArgs,
    EventGetArgs,
    EventListArgs,
    EventSearchArgs,
    EventUpdateArgs,
)
from agent.events.models import DUE_TYPES, Event, EventSource, EventStatus, EventType, SourceType
from agent.events.graph import EventGraphLinker
from agent.events.service import EventService, SyncOutcome
from agent.events.sources import EventIngestor, IngestResult
from agent.events.temporal import EventScope, countdown
from agent.memory.models import Confidence
from agent.tasks.formatting import format_clock, format_when
from agent.tasks.matching import find_matches
from agent.tasks.models import TaskError, TaskPriority
from agent.tasks.service import TaskService
from agent.tasks.timeparse import TimeParser
from agent.tasks.tools import Clarify, Ready
from agent.tools.base import Tool
from backend.core.security import PermissionScope, RiskLevel
from integrations.gmail.service import GmailService
from integrations.gmail.text import one_line

MAX_SPOKEN = 5
PLACEHOLDER = "[Event information was read to the user. Stored event text is deliberately not kept in the conversation history.]"


@dataclass(frozen=True)
class EventToolContext:
    events: EventService
    parser: TimeParser
    clock: Any  # Callable[[], datetime]
    tasks: TaskService | None = None
    gmail: GmailService | None = None
    rag: Any = None  # RagService | None
    memory: Any = None  # MemoryService | None
    linker: EventGraphLinker | None = None

    def now(self) -> datetime:
        return self.clock()

    @property
    def zone(self):
        return self.events.zone


def _iso(value: datetime | None) -> str | None:
    return value.astimezone(timezone.utc).isoformat() if value else None


def _from_iso(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def when_text(event: Event, now: datetime, zone) -> str:
    if event.is_deadline:
        return "due " + format_when(event.due_at, now, zone, with_time=not event.all_day)  # type: ignore[arg-type]
    if event.all_day:
        return format_when(event.start_at, now, zone, with_time=False) + ", all day"  # type: ignore[arg-type]
    text = format_when(event.start_at, now, zone)  # type: ignore[arg-type]
    if event.end_at is not None:
        local_end = event.end_at.astimezone(zone)
        text += f" until {format_clock(local_end.hour, local_end.minute)}"
    return text


def describe(event: Event, now: datetime, zone) -> str:
    flags = []
    if event.status is EventStatus.UNKNOWN:
        flags.append("unconfirmed")
    elif event.status is EventStatus.MISSED:
        flags.append("overdue" if event.is_deadline else "missed")
    elif event.status is EventStatus.ACTIVE:
        flags.append("happening now")
    if event.priority is not None and event.priority >= TaskPriority.HIGH:
        flags.append(f"{event.priority.name.lower()} priority")
    extra = f", {', '.join(flags)}" if flags else ""
    return f"{one_line(event.title, 80)} ({when_text(event, now, zone)}{extra})"


def _spoken(items: list[str]) -> str:
    shown = items[:MAX_SPOKEN]
    text = "; ".join(shown)
    return text + (f"; and {len(items) - len(shown)} more" if len(items) > len(shown) else "")


def _conflict_note(conflicts: list[Conflict], now: datetime, zone) -> str:
    if not conflicts:
        return ""
    c = conflicts[0]
    kind = "are on the same day (one is all day)" if c.kind.value == "all_day" else "overlap"
    more = f" (and {len(conflicts) - 1} more conflict{'s' if len(conflicts) > 2 else ''})" if len(conflicts) > 1 else ""
    return f" Heads up: {one_line(c.first.title, 50)} and {one_line(c.second.title, 50)} {kind}{more}. I haven't changed anything."


def _candidates(events: list[Event], now: datetime, zone) -> str:
    return (f"I found {len(events)} events that could match: {_spoken([describe(e, now, zone) for e in events])}. "
            "Which one do you mean? Add a word from the title or its date.")


class EventTool(Tool, ABC):
    allowed_scopes = (PermissionScope.ONE_TIME,)
    action: EventActionName
    history_placeholder = PLACEHOLDER

    def __init__(self, context: EventToolContext):
        self._ctx = context
        self.name = self.action.value

    @abstractmethod
    def resolve(self, args: BaseModel) -> Clarify | Ready:
        raise NotImplementedError

    def _identify(self, query: str, **kw) -> Event | Clarify:
        found = self._ctx.events.find_matching(query, **kw)
        if not found:
            return Clarify("I couldn't find an event or deadline matching that.")
        if len(found) > 1:
            return Clarify(_candidates(found, self._ctx.now(), self._ctx.zone))
        return found[0]

    def _resolve_when(self, phrase: str) -> ResolvedWhen | Clarify:
        ctx = self._ctx
        resolved = resolve_when(ctx.parser, phrase, ctx.now())
        if resolved.kind is WhenKind.AMBIGUOUS:
            return Clarify(resolved.question or "Which date do you mean?")
        if resolved.kind is WhenKind.UNRESOLVED or resolved.value is None:
            return Clarify("I couldn't understand that date. Could you say it like 'October 5 at 10 AM' or 'next Monday'?")
        return resolved


# ---- create ---------------------------------------------------------------------------------------------------------


class EventCreateTool(EventTool):
    action = EventActionName.CREATE
    description = "Save an event or deadline the user states: an interview, meeting, exam, assignment, application deadline, ..."
    input_schema = {
        "title": "string: what it is (e.g. 'internship interview'); may be omitted if task_query is given",
        "when": "string: the date/time exactly as the user said it (e.g. 'tomorrow at 10 AM', 'October 5', 'by Friday')",
        "type": "deadline | meeting | interview | exam | assignment | application | appointment | event | other (optional)",
        "duration_minutes": "integer, optional (only for timed events)",
        "priority": "low | medium | high | critical, optional",
        "description": "string, optional",
        "task_query": "string, optional: words identifying an EXISTING task to link (no new task is created)",
        "project": "string, optional: an existing project in the knowledge graph",
        "person": "string, optional: an existing person in the knowledge graph",
    }
    requires_permission = False
    risk = RiskLevel.LOW

    def resolve(self, args: EventCreateArgs) -> Clarify | Ready:
        ctx = self._ctx
        title, task_id, when = args.title, None, None
        due_from_task: datetime | None = None
        if args.task_query:
            if ctx.tasks is None:
                return Clarify("Tasks are turned off, so I can't link a task.")
            found = ctx.tasks.find_matching_tasks(args.task_query)
            if not found:
                return Clarify("I couldn't find an open task matching that.")
            if len(found) > 1:
                return Clarify(f"I found {len(found)} tasks that could match: {_spoken([one_line(t.title, 60) for t in found])}. Which one do you mean?")
            task = found[0]
            task_id, title = task.task_id, title or task.title
            due_from_task = task.due_at
        if args.when:
            when = self._resolve_when(args.when)
            if isinstance(when, Clarify):
                return when
        elif due_from_task is not None:
            when = ResolvedWhen(WhenKind.EXACT, due_from_task.astimezone(ctx.zone))
        else:
            return Clarify("That task has no due date. When is it?")
        event_type = args.type or infer_type(f"{title} {args.when or ''}") or (EventType.DEADLINE if args.task_query else EventType.EVENT)
        times = event_times(when, event_type, args.duration_minutes)
        if (times.due_at or times.end_at or times.start_at) < ctx.now():  # type: ignore[operator]
            return Clarify("That time has already passed. What is the correct date?")
        return Ready({
            "title": title, "event_type": event_type.value, "start_at": _iso(times.start_at), "end_at": _iso(times.end_at),
            "due_at": _iso(times.due_at), "all_day": times.all_day, "description": args.description,
            "priority": args.priority.name if args.priority else None, "task_id": task_id,
            "project": args.project, "person": args.person, "day_part": when.day_part,
        })

    def run(self, *, title: str, event_type: str, start_at: str | None, end_at: str | None, due_at: str | None, all_day: bool,
            description: str | None, priority: str | None, task_id: str | None, project: str | None, person: str | None,
            day_part: str | None, origin_session: str | None = None) -> str:
        ctx, now = self._ctx, self._ctx.now()
        note = f"Said as: {day_part}" if day_part else None
        result = ctx.events.create_event(
            title, EventType(event_type), start_at=_from_iso(start_at), end_at=_from_iso(end_at), due_at=_from_iso(due_at),
            all_day=all_day, description=description or note, priority=TaskPriority[priority] if priority else None,
            source=EventSource(source_type=SourceType.USER_EXPLICIT, source_id=None, reference="you told me"),
            confidence=Confidence.HIGH, task_id=task_id, metadata={"session_id": origin_session, "day_part": day_part},
            revive_closed=True,
        )
        event = result.event
        text = describe(event, now, ctx.zone)
        if not result.created and not result.revived:
            return f"You already have that: {text}."
        notes = ""
        if result.created and ctx.linker is not None and (project or person):
            links = ctx.linker.link(event, project=project, person=person)
            if links.relationship_ids:
                ctx.events.merge_metadata(event.event_id, {"graph_links": links.relationship_ids})
            notes += "".join(f" ({n}.)" for n in links.notes)
        if task_id:
            notes += " It's linked to your task."
            if ctx.events.task_due_state(event) is SyncOutcome.MISMATCH:
                notes += " Note: the task's own due date is different; I left it as it is."
        notes += _conflict_note(ctx.events.conflicts_for(event), now, ctx.zone)
        verb = "added it back" if result.revived else "added"
        return f"Okay, I've {verb}: {text}.{notes}"


# ---- read -------------------------------------------------------------------------------------------------------------


class EventListTool(EventTool):
    action = EventActionName.LIST
    description = "List the user's events and deadlines: upcoming, today, tomorrow, this week, next week, overdue, or everything known."
    input_schema = {
        "scope": "upcoming | today | tomorrow | this_week | next_week | next_7_days | overdue | all (default upcoming)",
        "type": "deadline | meeting | interview | exam | ... optional filter",
        "next": "boolean: only the next one (e.g. 'when is my next interview')",
        "conflicts": "boolean: also say whether the listed events overlap",
    }
    requires_permission = False
    risk = RiskLevel.LOW

    def resolve(self, args: EventListArgs) -> Ready:
        return Ready({"scope": args.scope, "type": args.type.value if args.type else None, "next": args.next,
                      "conflicts": args.conflicts})

    def run(self, *, scope: str, type: str | None, next: bool, conflicts: bool, origin_session: str | None = None) -> str:
        ctx, now = self._ctx, self._ctx.now()
        kind = EventType(type) if type else None
        noun, plural = (kind.value, f"{kind.value}s") if kind else ("event or deadline", "events or deadlines")
        if next:
            event = ctx.events.next_event(kind)
            if event is None:
                return f"You have no upcoming {kind.value if kind else 'event or deadline'} that I know about."
            when = countdown(event, now, ctx.zone)
            return f"Your next {kind.value if kind else 'event or deadline'} is {describe(event, now, ctx.zone)}, {when}."
        listing = ctx.events.list_scope(EventScope(scope), event_type=kind)
        phrase = {"upcoming": f"in the next {ctx.events.lookahead_days} days", "today": "today", "tomorrow": "tomorrow",
                  "this_week": "this week", "next_week": "next week", "next_7_days": "in the next 7 days",
                  "overdue": "overdue", "all": "that I know about"}[scope]
        if not listing.events:
            text = f"You have no {plural} {phrase}." if scope != "overdue" else "You have no overdue deadlines."
        else:
            n = listing.total
            text = f"You have {n} {noun if n == 1 else plural} {phrase}: " + _spoken(
                [describe(e, now, ctx.zone) for e in listing.events]) + "."
            if listing.truncated:
                text += f" I only list the first {len(listing.events)}."
        if listing.unconfirmed:
            text += f" I also have {listing.unconfirmed} unconfirmed item{'s' if listing.unconfirmed != 1 else ''} from your emails or documents; ask about them to confirm."
        if conflicts:
            found = ctx.events.conflicts_in(listing.events)
            text += _conflict_note(found, now, ctx.zone) if found else " None of them overlap."
        return text


class EventSearchTool(EventTool):
    action = EventActionName.SEARCH
    description = "Find events or deadlines by words (e.g. 'project submission', 'internship interview')."
    input_schema = {"query": "string: words from the title", "type": "optional type filter", "include_past": "boolean, default false"}
    requires_permission = False
    risk = RiskLevel.LOW

    def resolve(self, args: EventSearchArgs) -> Ready:
        return Ready({"query": args.query, "type": args.type.value if args.type else None, "include_past": args.include_past})

    def run(self, *, query: str, type: str | None, include_past: bool, origin_session: str | None = None) -> str:
        ctx, now = self._ctx, self._ctx.now()
        found = ctx.events.find_matching(query, event_type=EventType(type) if type else None, include_closed=include_past)
        if not found:
            return "I don't know of any event or deadline matching that."
        return f"I found {len(found)}: " + _spoken([describe(e, now, ctx.zone) for e in found]) + "."


class EventGetTool(EventTool):
    action = EventActionName.GET
    description = "Details of one event or deadline: when, how long until it, its source, its task and any overlaps."
    input_schema = {"query": "string: words identifying the event (e.g. 'my interview')"}
    requires_permission = False
    risk = RiskLevel.LOW

    def resolve(self, args: EventGetArgs) -> Ready:
        return Ready({"query": args.query})

    def run(self, *, query: str, origin_session: str | None = None) -> str:
        ctx, now = self._ctx, self._ctx.now()
        found = self._identify(query)
        if isinstance(found, Clarify):
            return found.message
        event = found
        parts = [f"{describe(event, now, ctx.zone)}: {countdown(event, now, ctx.zone)}."]
        if event.status is EventStatus.UNKNOWN:
            parts.append("I'm not sure about this one; say 'confirm that one' if it is right.")
        elif event.confidence is Confidence.LOW:
            parts.append("I'm not very sure about this date.")
        src = event.source
        if src.source_type is SourceType.UNKNOWN or not src.reference:
            parts.append("I don't know where this came from.")
        else:
            parts.append(f"This came from {src.reference}.")
        if event.task_id and ctx.tasks is not None:
            try:
                parts.append(f"It's linked to your task '{one_line(ctx.tasks.get_task(event.task_id).title, 60)}'.")
                if ctx.events.task_due_state(event) is SyncOutcome.MISMATCH:
                    parts.append("The task's due date is different from this one.")
            except TaskError:
                pass
        parts.append(_conflict_note(ctx.events.conflicts_for(event), now, ctx.zone).strip())
        return " ".join(p for p in parts if p)


# ---- complete / cancel / update ----------------------------------------------------------------------------------------


class EventCompleteTool(EventTool):
    action = EventActionName.COMPLETE
    description = "Mark an event or deadline as done. Describe it in words; ids are never used."
    input_schema = {"query": "string: words identifying the event"}
    requires_permission = False
    risk = RiskLevel.LOW

    def resolve(self, args: EventCompleteArgs) -> Clarify | Ready:
        found = self._identify(args.query)
        return found if isinstance(found, Clarify) else Ready({"event_id": found.event_id})

    def run(self, *, event_id: str, origin_session: str | None = None) -> str:
        event = self._ctx.events.complete_event(event_id)
        return f"Done. I've marked it completed: {one_line(event.title, 80)}."


class EventCancelTool(EventTool):
    action = EventActionName.CANCEL
    description = "Cancel an event or deadline (asks the user to confirm first). Describe it in words; ids are never used."
    input_schema = {"query": "string: words identifying the event"}
    requires_permission = True
    risk = RiskLevel.MEDIUM

    def resolve(self, args: EventCancelArgs) -> Clarify | Ready:
        found = self._identify(args.query)
        if isinstance(found, Clarify):
            return found
        return Ready({"event_id": found.event_id},
                     f"Do you want me to cancel: {describe(found, self._ctx.now(), self._ctx.zone)}? Say yes to confirm.")

    def run(self, *, event_id: str, origin_session: str | None = None) -> str:
        ctx = self._ctx
        event = ctx.events.cancel_event(event_id)
        links = event.metadata.get("graph_links")
        if links and ctx.linker is not None:
            ctx.linker.unlink(list(links))
        return f"Okay, I've cancelled: {one_line(event.title, 80)}. Nothing outside JARVIS was changed."


class EventUpdateTool(EventTool):
    action = EventActionName.UPDATE
    description = (
        "Change an event or deadline (asks the user to confirm first): its time, title, type, priority, description or "
        "task link, or confirm an unconfirmed one. This only edits JARVIS's own record; nothing is rescheduled elsewhere."
    )
    input_schema = {
        "query": "string, required: words identifying the event",
        "when": "string: the new date/time exactly as the user said it",
        "title": "string", "type": "deadline | meeting | interview | exam | ...", "duration_minutes": "integer",
        "priority": "low | medium | high | critical", "description": "string",
        "task_query": "string: words identifying an existing task to link",
        "confirm": "boolean: true = the user confirms an unconfirmed event is right",
    }
    requires_permission = True
    risk = RiskLevel.MEDIUM

    def resolve(self, args: EventUpdateArgs) -> Clarify | Ready:
        ctx = self._ctx
        found = self._identify(args.query)
        if isinstance(found, Clarify):
            return found
        event = found
        params: dict[str, Any] = {
            "event_id": event.event_id, "title": args.title, "event_type": args.type.value if args.type else None,
            "priority": args.priority.name if args.priority else None, "description": args.description,
            "confirm": args.confirm and event.status is EventStatus.UNKNOWN, "task_id": None, "times": None,
        }
        changes = []
        if args.title:
            changes.append("rename it")
        if args.type:
            changes.append(f"make it a {args.type.value}")
        if args.priority:
            changes.append(f"set {args.priority.name.lower()} priority")
        if params["confirm"]:
            changes.append("mark it confirmed")
        if args.task_query:
            if ctx.tasks is None:
                return Clarify("Tasks are turned off, so I can't link a task.")
            tasks = ctx.tasks.find_matching_tasks(args.task_query)
            if len(tasks) != 1:
                return Clarify("I couldn't find one open task matching that." if not tasks else
                               f"I found {len(tasks)} tasks that could match: {_spoken([one_line(t.title, 60) for t in tasks])}. Which one?")
            params["task_id"] = tasks[0].task_id
            changes.append("link it to your task")
        if args.when or args.duration_minutes:
            new_type = args.type or event.event_type
            if args.when:
                when = self._resolve_when(args.when)
                if isinstance(when, Clarify):
                    return when
            else:  # only the duration changes: keep the start
                if event.start_at is None:
                    return Clarify("That's a deadline, so it has no duration. Tell me the new time instead.")
                when = ResolvedWhen(WhenKind.EXACT, event.start_at.astimezone(ctx.zone))
            times = event_times(when, new_type, args.duration_minutes)
            if (times.due_at or times.end_at or times.start_at) < ctx.now():  # type: ignore[operator]
                return Clarify("That time has already passed. What is the correct date?")
            params["times"] = {"start_at": _iso(times.start_at), "end_at": _iso(times.end_at), "due_at": _iso(times.due_at),
                               "all_day": times.all_day}
            changes.append("move it to " + (when_text(_preview(event, new_type, times), ctx.now(), ctx.zone)))
        if args.description and "rename it" not in changes:
            changes.append("update its description")
        prompt = (f"Do you want me to {', and '.join(changes)} for {describe(event, ctx.now(), ctx.zone)}? Say yes to confirm.")
        return Ready(params, prompt)

    def run(self, *, event_id: str, title: str | None, event_type: str | None, priority: str | None, description: str | None,
            confirm: bool, task_id: str | None, times: dict | None, origin_session: str | None = None) -> str:
        events = self._ctx.events
        if confirm:
            events.confirm_event(event_id)
        kwargs: dict[str, Any] = {}
        if title:
            kwargs["title"] = title
        if event_type:
            kwargs["event_type"] = EventType(event_type)
        if priority:
            kwargs["priority"] = TaskPriority[priority]
        if description:
            kwargs["description"] = description
        if times:
            kwargs.update(start_at=_from_iso(times["start_at"]), end_at=_from_iso(times["end_at"]),
                          due_at=_from_iso(times["due_at"]), all_day=times["all_day"])
        event = events.update_event(event_id, **kwargs) if kwargs else events.get_event(event_id)
        if task_id:
            event = events.link_task(event_id, task_id)
        conflicts = events.conflicts_for(event)
        return f"Okay, I've updated it: {describe(event, self._ctx.now(), self._ctx.zone)}.{_conflict_note(conflicts, self._ctx.now(), self._ctx.zone)}"


def _preview(event: Event, event_type: EventType, times) -> Event:
    return event.model_copy(update={"event_type": event_type, "start_at": times.start_at, "end_at": times.end_at,
                                    "due_at": times.due_at, "all_day": times.all_day})


# ---- extract from sources ------------------------------------------------------------------------------------------------


class EventExtractTool(EventTool):
    action = EventActionName.EXTRACT
    description = (
        "Find dates and deadlines in one of the user's emails, indexed documents or saved memories and save them as events "
        "(asks the user to confirm first). Nothing is added automatically."
    )
    input_schema = {
        "source": "gmail | document | memory",
        "query": "string: gmail search words (e.g. 'from:acme interview'), a file name, or memory words",
        "latest": "boolean, gmail only: the newest matching email",
    }
    requires_permission = True
    risk = RiskLevel.MEDIUM

    def resolve(self, args: EventExtractArgs) -> Clarify | Ready:
        ctx = self._ctx
        if args.source == "gmail" and ctx.gmail is None:
            return Clarify("Gmail isn't turned on, so I can't look in your email.")
        if args.source == "document" and ctx.rag is None:
            return Clarify("Document search isn't turned on, so I can't look in your documents.")
        if args.source == "memory" and ctx.memory is None:
            return Clarify("Personal memory isn't turned on.")
        where = {"gmail": "your email", "document": "your documents", "memory": "what you've told me"}[args.source]
        target = f" matching '{one_line(args.query, 60)}'" if args.query else " (the latest one)"
        return Ready({"source": args.source, "query": args.query or "", "latest": args.latest},
                     f"Do you want me to look in {where}{target} for dates and deadlines and save what I find? Say yes to confirm.")

    def run(self, *, source: str, query: str, latest: bool, origin_session: str | None = None) -> str:
        ctx, now = self._ctx, self._ctx.now()
        ingestor = EventIngestor(ctx.events, ctx.parser, ctx.linker)
        if source == "gmail":
            assert ctx.gmail is not None
            found = ctx.gmail.find(query)
            if not found:
                return "I couldn't find a matching email."
            if len(found) > 1 and not latest:
                listed = "; ".join(f"{one_line(m.sender.display if m.sender else 'unknown', 40)}: {one_line(m.subject or '(no subject)', 60)}" for m in found[:MAX_SPOKEN])
                return f"I found {len(found)} emails that could match: {listed}. Which one do you mean? Give me the sender or a word from the subject."
            result = ingestor.from_gmail_message(found[0], now)
            label = "that email"
        elif source == "document":
            docs = ctx.rag.list_documents()
            hits = find_matches(query, [(d.document_id, f"{d.filename} {d.title or ''}") for d in docs if d.status.value == "indexed"])
            if not hits:
                return "I couldn't find an indexed document matching that."
            if len(hits) > 1:
                return f"I found {len(hits)} documents that could match. Which one do you mean? Say more of the file name."
            doc = next(d for d in docs if d.document_id == hits[0])
            result = ingestor.from_document(doc.document_id, doc.filename, ctx.rag.get_chunks(doc.document_id), now)
            label = "that document"
        else:
            result = ingestor.from_memories(ctx.memory.retrieve(query, limit=5), now)
            label = "what you've told me"
        return _summary(result, label, now, ctx.zone)


def _summary(result: IngestResult, label: str, now: datetime, zone) -> str:
    if not (result.created or result.duplicates or result.unresolved):
        extra = f" ({result.expired} had dates that have already passed.)" if result.expired else ""
        return f"I didn't find any dates or deadlines in {label}.{extra}"
    parts = []
    if result.created:
        text = f"I saved {len(result.created)} from {label}: " + _spoken([describe(e, now, zone) for e in result.created]) + "."
        if result.unconfirmed:
            text += f" {result.unconfirmed} of them I'm not sure about, so they're marked unconfirmed."
        parts.append(text)
    if result.duplicates:
        parts.append(f"{len(result.duplicates)} {'was' if len(result.duplicates) == 1 else 'were'} already saved from it, so I added nothing new.")
    for u in result.unresolved[:2]:
        parts.append(f"I found a date I couldn't pin down. {u.question}")
    if result.expired:
        parts.append(f"{result.expired} had dates that have already passed, so I skipped {'it' if result.expired == 1 else 'them'}.")
    return " ".join(parts)


def build_event_tools(context: EventToolContext) -> list[EventTool]:
    tools: list[EventTool] = [
        EventCreateTool(context), EventListTool(context), EventSearchTool(context), EventGetTool(context),
        EventCompleteTool(context), EventCancelTool(context), EventUpdateTool(context),
    ]
    if context.gmail is not None or context.rag is not None or context.memory is not None:
        tools.append(EventExtractTool(context))
    return tools
