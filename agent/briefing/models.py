"""Typed models for the daily briefing and productivity intelligence.

    existing services -> ProductivityContext (normalized items + source references) -> analysis -> DailyBriefing -> spoken text

Nothing here stores a copy of source data: an item keeps a short sanitized title, the relevant time, the analysis result and a
reference (`SourceRef`) back to the service that owns the data. The briefing is a read-only VIEW of existing information.
"""

from datetime import datetime
from enum import IntEnum, StrEnum

from pydantic import BaseModel, ConfigDict, Field

from agent.tasks.models import TaskPriority


class BriefingError(Exception):
    """Base class. Messages never contain source content."""


class BriefingWindow(StrEnum):
    TODAY = "today"
    TOMORROW = "tomorrow"
    THIS_WEEK = "this_week"  # today until the end of Sunday
    NEXT_7_DAYS = "next_7_days"
    YESTERDAY = "yesterday"  # looking back: "what did I miss yesterday?"
    LAST_24_HOURS = "last_24_hours"  # looking back: "what happened while I was away?"

    @property
    def is_past(self) -> bool:
        return self in (BriefingWindow.YESTERDAY, BriefingWindow.LAST_24_HOURS)


class Detail(StrEnum):
    QUICK = "quick"
    NORMAL = "normal"
    DETAILED = "detailed"


class View(StrEnum):
    OVERVIEW = "overview"  # the morning briefing / "what do I have today?"
    SCHEDULE = "schedule"
    TASKS = "tasks"
    DEADLINES = "deadlines"
    PRIORITIES = "priorities"
    FOCUS = "focus"
    NEXT = "next"
    MISSED = "missed"
    PREPARE = "prepare"


class PriorityLevel(IntEnum):
    """The briefing's own ordering categories. They never change the underlying task or event priority."""

    LOW = 1
    NORMAL = 2
    HIGH = 3
    CRITICAL = 4


class SourceName(StrEnum):
    TASKS = "tasks"
    REMINDERS = "reminders"
    EVENTS = "events"  # Phase 11 events and deadlines
    CALENDAR = "calendar"  # Google Calendar
    GMAIL = "gmail"
    MESSAGING = "messaging"


class ItemKind(StrEnum):
    TASK = "task"
    REMINDER = "reminder"
    DEADLINE = "deadline"
    EVENT = "event"  # a Phase 11 event
    CALENDAR_EVENT = "calendar_event"
    EMAIL = "email"
    MESSAGE = "message"


class SourceState(StrEnum):
    OK = "ok"
    NOT_CONFIGURED = "not_configured"  # never set up: silently left out, it is not an error
    DISABLED = "disabled"
    UNSUPPORTED = "unsupported"  # configured, but the provider cannot do it
    UNAVAILABLE = "unavailable"  # set up but failing right now: reported honestly


class SourceRef(BaseModel):
    model_config = ConfigDict(frozen=True)

    source: SourceName
    source_id: str = Field(default="", max_length=128)
    label: str = Field(max_length=100)  # "your task list", "your Google Calendar"


class SourceStatus(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: SourceName
    state: SourceState = SourceState.OK


class BriefingItem(BaseModel):
    """One normalized thing worth mentioning, with where it came from and why it ranks where it does."""

    model_config = ConfigDict(frozen=True)

    key: str = Field(max_length=200)  # "<kind>:<source id>"
    kind: ItemKind
    title: str = Field(max_length=100)  # sanitized, bounded; may be external text and is only ever quoted
    when: datetime | None = None  # start / due / scheduled time (aware)
    ends: datetime | None = None
    all_day: bool = False
    level: PriorityLevel = PriorityLevel.NORMAL
    score: int = Field(default=0, exclude=True)  # internal ordering only; never shown to the user
    explicit_priority: TaskPriority | None = None  # what the source itself says (tasks, events); None if it says nothing
    reasons: tuple[str, ...] = ()  # short factual reasons, used to answer "why are you mentioning this?"
    source: SourceRef
    detail: str = Field(default="", max_length=100)  # e.g. an email's sender name; never an address or a body
    flags: tuple[str, ...] = ()  # "overdue", "unread", "high_priority", "past", ...


class ConflictKind(StrEnum):
    OVERLAP = "overlap"
    ALL_DAY = "all_day"
    DEADLINE_CLUSTER = "deadline_cluster"  # several important deadlines close together


class ConflictInfo(BaseModel):
    model_config = ConfigDict(frozen=True)

    kind: ConflictKind
    first: str  # item key or title
    second: str
    first_title: str = ""
    second_title: str = ""
    when: datetime | None = None
    source: SourceName = SourceName.CALENDAR


class PreparationLink(BaseModel):
    """An existing pending task that looks like preparation for an upcoming event. Nothing is invented or created."""

    model_config = ConfigDict(frozen=True)

    event_key: str
    event_title: str = ""
    event_when: datetime | None = None
    task_key: str
    task_title: str = ""
    basis: str  # "linked" (recorded link), "named" (shares a keyword) or "pending" (a preparation-style task due before it)


class ProductivityContext(BaseModel):
    """Everything the briefing may draw on, normalized, with the status of every source."""

    now: datetime
    timezone: str
    window: BriefingWindow
    start: datetime
    end: datetime
    tasks_overdue: list[BriefingItem] = Field(default_factory=list)
    tasks_due: list[BriefingItem] = Field(default_factory=list)  # due inside the window
    tasks_upcoming: list[BriefingItem] = Field(default_factory=list)  # due after the window, within the lookahead
    tasks_high: list[BriefingItem] = Field(default_factory=list)  # open tasks the user marked high/critical
    reminders: list[BriefingItem] = Field(default_factory=list)
    deadlines_overdue: list[BriefingItem] = Field(default_factory=list)
    deadlines_due: list[BriefingItem] = Field(default_factory=list)
    deadlines_upcoming: list[BriefingItem] = Field(default_factory=list)
    events: list[BriefingItem] = Field(default_factory=list)  # calendar and Phase 11 events inside the window
    events_upcoming: list[BriefingItem] = Field(default_factory=list)
    emails_action: list[BriefingItem] = Field(default_factory=list)
    emails_important: list[BriefingItem] = Field(default_factory=list)
    messages: list[BriefingItem] = Field(default_factory=list)
    conflicts: list[ConflictInfo] = Field(default_factory=list)
    preparation: list[PreparationLink] = Field(default_factory=list)
    missed: list[BriefingItem] = Field(default_factory=list)
    completed_today: int | None = None  # a factual count, never a score
    past_event_count: int | None = None
    calendar_truncated: bool = False  # the calendar had more events than were read (the count is then "at least")
    statuses: list[SourceStatus] = Field(default_factory=list)

    def state_of(self, name: SourceName) -> SourceState:
        return next((s.state for s in self.statuses if s.name is name), SourceState.NOT_CONFIGURED)

    @property
    def unavailable(self) -> list[SourceName]:
        return [s.name for s in self.statuses if s.state is SourceState.UNAVAILABLE]

    @property
    def high_priority(self) -> list[BriefingItem]:
        """Items whose own priority or timing puts them at HIGH or above, most pressing first."""
        pool = [*self.tasks_overdue, *self.tasks_due, *self.tasks_upcoming, *self.tasks_high, *self.deadlines_overdue, *self.deadlines_due, *self.deadlines_upcoming]
        seen: dict[str, BriefingItem] = {}
        for item in pool:
            if item.level >= PriorityLevel.HIGH:
                seen.setdefault(item.key, item)
        return sorted(seen.values(), key=lambda i: (-i.score, i.when is None, i.when, i.key))

    def all_items(self) -> dict[str, BriefingItem]:
        pool = [
            *self.tasks_overdue, *self.tasks_due, *self.tasks_upcoming, *self.tasks_high, *self.reminders, *self.deadlines_overdue, *self.deadlines_due,
            *self.deadlines_upcoming, *self.events, *self.events_upcoming, *self.emails_action, *self.emails_important, *self.messages, *self.missed,
        ]
        return {i.key: i for i in pool}


class SectionName(StrEnum):
    SCHEDULE = "schedule"
    TASKS = "tasks"
    DEADLINES = "deadlines"
    REMINDERS = "reminders"
    EMAILS = "emails"
    MESSAGES = "messages"
    CONFLICTS = "conflicts"
    PREPARATION = "preparation"
    PRIORITIES = "priorities"
    MISSED = "missed"


class BriefingSection(BaseModel):
    name: SectionName
    total: int  # how many items exist, before the detail limit
    items: list[BriefingItem] = Field(default_factory=list)  # the items shown for this detail level
    line: str  # the spoken sentence for this section


class DailyBriefing(BaseModel):
    """The result: only sections that have real content, plus honest notes about anything that could not be checked."""

    view: View
    window: BriefingWindow
    detail: Detail
    generated_at: datetime
    timezone: str
    greeting: str = ""
    date_label: str = ""
    sections: list[BriefingSection] = Field(default_factory=list)
    focus: list[str] = Field(default_factory=list)  # each a hedged, fact-based suggestion (the user decides)
    risks: list[str] = Field(default_factory=list)  # neutral factual attention indicators
    notes: list[str] = Field(default_factory=list)  # degraded-state notes ("I couldn't reach your calendar")
    spoken: str = ""
    items: dict[str, BriefingItem] = Field(default_factory=dict)  # for "where did you get that?" (references, not copies)
    empty: bool = False  # nothing real was found (and nothing failed)
    degraded: bool = False  # at least one source could not be read

    def section(self, name: SectionName) -> BriefingSection | None:
        return next((s for s in self.sections if s.name is name), None)
