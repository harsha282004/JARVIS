"""Briefing generation: deterministic analysis and voice-optimized wording over a ProductivityContext.

Everything spoken is built from the structured context by fixed templates, so nothing can be invented: a number is a count of
real items, a title is a sanitized source title in quotes, a time is a source time. A section exists only if it has real content
(there are no fake "nothing found" sections). Suggestions are hedged ("one reasonable focus is ...") and always carry their
factual reason; the user stays the decision-maker. There is no score or "productivity" judgment anywhere in the output.

Voice rules: counts and grouping instead of long lists, no ids, no e-mail addresses (removed when items are collected), at most
`max_items` items when the user asks for detail, and a hard length cap per detail level.
"""

import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from agent.briefing.models import (
    BriefingItem,
    BriefingSection,
    BriefingWindow,
    ConflictKind,
    DailyBriefing,
    Detail,
    ItemKind,
    ProductivityContext,
    SectionName,
    SourceName,
    SourceState,
    View,
)
from agent.tasks.formatting import format_when
from agent.tasks.models import TaskPriority

_WORDS = ["no", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten"]
_MONTHS = ("January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December")
_DAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
_CAPS = {Detail.QUICK: 320, Detail.NORMAL: 950, Detail.DETAILED: 2400}
_WINDOW_WORDS = {
    BriefingWindow.TODAY: "today", BriefingWindow.TOMORROW: "tomorrow", BriefingWindow.THIS_WEEK: "this week",
    BriefingWindow.NEXT_7_DAYS: "over the next seven days", BriefingWindow.YESTERDAY: "yesterday", BriefingWindow.LAST_24_HOURS: "in the last 24 hours",
}
_SOURCE_WORDS = {
    SourceName.TASKS: ("your tasks", "your tasks"), SourceName.REMINDERS: ("your reminders", "your reminders"),
    SourceName.EVENTS: ("your events and deadlines", "your events and deadlines"), SourceName.CALENDAR: ("your Google Calendar", "your calendar"),
    SourceName.GMAIL: ("your email", "your email"), SourceName.MESSAGING: ("your messages", "your messages"),
}
FOCUS_MIN_SCORE = 25  # something concrete (e.g. a task due today, or a high-priority task due tomorrow) is needed before suggesting a focus


# ---- small speech helpers -----------------------------------------------------------------------------------------------------------


def num(n: int) -> str:
    return _WORDS[n] if 0 <= n < len(_WORDS) else str(n)


def count(n: int, singular: str, plural: str | None = None) -> str:
    return f"{num(n)} {singular if n == 1 else (plural or singular + 's')}"


def say_time(value: datetime, zone: ZoneInfo) -> str:
    local = value.astimezone(zone)
    suffix = "AM" if local.hour < 12 else "PM"
    hour = local.hour % 12 or 12
    return f"{hour} {suffix}" if local.minute == 0 else f"{hour}:{local.minute:02d} {suffix}"


def say_when(item_when: datetime, all_day: bool, now: datetime, zone: ZoneInfo) -> str:
    day = format_when(item_when, now, zone, with_time=False)
    if all_day:
        return "all day" if day == "today" else f"{day}, all day"
    return f"at {say_time(item_when, zone)}" if day == "today" else f"{day} at {say_time(item_when, zone)}"


def say_day(value: datetime, now: datetime, zone: ZoneInfo, *, with_time: bool = True) -> str:
    """'today', 'tomorrow at 5 PM', 'Friday', 'March 9'."""
    day = format_when(value, now, zone, with_time=False)
    return f"{day} at {say_time(value, zone)}" if with_time else day


def title(item: BriefingItem) -> str:
    return f"'{item.title}'"


def join_and(parts: list[str]) -> str:
    if len(parts) <= 1:
        return "".join(parts)
    return ", ".join(parts[:-1]) + " and " + parts[-1]


def reasons_phrase(item: BriefingItem) -> str:
    """'it is marked high priority and is due tomorrow' from the recorded factual reasons."""
    parts = [r for r in item.reasons]
    if not parts:
        return "it falls in the period you asked about"
    return " and ".join([parts[0], *[r[3:] if r.startswith("it ") else r for r in parts[1:]]])


def short_reason(item: BriefingItem) -> str:
    """The most specific recorded reason, without the leading 'it is': 'due tomorrow', 'overdue', 'marked high priority'."""
    if not item.reasons:
        return "in the period you asked about"
    reason = item.reasons[-1]
    for prefix in ("it is ", "it "):
        if reason.startswith(prefix):
            return reason[len(prefix):]
    return reason


def traceable(ctx: ProductivityContext, view: View) -> dict[str, BriefingItem]:
    """The items a briefing of this view actually considered, so "where did you get that?" never points at something that view did
    not present (references only)."""
    everything = ctx.all_items()
    if view is View.OVERVIEW:
        return everything
    pools = {
        View.SCHEDULE: [*ctx.events, *ctx.events_upcoming],
        View.TASKS: [*ctx.tasks_overdue, *ctx.tasks_due, *ctx.tasks_upcoming],
        View.DEADLINES: [*ctx.deadlines_overdue, *ctx.deadlines_due, *ctx.deadlines_upcoming],
        View.PRIORITIES: focus_candidates(ctx),
        View.FOCUS: focus_candidates(ctx),
        View.NEXT: [*ctx.events, *ctx.events_upcoming, *ctx.tasks_overdue, *ctx.tasks_due],
        View.MISSED: ctx.missed,
        View.PREPARE: [i for i in everything.values() if i.key in {p.task_key for p in ctx.preparation}],
    }
    return {i.key: i for i in pools[view]}


def bound(text: str, limit: int) -> str:
    """Cut at a sentence boundary so a spoken answer never runs on, whatever the data."""
    if len(text) <= limit:
        return text
    cut = text[:limit]
    end = max(cut.rfind(". "), cut.rfind("."))
    return (cut[: end + 1] if end > limit // 2 else cut.rsplit(" ", 1)[0] + ".").strip()


# ---- analysis -------------------------------------------------------------------------------------------------------------------------


def focus_candidates(ctx: ProductivityContext) -> list[BriefingItem]:
    """Open tasks and deadlines, most pressing first, that are concrete enough to suggest (never calendar meetings)."""
    pool = [*ctx.tasks_overdue, *ctx.tasks_due, *ctx.tasks_upcoming, *ctx.tasks_high, *ctx.deadlines_overdue, *ctx.deadlines_due, *ctx.deadlines_upcoming]
    unique = {i.key: i for i in pool}
    return sorted((i for i in unique.values() if i.score >= FOCUS_MIN_SCORE), key=lambda i: (-i.score, i.when is None, i.when, i.key))


def focus_lines(ctx: ProductivityContext, zone: ZoneInfo) -> list[str]:
    ranked = focus_candidates(ctx)
    if not ranked:
        return []
    first = ranked[0]
    lines = [f"Based on your deadlines and priorities, one reasonable focus is {title(first)}, because {reasons_phrase(first)}."]
    high = [i for i in ranked if i.explicit_priority is not None and i.explicit_priority >= TaskPriority.HIGH and i.when is not None]
    if len(high) >= 2 and (high[1].when.astimezone(zone).date() != high[0].when.astimezone(zone).date()):
        early, late = sorted(high[:2], key=lambda i: i.when)
        lines.append(
            f"Of your high-priority items, {title(early)} is due {say_day(early.when, ctx.now, zone)} and {title(late)} is due "
            f"{say_day(late.when, ctx.now, zone)}, so their deadlines are a day or more apart."
        )
    elif len(ranked) > 1:
        second = ranked[1]
        lines.append(f"{title(second)} is also worth keeping in view, because {reasons_phrase(second)}.")
    return lines


def risk_lines(ctx: ProductivityContext) -> list[str]:
    """Neutral factual attention indicators. No 'crisis' language: only counts of things the data actually shows."""
    risks: list[str] = []
    overdue = len(ctx.tasks_overdue) + len(ctx.deadlines_overdue)
    if overdue:
        risks.append(f"{count(overdue, 'overdue item')}")
    soon = [i for i in (*ctx.tasks_due, *ctx.tasks_upcoming, *ctx.deadlines_due, *ctx.deadlines_upcoming)
            if i.when is not None and ctx.now <= i.when <= ctx.now + timedelta(hours=24)]
    if soon:
        risks.append(f"{count(len({i.key for i in soon}), 'deadline')} within 24 hours")
    unresolved = [i for i in ctx.tasks_high if "overdue" not in i.flags]
    if unresolved:
        risks.append(f"{count(len(unresolved), 'unresolved high-priority task')}")
    if ctx.conflicts:
        risks.append(f"{count(len(ctx.conflicts), 'scheduling conflict')}")
    mail = len(ctx.emails_action) + len(ctx.emails_important)
    if mail:
        risks.append(f"{count(mail, 'unread email')} that may need attention")
    for name in ctx.unavailable:
        risks.append(f"{_SOURCE_WORDS[name][0]} could not be checked")
    return risks


def degraded_notes(ctx: ProductivityContext, view: View) -> list[str]:
    notes = []
    for name in ctx.unavailable:
        notes.append(f"I couldn't check {_SOURCE_WORDS[name][0]} just now, so this doesn't include {_SOURCE_WORDS[name][1]}.")
    if view in (View.SCHEDULE, View.NEXT) and ctx.state_of(SourceName.CALENDAR) is SourceState.NOT_CONFIGURED:
        notes.append("Google Calendar isn't set up, so I can only see events saved in JARVIS.")
    return notes


def all_failed(ctx: ProductivityContext) -> bool:
    live = [s for s in ctx.statuses if s.state not in (SourceState.NOT_CONFIGURED, SourceState.DISABLED, SourceState.UNSUPPORTED)]
    return bool(live) and all(s.state is SourceState.UNAVAILABLE for s in live)


# ---- section wording ---------------------------------------------------------------------------------------------------------------------


class Builder:
    def __init__(self, zone: ZoneInfo, max_items: int):
        self._zone = zone
        self._max = max(1, max_items)

    def build(self, ctx: ProductivityContext, view: View, detail: Detail, *, day_part: str | None = None) -> DailyBriefing:
        zone = self._zone
        local = ctx.now.astimezone(zone)
        briefing = DailyBriefing(
            view=view, window=ctx.window, detail=detail, generated_at=ctx.now, timezone=ctx.timezone, items=traceable(ctx, view),
            date_label=f"{_DAYS[local.weekday()]}, {_MONTHS[local.month - 1]} {local.day}",
            greeting=(("Good morning" if local.hour < 12 else "Good afternoon" if local.hour < 17 else "Good evening") if view is View.OVERVIEW else ""),
            notes=degraded_notes(ctx, view), risks=risk_lines(ctx), degraded=bool(ctx.unavailable),
        )
        if all_failed(ctx):
            briefing.spoken = "I couldn't read your tasks, events or calendar just now, so I can't give you a reliable briefing. Please try again in a moment."
            briefing.sections, briefing.empty = [], False
            return briefing
        make = {
            View.OVERVIEW: self._overview, View.SCHEDULE: self._schedule_view, View.TASKS: self._tasks_view, View.DEADLINES: self._deadlines_view,
            View.PRIORITIES: self._priorities_view, View.FOCUS: self._focus_view, View.NEXT: self._next_view, View.MISSED: self._missed_view,
            View.PREPARE: self._prepare_view,
        }[view]
        make(briefing, ctx, detail, day_part)
        briefing.focus = focus_lines(ctx, zone) if view in (View.OVERVIEW, View.FOCUS, View.PRIORITIES) else []
        briefing.empty = not briefing.sections and not briefing.focus and not briefing.notes
        parts: list[str] = [briefing.greeting + "." if briefing.greeting else ""]
        body = [s.line for s in briefing.sections]
        if view is View.OVERVIEW and detail is not Detail.QUICK:
            body += briefing.focus[:1] if detail is Detail.NORMAL else briefing.focus
        elif view is View.OVERVIEW:
            body += briefing.focus[:1]
        elif view in (View.FOCUS, View.PRIORITIES):
            body += briefing.focus
        if detail is Detail.DETAILED and view is View.OVERVIEW and briefing.risks:
            body.append("Worth attention: " + join_and(briefing.risks) + ".")
        if not body and not briefing.notes:
            body.append(self._empty(ctx, view))
        parts += body + briefing.notes
        briefing.spoken = bound(" ".join(p for p in parts if p), _CAPS[detail])
        return briefing

    # -- empty --------------------------------------------------------------------------------------------------------------------------

    def _empty(self, ctx: ProductivityContext, view: View) -> str:
        span = _WINDOW_WORDS[ctx.window]
        if view is View.MISSED:
            return f"I don't see anything that slipped by {span}."
        if view is View.FOCUS:
            return "I don't see a task or deadline that stands out right now, so I have no focus suggestion."
        if view is View.PREPARE:
            return "I don't see any existing task that looks like preparation for something coming up."
        if view is View.NEXT:
            return "I don't see anything else coming up on your schedule."
        noun = {View.SCHEDULE: "events", View.TASKS: "tasks", View.DEADLINES: "deadlines", View.PRIORITIES: "priorities"}.get(view, "events, tasks, deadlines or reminders")
        return f"I don't see any {noun} {span}."

    # -- sections ----------------------------------------------------------------------------------------------------------------------

    def _section(self, briefing: DailyBriefing, name: SectionName, total: int, items: list[BriefingItem], line: str) -> None:
        briefing.sections.append(BriefingSection(name=name, total=total, items=items, line=line))

    def _limit(self, detail: Detail, normal: int) -> int:
        return {Detail.QUICK: 1, Detail.NORMAL: normal, Detail.DETAILED: self._max}[detail]

    def _at(self, item: BriefingItem, ctx: ProductivityContext) -> str:
        """'at 10 AM' inside a one-day window (the day is already in the sentence); 'tomorrow at 10 AM' across several days."""
        if ctx.window in (BriefingWindow.TODAY, BriefingWindow.TOMORROW):
            return "all day" if item.all_day else f"at {say_time(item.when, self._zone)}"
        return say_when(item.when, item.all_day, ctx.now, self._zone)

    def _schedule(self, briefing, ctx, detail, day_part=None) -> None:
        events = list(ctx.events)
        if day_part:
            lo, hi = {"morning": (5, 12), "afternoon": (12, 17), "evening": (17, 22)}[day_part]
            events = [e for e in events if e.all_day or (lo <= e.when.astimezone(self._zone).hour < hi)]
        if not events:
            return
        n, span = len(events), _WINDOW_WORDS[ctx.window] + (f" {day_part}" if day_part else "")
        shown = events[: self._limit(detail, 3)]
        phrases = [f"{title(e)} {self._at(e, ctx)}" for e in shown]
        total = f"at least {count(n, 'event')}" if ctx.calendar_truncated and not day_part else count(n, "event")
        if n == 1 and not ctx.calendar_truncated:
            line = f"You have one event {span}: {phrases[0]}."
        elif len(shown) == n and not ctx.calendar_truncated:
            line = f"You have {total} {span}: {join_and(phrases)}."
        else:
            line = f"You have {total} {span}; the first is {phrases[0]}."
        self._section(briefing, SectionName.SCHEDULE, n, shown, line)

    def _tasks(self, briefing, ctx, detail, *, include_upcoming: bool = False) -> None:
        overdue, due = ctx.tasks_overdue, ctx.tasks_due
        total = len(overdue) + len(due)
        upcoming = ctx.tasks_upcoming if include_upcoming else []
        if not total and not upcoming:
            return
        span = _WINDOW_WORDS[ctx.window]
        parts: list[str] = []
        if overdue:
            parts.append(f"{count(len(overdue), 'overdue task')}")
        if due:
            parts.append(f"{count(len(due), 'task')} due {span}")
        high = len([t for t in (*overdue, *due) if t.explicit_priority is not None and t.explicit_priority >= TaskPriority.HIGH])
        if total == 0:
            line = f"You have {count(len(upcoming), 'task')} coming up later."
        elif total <= 6 and not overdue and high and total > high:
            line = f"You have {count(total, 'task')} due {span}: {num(high)} high priority and {num(total - high)} normal."
        elif total <= 6:
            line = f"You have {join_and(parts)}" + (f", including {num(high)} marked high priority" if high and total > 1 else ", marked high priority" if high else "") + "."
        else:
            line = f"You have {count(total, 'task')} to look at, including {num(high)} marked high priority." if high else f"You have {count(total, 'task')} to look at."
            if overdue:
                line += f" {num(len(overdue)).capitalize()} of them {'is' if len(overdue) == 1 else 'are'} overdue."
        shown = [*overdue, *due][: self._limit(detail, 0)]
        if detail is Detail.DETAILED and shown:
            named = [f"{title(t)} ({short_reason(t)})" for t in shown]
            line += " " + join_and(named) + ("." if len(shown) == total else f", and {num(total - len(shown))} more.")
        if include_upcoming and upcoming and total:
            line += f" {num(len(upcoming)).capitalize()} more {'is' if len(upcoming) == 1 else 'are'} due in the days after."
        self._section(briefing, SectionName.TASKS, total + len(upcoming), shown, line)

    def _deadlines(self, briefing, ctx, detail) -> None:
        items = [*ctx.deadlines_overdue, *ctx.deadlines_due, *ctx.deadlines_upcoming]
        if not items:
            return
        shown = items[: self._limit(detail, 2)]
        phrases = []
        for d in shown:
            if "overdue" in d.flags:
                phrases.append(f"the deadline {title(d)} passed {say_day(d.when, ctx.now, self._zone, with_time=False)} and is not marked done")
            else:
                phrases.append(f"the deadline {title(d)} is {say_day(d.when, ctx.now, self._zone)}")
        line = phrases[0][0].upper() + phrases[0][1:] + "".join(f"; {p}" for p in phrases[1:]) + "."
        if len(items) > len(shown):
            line += f" {num(len(items) - len(shown)).capitalize()} more {'is' if len(items) - len(shown) == 1 else 'are'} coming up."
        self._section(briefing, SectionName.DEADLINES, len(items), shown, line)

    def _reminders(self, briefing, ctx, detail) -> None:
        if not ctx.reminders:
            return
        n, first = len(ctx.reminders), ctx.reminders[0]
        line = (f"You have one reminder {_WINDOW_WORDS[ctx.window]}: {title(first)} {self._at(first, ctx)}." if n == 1 else
                f"You have {count(n, 'reminder')} {_WINDOW_WORDS[ctx.window]}; the next is {title(first)} {self._at(first, ctx)}.")
        self._section(briefing, SectionName.REMINDERS, n, ctx.reminders[: self._limit(detail, 1)], line)

    def _emails(self, briefing, ctx, detail) -> None:
        action, important = ctx.emails_action, ctx.emails_important
        if not action and not important:
            return
        parts = []
        if action:
            parts.append(f"{count(len(action), 'email')} {'appears' if len(action) == 1 else 'appear'} to need your attention")
        if important:
            parts.append(f"{count(len(important), 'email')} {'is' if len(important) == 1 else 'are'} marked important")
        line = (parts[0][0].upper() + parts[0][1:] + (f", and {parts[1]}" if len(parts) > 1 else "")) + "."
        shown = [*action, *important][: self._limit(detail, 0)]
        if detail is Detail.DETAILED and shown:
            line += " That is " + join_and([f"{title(e)} from {e.detail}" for e in shown]) + "."
        self._section(briefing, SectionName.EMAILS, len(action) + len(important), shown, line)

    def _messages(self, briefing, ctx, detail) -> None:
        if not ctx.messages:
            return
        n = len(ctx.messages)
        line = f"{count(n, 'message').capitalize()} {'appears' if n == 1 else 'appear'} to ask something of you."
        self._section(briefing, SectionName.MESSAGES, n, ctx.messages[: self._limit(detail, 0)], line)

    def _conflicts(self, briefing, ctx, detail, *, kinds: tuple[ConflictKind, ...] | None = None) -> None:
        conflicts = [c for c in ctx.conflicts if kinds is None or c.kind in kinds]
        if not conflicts:
            return
        phrases = []
        for c in conflicts[: self._limit(detail, 2)]:
            if c.kind is ConflictKind.DEADLINE_CLUSTER:
                phrases.append(f"two important deadlines fall close together: '{c.first_title}' and '{c.second_title}'")
            else:
                when = f" around {say_time(c.when, self._zone)}" if c.when else ""
                phrases.append(f"'{c.first_title}' and '{c.second_title}' overlap{when}")
        line = phrases[0][0].upper() + phrases[0][1:] + "".join(f"; {p}" for p in phrases[1:]) + ". I haven't changed anything."
        self._section(briefing, SectionName.CONFLICTS, len(conflicts), [], line)

    def _prepare(self, briefing, ctx, detail) -> None:
        if not ctx.preparation:
            return
        phrases = []
        for link in ctx.preparation[: self._limit(detail, 2)]:
            when = f" {say_day(link.event_when, ctx.now, self._zone)}" if link.event_when else ""
            verb = "you also have a task still pending" if link.basis in ("linked", "named") else "you also have a pending task that looks like preparation"
            phrases.append(f"before '{link.event_title}'{when}, {verb}: '{link.task_title}'")
        line = phrases[0][0].upper() + phrases[0][1:] + "".join(f". {p[0].upper() + p[1:]}" for p in phrases[1:]) + "."
        self._section(briefing, SectionName.PREPARATION, len(ctx.preparation), [], line)

    # -- views -------------------------------------------------------------------------------------------------------------------------

    def _overview(self, briefing, ctx, detail, day_part) -> None:
        self._schedule(briefing, ctx, detail)
        self._tasks(briefing, ctx, detail)
        self._deadlines(briefing, ctx, detail)
        self._reminders(briefing, ctx, detail)
        self._emails(briefing, ctx, detail)
        self._messages(briefing, ctx, detail)
        self._conflicts(briefing, ctx, detail)
        if detail is not Detail.QUICK:
            self._prepare(briefing, ctx, detail)
        if detail is Detail.QUICK:
            self._quicken(briefing, ctx)

    def _quicken(self, briefing: DailyBriefing, ctx: ProductivityContext) -> None:
        """A quick briefing is one sentence of counts."""
        pieces = []
        if ctx.events:
            pieces.append(count(len(ctx.events), "event") + f" {_WINDOW_WORDS[ctx.window]}")
        due = len(ctx.tasks_overdue) + len(ctx.tasks_due)
        if due:
            pieces.append(count(due, "task") + " to look at" + (f" ({num(len(ctx.tasks_overdue))} overdue)" if ctx.tasks_overdue else ""))
        dl = len(ctx.deadlines_overdue) + len(ctx.deadlines_due) + len(ctx.deadlines_upcoming)
        if dl:
            pieces.append(count(dl, "deadline"))
        mail = len(ctx.emails_action)
        if mail:
            pieces.append(count(mail, "email") + " that may need attention")
        if ctx.conflicts:
            pieces.append(count(len(ctx.conflicts), "scheduling conflict"))
        briefing.sections = [BriefingSection(name=SectionName.PRIORITIES, total=len(pieces), items=[], line=f"You have {join_and(pieces)}.")] if pieces else []

    def _schedule_view(self, briefing, ctx, detail, day_part) -> None:
        self._schedule(briefing, ctx, detail, day_part)
        self._conflicts(briefing, ctx, detail, kinds=(ConflictKind.OVERLAP, ConflictKind.ALL_DAY))
        if not briefing.sections and ctx.events_upcoming and not day_part:
            nxt = ctx.events_upcoming[0]
            self._section(briefing, SectionName.SCHEDULE, len(ctx.events_upcoming), [nxt], f"Nothing {_WINDOW_WORDS[ctx.window]}, and the next one is {title(nxt)} {say_day(nxt.when, ctx.now, self._zone)}.")

    def _tasks_view(self, briefing, ctx, detail, day_part) -> None:
        self._tasks(briefing, ctx, detail, include_upcoming=True)
        if ctx.completed_today:
            briefing.notes.append(f"You have completed {count(ctx.completed_today, 'task')} today.")

    def _deadlines_view(self, briefing, ctx, detail, day_part) -> None:
        self._deadlines(briefing, ctx, detail)

    def _priorities_view(self, briefing, ctx, detail, day_part) -> None:
        ranked = focus_candidates(ctx)[: self._limit(detail, 3)]
        if not ranked:
            return
        phrases = [f"{title(i)} ({short_reason(i)})" for i in ranked]
        self._section(briefing, SectionName.PRIORITIES, len(ranked), ranked, "Your most pressing items are " + join_and(phrases) + ".")

    def _focus_view(self, briefing, ctx, detail, day_part) -> None:
        pass  # the focus lines are added by build(); a focus answer has no sections of its own

    def _next_view(self, briefing, ctx, detail, day_part) -> None:
        now, zone = ctx.now, self._zone
        upcoming = sorted([e for e in (*ctx.events, *ctx.events_upcoming) if not e.all_day and e.when is not None and e.when >= now], key=lambda e: (e.when, e.key))
        if not upcoming:
            return
        nxt = upcoming[0]
        minutes = round((nxt.when - now).total_seconds() / 60)
        soon = f", in about {minutes} minute{'s' if minutes != 1 else ''}" if minutes < 120 else ""
        line = f"Your next event is {title(nxt)} {say_day(nxt.when, now, zone)}{soon}."
        after = [t for t in (*ctx.tasks_overdue, *ctx.tasks_due) if t.when is None or t.when >= (nxt.ends or nxt.when)][:2]
        if after:
            line += " Once it's over, " + join_and([f"{title(t)}" for t in after]) + (" is" if len(after) == 1 else " are") + " on your task list for today."
        self._section(briefing, SectionName.SCHEDULE, len(upcoming), [nxt], line)

    def _missed_view(self, briefing, ctx, detail, day_part) -> None:
        span = _WINDOW_WORDS[ctx.window]
        buckets = [
            ("task", [m for m in ctx.missed if m.kind is ItemKind.TASK], "became overdue"),
            ("reminder", [m for m in ctx.missed if m.kind is ItemKind.REMINDER], "went by without being delivered"),
            ("deadline", [m for m in ctx.missed if m.kind is ItemKind.DEADLINE], "passed without being marked done"),
        ]
        lines = []
        shown_all: list[BriefingItem] = []
        for noun, items, verb in buckets:
            if items:
                shown = items[: self._limit(detail, 2)]
                shown_all += shown
                names = join_and([title(i) for i in shown]) + (f" and {num(len(items) - len(shown))} more" if len(items) > len(shown) else "")
                lines.append(f"{count(len(items), noun).capitalize()} {verb} {span}: {names}.")
        mail = [m for m in ctx.missed if m.kind is ItemKind.EMAIL]
        if mail:
            shown_all += mail[: self._limit(detail, 2)]
            lines.append(f"{count(len(mail), 'email').capitalize()} that may need attention {'is' if len(mail) == 1 else 'are'} still unread.")
        if ctx.past_event_count:
            lines.append(f"Your calendar had {count(ctx.past_event_count, 'event')} {span}.")
        if lines:
            self._section(briefing, SectionName.MISSED, len(ctx.missed), shown_all, " ".join(lines))

    def _prepare_view(self, briefing, ctx, detail, day_part) -> None:
        self._prepare(briefing, ctx, detail)
