"""EventService: all event and deadline rules. Persistence is the repository's job.

The LLM never reaches this class: a validated action goes through the PermissionManager and a Tool. Nothing here
notifies, reminds or schedules anything (that is Phase 14) and nothing talks to any calendar (that is Phase 12).
Logging carries ids and statuses only, never titles, descriptions or source text.
"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any
from zoneinfo import ZoneInfo

from agent.events.conflicts import Conflict, conflict_between, find_conflicts
from agent.events.models import (
    EVENT_TRANSITIONS,
    LIVE_STATUSES,
    OPEN_STATUSES,
    CreateResult,
    Event,
    EventNotFound,
    EventSource,
    EventStatus,
    EventType,
    EventValidationError,
    InvalidEventTransition,
    SourceType,
    make_dedupe_key,
)
from agent.events.repository import EventRepository
from agent.events.temporal import EventScope, effective_end, in_window, scope_window
from agent.memory.models import Confidence
from agent.tasks.matching import find_matches
from agent.tasks.models import TaskError, TaskPriority, TaskStatus
from agent.tasks.models import to_utc as _to_utc
from agent.tasks.models import utcnow
from agent.tasks.service import TaskService
from backend.core.logging import get_logger

logger = get_logger(__name__)

Clock = Callable[[], datetime]
SCAN_LIMIT = 500  # events scanned per query; a personal event list is far smaller
_UNSET: Any = object()
# Sources whose text JARVIS did not hear from the user: a LOW-confidence result waits for confirmation.
_EXTERNAL = frozenset({SourceType.GMAIL, SourceType.RAG_DOCUMENT, SourceType.MEMORY})


@dataclass(frozen=True)
class EventListing:
    events: list[Event]
    total: int  # matching events before the result limit
    unconfirmed: int = 0  # unconfirmed (UNKNOWN) events that would also fall in this scope

    @property
    def truncated(self) -> bool:
        return self.total > len(self.events)


class SyncOutcome(StrEnum):
    APPLIED = "applied"  # the linked task had no due date, now it has the event's
    IN_SYNC = "in_sync"
    MISMATCH = "mismatch"  # both have a date and they differ: reported, never overwritten
    NO_TASK = "no_task"


def _require_aware(value: datetime | None, name: str) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None or value.utcoffset() is None:
        raise EventValidationError(f"{name} must be timezone-aware", "I need an exact date and time zone for that.")
    return _to_utc(value)


def _first_error(exc: ValueError) -> str:
    errors = getattr(exc, "errors", None)
    if callable(errors):
        first = errors()[0]
        return f"Invalid {'.'.join(str(p) for p in first['loc'])}: {first['msg']}"
    return "Invalid value"


class EventService:
    def __init__(
        self,
        repository: EventRepository,
        *,
        zone: ZoneInfo,
        clock: Clock = utcnow,
        tasks: TaskService | None = None,
        max_results: int = 20,
        lookahead_days: int = 7,
    ):
        self._repo = repository
        self._zone = zone
        self._clock = clock
        self._tasks = tasks
        self._max_results = max(1, max_results)
        self._lookahead = max(1, lookahead_days)

    @property
    def zone(self) -> ZoneInfo:
        return self._zone

    @property
    def max_results(self) -> int:
        return self._max_results

    @property
    def lookahead_days(self) -> int:
        return self._lookahead

    # ---- create / read -----------------------------------------------------------------------------------

    def create_event(
        self,
        title: str,
        event_type: EventType = EventType.EVENT,
        *,
        start_at: datetime | None = None,
        end_at: datetime | None = None,
        due_at: datetime | None = None,
        all_day: bool = False,
        description: str | None = None,
        priority: TaskPriority | None = None,
        source: EventSource | None = None,
        confidence: Confidence = Confidence.HIGH,
        status: EventStatus | None = None,
        task_id: str | None = None,
        metadata: dict[str, Any] | None = None,
        revive_closed: bool = False,
    ) -> CreateResult:
        """Store an event. An identical event from the same source is not stored twice: the existing one is
        returned with created=False. With `revive_closed` (the user explicitly adds it again) a completed or
        cancelled identical event comes back to life. When uncertain, events stay separate (no semantic merging)."""
        now = self._clock()
        source = source or EventSource()
        start_at, end_at, due_at = (_require_aware(v, n) for v, n in ((start_at, "start_at"), (end_at, "end_at"), (due_at, "due_at")))
        if status is None:
            status = EventStatus.UNKNOWN if confidence is Confidence.LOW and source.source_type in _EXTERNAL else EventStatus.UPCOMING
        try:
            event = Event(
                title=title, description=description, event_type=event_type, status=status, priority=priority,
                start_at=start_at, end_at=end_at, due_at=due_at, timezone=self._zone.key, all_day=all_day,
                source=source, confidence=confidence, task_id=task_id, created_at=now, updated_at=now,
                metadata=dict(metadata or {}),
            )
        except ValueError as exc:
            raise EventValidationError(_first_error(exc)) from None
        event = event.model_copy(update={"dedupe_key": make_dedupe_key(event.title, event.event_type, event.anchor)})

        with self._repo.unit_of_work():
            existing = self._repo.find_by_dedupe(source.source_type, source.source_id, event.dedupe_key)
            if existing is not None:
                if revive_closed and existing.status in (EventStatus.COMPLETED, EventStatus.CANCELLED):
                    self._repo.update_event(existing.event_id, {
                        "status": EventStatus.UPCOMING.value, "updated_at": now, "completed_at": None, "cancelled_at": None,
                    })
                    logger.info("Event revived (event=%s)", existing.event_id)
                    return CreateResult(event=self.get_event(existing.event_id), created=False, revived=True)
                return CreateResult(event=existing, created=False)
            if task_id is not None:
                self._require_task(task_id)
            self._repo.add_event(event)
        logger.info("Event created (event=%s, type=%s, status=%s, source=%s, confidence=%s)", event.event_id,
                    event.event_type.value, event.status.value, source.source_type.value, event.confidence.name)
        return CreateResult(event=event, created=True)

    def get_event(self, event_id: str) -> Event:
        event = self._repo.get_event(event_id)
        if event is None:
            raise EventNotFound("No such event")
        return event

    def _require_task(self, task_id: str) -> None:
        if self._tasks is None:
            raise EventValidationError("tasks are disabled", "Tasks are turned off, so I can't link a task.")
        try:
            task = self._tasks.get_task(task_id)
        except TaskError:
            raise EventValidationError("no such task", "I can't find that task.") from None
        if task.status in (TaskStatus.COMPLETED, TaskStatus.CANCELLED):
            raise EventValidationError("closed task", "That task is already finished.")

    # ---- status ----------------------------------------------------------------------------------------------

    def refresh_statuses(self, now: datetime | None = None) -> int:
        """UPCOMING becomes ACTIVE while it is going on; UPCOMING/ACTIVE become MISSED once their time has passed
        without being marked completed. Runs before every query, so answers are right without any background job."""
        moment = now or self._clock()
        changed = 0
        for event in self._repo.list_events(statuses=OPEN_STATUSES, limit=SCAN_LIMIT):
            target = None
            if effective_end(event, self._zone) < moment:
                target = EventStatus.MISSED
            elif event.status is EventStatus.UPCOMING and event.start_at is not None and event.start_at <= moment:
                target = EventStatus.ACTIVE
            if target is None:
                continue
            try:
                self._transition(event.event_id, target)
                changed += 1
            except InvalidEventTransition:
                continue  # completed/cancelled meanwhile
        return changed

    def complete_event(self, event_id: str) -> Event:
        return self._transition(event_id, EventStatus.COMPLETED)

    def cancel_event(self, event_id: str) -> Event:
        return self._transition(event_id, EventStatus.CANCELLED)

    def confirm_event(self, event_id: str) -> Event:
        """The user confirms an unconfirmed (UNKNOWN) extraction: it becomes a normal upcoming event."""
        return self._transition(event_id, EventStatus.UPCOMING)

    def _transition(self, event_id: str, new: EventStatus) -> Event:
        for _ in range(3):
            event = self.get_event(event_id)
            if new not in EVENT_TRANSITIONS[event.status]:
                raise InvalidEventTransition(
                    f"Cannot change a {event.status.value} event to {new.value}",
                    f"That event is already {event.status.value}.",
                )
            now = self._clock()
            values: dict[str, Any] = {"status": new.value, "updated_at": now}
            if new is EventStatus.COMPLETED:
                values["completed_at"] = now
            elif new is EventStatus.CANCELLED:
                values["cancelled_at"] = now
            if self._repo.update_event(event_id, values, expected_status=event.status):
                logger.info("Event status changed (event=%s, %s -> %s)", event_id, event.status.value, new.value)
                return self.get_event(event_id)
        raise InvalidEventTransition("The event keeps changing", "That event keeps changing; please try again.")

    # ---- update ------------------------------------------------------------------------------------------------

    def update_event(
        self,
        event_id: str,
        *,
        title: str | None = None,
        event_type: EventType | None = None,
        start_at: datetime | None = _UNSET,
        end_at: datetime | None = _UNSET,
        due_at: datetime | None = _UNSET,
        all_day: bool | None = None,
        priority: TaskPriority | None = None,
        description: str | None = _UNSET,
    ) -> Event:
        """Edit a live event. A MISSED event moved to the future becomes UPCOMING again. Completed and cancelled
        events are final. Two events from one source cannot end up identical."""
        event = self.get_event(event_id)
        if event.status in (EventStatus.COMPLETED, EventStatus.CANCELLED):
            raise InvalidEventTransition(f"Cannot edit a {event.status.value} event",
                                         f"That event is {event.status.value}, so it can't be changed.")
        data = event.model_dump()
        for key, value in (("title", title), ("event_type", event_type), ("priority", priority), ("all_day", all_day)):
            if value is not None:
                data[key] = value
        for key, value in (("start_at", start_at), ("end_at", end_at), ("due_at", due_at), ("description", description)):
            if value is not _UNSET:
                data[key] = _require_aware(value, key) if key.endswith("_at") else value
        now = self._clock()
        data["updated_at"] = now
        try:
            updated = Event(**data)
        except ValueError as exc:
            raise EventValidationError(_first_error(exc)) from None
        key = make_dedupe_key(updated.title, updated.event_type, updated.anchor)
        clash = self._repo.find_by_dedupe(updated.source.source_type, updated.source.source_id, key)
        if clash is not None and clash.event_id != event_id:
            raise EventValidationError("duplicate", "Another event from the same source already has those details.")
        status = event.status
        if status is EventStatus.MISSED and effective_end(updated, self._zone) >= now:
            status = EventStatus.UPCOMING
        values = {
            "title": updated.title, "description": updated.description, "event_type": updated.event_type.value,
            "priority": int(updated.priority) if updated.priority is not None else None, "start_at": updated.start_at,
            "end_at": updated.end_at, "due_at": updated.due_at, "all_day": updated.all_day, "dedupe_key": key,
            "updated_at": now, "status": status.value,
        }
        if not self._repo.update_event(event_id, values, expected_status=event.status):
            raise InvalidEventTransition("The event changed while updating it", "That event changed while I was updating it.")
        logger.info("Event updated (event=%s)", event_id)
        return self.get_event(event_id)

    def merge_metadata(self, event_id: str, patch: dict[str, Any]) -> Event:
        event = self.get_event(event_id)
        self._repo.update_event(event_id, {"extra": {**event.metadata, **patch}, "updated_at": self._clock()})
        return self.get_event(event_id)

    # ---- queries -------------------------------------------------------------------------------------------------

    def list_scope(self, scope: EventScope, *, event_type: EventType | None = None, limit: int | None = None) -> EventListing:
        """Events in a scope, soonest first, bounded. Unconfirmed (UNKNOWN) events are listed only for `ALL`;
        for the others they are only counted, so a low-confidence guess never shows up as a firm commitment."""
        now = self._clock()
        self.refresh_statuses(now)
        cap = max(1, min(limit or self._max_results, self._max_results))
        window = scope_window(scope, now, self._zone, self._lookahead)

        if scope is EventScope.OVERDUE:
            pool = self._repo.list_events(statuses=LIVE_STATUSES, event_type=event_type, limit=SCAN_LIMIT)
            events = [e for e in pool if e.is_deadline and e.due_at < now]  # type: ignore[operator]
            unconfirmed = 0
        elif scope is EventScope.ALL:
            statuses = OPEN_STATUSES | {EventStatus.UNKNOWN}
            events = self._repo.list_events(statuses=statuses, event_type=event_type, limit=SCAN_LIMIT)
            unconfirmed = 0
        else:
            assert window is not None
            pool = self._repo.list_events(statuses=OPEN_STATUSES | {EventStatus.UNKNOWN}, event_type=event_type, limit=SCAN_LIMIT)
            inside = [e for e in pool if in_window(e, window, self._zone)]
            events = [e for e in inside if e.status is not EventStatus.UNKNOWN]
            unconfirmed = len(inside) - len(events)
        return EventListing(events=events[:cap], total=len(events), unconfirmed=unconfirmed)

    def next_event(self, event_type: EventType | None = None) -> Event | None:
        """The soonest event or deadline that has not passed yet."""
        now = self._clock()
        self.refresh_statuses(now)
        for event in self._repo.list_events(statuses=OPEN_STATUSES, event_type=event_type, limit=SCAN_LIMIT):
            if effective_end(event, self._zone) >= now:
                return event
        return None

    def find_matching(
        self, query: str, *, event_type: EventType | None = None, include_closed: bool = False, limit: int = 10
    ) -> list[Event]:
        """Candidates for a description. Matches whole words of the title or the type name; more than one candidate
        means the caller must ask the user. Ids never come from the model."""
        self.refresh_statuses()
        statuses = set(LIVE_STATUSES | {EventStatus.UNKNOWN})
        if include_closed:
            statuses |= {EventStatus.COMPLETED, EventStatus.CANCELLED}
        events = self._repo.list_events(statuses=statuses, event_type=event_type, limit=SCAN_LIMIT)
        ids = set(find_matches(query, [(e.event_id, f"{e.title} {e.event_type.value}") for e in events]))
        return [e for e in events if e.event_id in ids][:limit]

    # ---- conflicts (informational only) ----------------------------------------------------------------------------

    def conflicts_for(self, event: Event) -> list[Conflict]:
        """Other live events that overlap this one. Nothing is rescheduled or changed."""
        if event.start_at is None:
            return []
        pool = self._repo.list_events(statuses=OPEN_STATUSES, limit=SCAN_LIMIT)
        found = [c for e in pool if (c := conflict_between(event, e, self._zone)) is not None]
        return sorted(found, key=lambda c: (c.first.anchor, c.first.event_id, c.second.event_id))

    def conflicts_in(self, events: list[Event]) -> list[Conflict]:
        return find_conflicts(events, self._zone)

    # ---- tasks ------------------------------------------------------------------------------------------------------

    def link_task(self, event_id: str, task_id: str) -> Event:
        """Associate the event with an existing task (a reference, not a copy). No task is created."""
        self._require_task(task_id)
        self.get_event(event_id)
        self._repo.update_event(event_id, {"task_id": task_id, "updated_at": self._clock()})
        logger.info("Event linked to a task (event=%s, task=%s)", event_id, task_id)
        return self.get_event(event_id)

    def unlink_task(self, event_id: str) -> Event:
        self.get_event(event_id)
        self._repo.update_event(event_id, {"task_id": None, "updated_at": self._clock()})
        return self.get_event(event_id)

    def event_for_task(self, task_id: str) -> CreateResult:
        """A DEADLINE event for a task that has a due date, linked to it. Calling it again returns the same event
        (identical source and time), so a task never produces duplicate events."""
        if self._tasks is None:
            raise EventValidationError("tasks are disabled", "Tasks are turned off, so I can't use a task.")
        task = self._tasks.get_task(task_id)
        if task.due_at is None:
            raise EventValidationError("no due date", "That task has no due date.")
        return self.create_event(
            task.title, EventType.DEADLINE, due_at=task.due_at, priority=task.priority, task_id=task.task_id,
            source=EventSource(source_type=SourceType.TASK, source_id=task.task_id, reference="your task"),
        )

    def task_due_state(self, event: Event) -> SyncOutcome:
        """Compare the event with its linked task's due date without changing anything."""
        if event.task_id is None or self._tasks is None:
            return SyncOutcome.NO_TASK
        try:
            task = self._tasks.get_task(event.task_id)
        except TaskError:
            return SyncOutcome.NO_TASK
        if task.due_at is None:
            return SyncOutcome.APPLIED  # "could be applied": the task has no due date yet
        return SyncOutcome.IN_SYNC if task.due_at == (event.due_at or event.start_at) else SyncOutcome.MISMATCH

    def sync_task_due(self, event_id: str) -> SyncOutcome:
        """Give the linked task the event's date, but only if the task has none. A differing date is reported as
        MISMATCH and never overwritten: JARVIS does not decide which of two dates is right."""
        event = self.get_event(event_id)
        state = self.task_due_state(event)
        if state is SyncOutcome.APPLIED and self._tasks is not None and event.task_id is not None:
            self._tasks.update_task(event.task_id, due_at=event.due_at or event.start_at)
            return SyncOutcome.APPLIED
        return SyncOutcome.IN_SYNC if state is SyncOutcome.APPLIED else state
