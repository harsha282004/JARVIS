"""Google Calendar tools: the only code that turns a validated CalendarAction into Google Calendar calls.

Same two-part shape as the other tools:
  resolve(args) -> Clarify | Ready   validates arguments and resolves times with the Phase 9/11 parsers. For update and
                                     cancel it also identifies the exact event with bounded, read-only, GET-only lookups so
                                     the permission can be bound to that exact event (see below). It changes nothing.
  run(**params) -> str               the Google Calendar call, reached only through Tool.execute after the PermissionManager
                                     authorized exactly these parameters.

Permission policy (also in docs/google-calendar-integration.md):
  calendar_list, calendar_events, calendar_search, calendar_get_event   LOW, no approval   read-only, bounded
  calendar_create_event, calendar_update_event, calendar_cancel_event   MEDIUM, approval    the user says "yes" first
The approval is bound to the resolved parameters: calendar id, event id, etag and the exact new values. Ids come only from
Google's own responses, never from the model. The identification lookups for update/cancel are read-only requests that
LOW-risk reads would allow without approval anyway; nothing is created, changed or deleted before the user's "yes".

Nothing is ever emailed: writes use sendUpdates=none, so guests are recorded but Google sends no invitation. Overlaps are
reported before creating or moving anything and are never resolved for the user. Replies contain calendar text (which can
come from a stranger's invitation), so the conversation history keeps only a placeholder for them.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo

from pydantic import BaseModel

from agent.events.dates import ResolvedWhen, WhenKind, resolve_when
from agent.events.temporal import EventScope, scope_window
from agent.tasks.formatting import format_clock, format_recurrence, format_when, local_day_bounds
from agent.tasks.matching import find_matches
from agent.tasks.models import Recurrence
from agent.tasks.recurrence import next_occurrence
from agent.tasks.tools import Clarify, Ready
from agent.tasks.timeparse import TimeParseError, TimeParser, extract_time_of_day
from agent.tools.base import Tool
from backend.core.security import PermissionScope, RiskLevel
from integrations.calendar.intents import (
    CalendarActionName,
    CalendarCancelEventArgs,
    CalendarCreateEventArgs,
    CalendarEventsArgs,
    CalendarGetEventArgs,
    CalendarListArgs,
    CalendarSearchArgs,
    CalendarUpdateEventArgs,
)
from integrations.calendar.models import (
    CalendarError,
    CalendarEvent,
    CalendarEventDraft,
    CalendarEventPatch,
    CalendarInfo,
    valid_email,
)
from integrations.calendar.parser import clean
from integrations.calendar.rrule import build_rrule, describe_rrule, is_endless
from integrations.calendar.service import ABSOLUTE_MAX_RESULTS, CalendarService
from integrations.calendar.sync import CalendarEventSync
from integrations.gmail.text import one_line

MAX_SPOKEN = 5
SEARCH_DAYS = 90
MAX_ATTENDEES = 10
DAY_PARTS = {"morning": (5, 12), "afternoon": (12, 17), "evening": (17, 22)}  # local hours, documented
PLACEHOLDER = "[Calendar information was read to the user. Calendar text is deliberately not kept in the conversation history.]"
_FILLER = {"with", "and", "for", "the", "my", "a", "an", "to", "at", "on", "in", "of", "from", "event", "events", "calendar"}
_GENERIC = {"meeting", "meetings", "appointment", "call", "session"}


@dataclass(frozen=True)
class CalendarToolContext:
    service: CalendarService
    parser: TimeParser
    clock: Any  # Callable[[], datetime]
    sync: CalendarEventSync | None = None
    lookahead_days: int = 7

    def now(self) -> datetime:
        return self.clock()

    @property
    def zone(self) -> ZoneInfo:
        return self.service.zone


def _iso(value: datetime | None) -> str | None:
    return value.astimezone(timezone.utc).isoformat() if value else None


def _from_iso(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


# ---- speaking -----------------------------------------------------------------------------------------------------------


def when_text(event: CalendarEvent, now: datetime, zone: ZoneInfo) -> str:
    if event.all_day:
        first = event.start.astimezone(zone)
        last = (event.end - timedelta(seconds=1)).astimezone(zone)
        text = format_when(first, now, zone, with_time=False)
        if last.date() > first.date():
            text += " through " + format_when(last, now, zone, with_time=False)
        return text + ", all day"
    text = format_when(event.start, now, zone)
    if event.end > event.start:
        end = event.end.astimezone(zone)
        text += f" until {format_clock(end.hour, end.minute)}" if end.date() == event.start.astimezone(zone).date() else f" until {format_when(event.end, now, zone)}"
    return text


def describe(event: CalendarEvent, now: datetime, zone: ZoneInfo, calendars: dict[str, CalendarInfo] | None = None) -> str:
    bits = [when_text(event, now, zone)]
    if event.location:
        bits.append("at " + one_line(event.location, 60))
    if event.recurring_event_id or event.recurrence:
        bits.append("repeats")
    if event.status == "tentative":
        bits.append("tentative")
    info = (calendars or {}).get(event.calendar_id)
    if info is not None and not info.primary and len(calendars or {}) > 1:
        bits.append("on " + one_line(info.summary, 40))
    return f"{one_line(event.summary or '(no title)', 80)} ({', '.join(bits)})"


def _spoken(items: list[str]) -> str:
    shown = items[:MAX_SPOKEN]
    return "; ".join(shown) + (f"; and {len(items) - len(shown)} more" if len(items) > len(shown) else "")


def _overlap_text(events: list[CalendarEvent], now: datetime, zone: ZoneInfo) -> str:
    return "; ".join(f"{one_line(e.summary or '(no title)', 60)} ({when_text(e, now, zone)})" for e in events[:2])


class CalendarTool(Tool, ABC):
    allowed_scopes = (PermissionScope.ONE_TIME,)
    action: CalendarActionName
    history_placeholder = PLACEHOLDER
    prompt_history_placeholder = None  # set by tools whose confirmation question names an existing (external) event

    def __init__(self, context: CalendarToolContext):
        self._ctx = context
        self.name = self.action.value

    @abstractmethod
    def resolve(self, args: BaseModel) -> Clarify | Ready:
        raise NotImplementedError

    # ---- shared helpers ------------------------------------------------------------------------------------------

    def _phrase(self, phrase: str, parser: TimeParser | None = None, reference: datetime | None = None) -> ResolvedWhen | Clarify:
        ctx = self._ctx
        resolved = resolve_when(parser or ctx.parser, phrase, reference or ctx.now())
        if resolved.kind is WhenKind.AMBIGUOUS:
            return Clarify(resolved.question or "Which date do you mean?")
        if not resolved.is_resolved or resolved.value is None:
            return Clarify("I couldn't understand that date or time. Could you say it like 'Friday at 3 PM' or 'October 5'?")
        return resolved

    def _day_window(self, phrase: str) -> tuple[datetime, datetime, str] | Clarify:
        ctx, now = self._ctx, self._ctx.now()
        resolved = self._phrase(phrase)
        if isinstance(resolved, Clarify):
            return resolved
        start, end = local_day_bounds(0, resolved.value, ctx.zone)  # type: ignore[arg-type]
        return start, end, format_when(resolved.value, now, ctx.zone, with_time=False)  # type: ignore[arg-type]

    def _calendars_for(self, name: str | None, *, writable: bool = False) -> list[CalendarInfo] | Clarify:
        """The calendars to use: the named one, or (no name) every selected one (writable ones only for changes)."""
        service = self._ctx.service
        if name:
            found = service.find_calendar(name, writable=writable)
            if not found:
                return Clarify(f"I couldn't find {'a writable ' if writable else 'a '}calendar called '{one_line(name, 40)}'.")
            if len(found) > 1:
                return Clarify(f"I found {len(found)} calendars that could match: {_spoken([one_line(c.summary, 40) for c in found])}. Which one do you mean?")
            return found
        chosen = service.selected_calendars()
        return [c for c in chosen if c.writable] if writable else chosen

    def _identify(self, query: str, on: str | None, calendar: str | None, *, writable: bool) -> CalendarEvent | Clarify:
        """One exact event: bounded GET-only lookups, local word matching, ask when unclear. Never a model id."""
        ctx, now = self._ctx, self._ctx.now()
        calendars = self._calendars_for(calendar, writable=writable)
        if isinstance(calendars, Clarify):
            return calendars
        if on:
            window = self._day_window(on)
            if isinstance(window, Clarify):
                return window
            start, end = max(window[0], now - timedelta(hours=12)), window[1]
        else:
            start, end = now, now + timedelta(days=SEARCH_DAYS)
        listing = ctx.service.events_between(start, end, calendars=calendars, limit=ABSOLUTE_MAX_RESULTS)
        candidates = [e for e in listing.events if e.end >= now or e.all_day]
        matches = _match(query, candidates)
        if not matches:
            return Clarify("I couldn't find a matching event on your calendar.")
        series = {e.recurring_event_id for e in matches}
        if len(matches) > 1 and len(series) == 1 and None not in series:
            return matches[0]  # occurrences of one repeating event: the next one is meant
        if len(matches) > 1:
            info = {c.calendar_id: c for c in calendars}
            return Clarify(f"I found {len(matches)} events that could match: {_spoken([describe(e, now, ctx.zone, info) for e in matches])}. "
                           "Which one do you mean? Add the day or a word from the title.")
        return matches[0]


def _match(query: str, events: list[CalendarEvent]) -> list[CalendarEvent]:
    def text(e: CalendarEvent) -> str:
        return f"{e.summary} {e.location} {' '.join(a.display for a in e.attendees[:10])}"

    words = [w for w in query.lower().split() if w.strip(".,!?'\"") not in _FILLER]
    for attempt in (words, [w for w in words if w not in _GENERIC]):
        if not attempt:
            continue
        keyed = [(f"{e.calendar_id}|{e.event_id}", text(e)) for e in events]
        ids = set(find_matches(" ".join(attempt), keyed))
        found = [e for e in events if f"{e.calendar_id}|{e.event_id}" in ids]
        if found:
            return found
    return []


# ---- reading --------------------------------------------------------------------------------------------------------------


class CalendarListTool(CalendarTool):
    action = CalendarActionName.LIST
    description = "List the user's Google calendars (names, which is primary, which are read-only)."
    input_schema: dict[str, str] = {}
    requires_permission = False
    risk = RiskLevel.LOW

    def resolve(self, args: CalendarListArgs) -> Ready:
        return Ready({})

    def run(self, *, origin_session: str | None = None) -> str:
        calendars = self._ctx.service.calendars(refresh=True)
        if not calendars:
            return "I couldn't find any calendars on your Google account."
        items = [f"{one_line(c.summary or c.calendar_id, 40)}{' (primary)' if c.primary else ''}{'' if c.writable else ' (read-only)'}" for c in calendars]
        return f"You have {len(calendars)} calendar{'s' if len(calendars) != 1 else ''}: {_spoken(items)}."


class CalendarEventsTool(CalendarTool):
    action = CalendarActionName.EVENTS
    description = "Read the user's Google Calendar for a period: today, tomorrow, this week, a day, a range, or what is coming up."
    input_schema = {
        "scope": "upcoming | today | tomorrow | this_week | next_week | next_7_days (default upcoming)",
        "on": "string: one day as the user said it (e.g. 'Friday', 'December 12')",
        "start": "string: first day of a period", "end": "string: last day of a period",
        "day_part": "morning | afternoon | evening, optional",
        "calendar": "string: a calendar NAME (default: all the user's calendars)",
        "conflicts": "boolean: also say whether the events overlap", "next": "boolean: only the next event",
    }
    requires_permission = False
    risk = RiskLevel.LOW

    def resolve(self, args: CalendarEventsArgs) -> Clarify | Ready:
        ctx, now = self._ctx, self._ctx.now()
        label = args.scope.replace("_", " ")
        if args.on:
            window = self._day_window(args.on)
            if isinstance(window, Clarify):
                return window
            start, end, label = window
        elif args.start:
            first = self._day_window(args.start)
            last = self._day_window(args.end or args.start)
            if isinstance(first, Clarify):
                return first
            if isinstance(last, Clarify):
                return last
            if last[1] < first[0]:
                return Clarify("The end of that period is before its start. What period do you mean?")
            start, end, label = first[0], last[1], f"from {first[2]} to {last[2]}" if args.end else first[2]
            if (end - start) > timedelta(days=62):
                return Clarify("That period is too long for me to read at once. Try two months or less.")
        else:
            window = scope_window(EventScope(args.scope), now, ctx.zone, ctx.lookahead_days)
            assert window is not None
            start, end = window
            if args.scope in ("upcoming", "next_7_days", "this_week"):
                start = max(start, now)
            label = {"upcoming": f"in the next {ctx.lookahead_days} days", "today": "today", "tomorrow": "tomorrow", "this_week": "this week",
                     "next_week": "next week", "next_7_days": "in the next 7 days"}[args.scope]
        return Ready({"start": _iso(start), "end": _iso(end), "label": label, "day_part": args.day_part, "calendar": args.calendar,
                      "conflicts": args.conflicts, "next": args.next})

    def run(self, *, start: str, end: str, label: str, day_part: str | None, calendar: str | None, conflicts: bool,
            next: bool, origin_session: str | None = None) -> str:
        ctx, now = self._ctx, self._ctx.now()
        chosen = self._calendars_for(calendar)
        if isinstance(chosen, Clarify):
            return chosen.message
        if ctx.sync is not None:
            ctx.sync.reconcile()  # bounded, throttled: keeps mapped Phase 11 records honest
        listing = ctx.service.events_between(_from_iso(start), _from_iso(end), calendars=chosen)  # type: ignore[arg-type]
        events = _in_day_part(listing.events, day_part, ctx.zone)
        info = {c.calendar_id: c for c in chosen}
        where = "on your calendar" if not calendar else f"on your {one_line(calendar, 30)} calendar"
        if next:
            upcoming = [e for e in events if e.end >= now]
            if not upcoming:
                return f"You have nothing coming up {where}."
            return f"Your next event is {describe(upcoming[0], now, ctx.zone, info)}."
        part = f" this {day_part}" if day_part else ""
        if not events:
            text = f"You have nothing {where} {label}{part}."
        else:
            n = len(events)
            text = f"You have {n} event{'s' if n != 1 else ''} {where} {label}{part}: " + _spoken([describe(e, now, ctx.zone, info) for e in events]) + "."
            if listing.truncated:
                text += f" I only read the first {ctx.service.max_results}, so there may be more."
        if conflicts:
            found = ctx.service.conflicts_among(events)
            if found:
                first = found[0]
                kind = "are on the same day (one is all day)" if first.kind.value == "all_day" else "overlap"
                text += f" Heads up: {one_line(first.first.title, 50)} and {one_line(first.second.title, 50)} {kind}."
                if len(found) > 1:
                    text += f" There are {len(found) - 1} more conflict{'s' if len(found) > 2 else ''}."
                text += " I haven't changed anything."
            else:
                text += " None of them overlap."
        return text


def _in_day_part(events: list[CalendarEvent], day_part: str | None, zone: ZoneInfo) -> list[CalendarEvent]:
    if not day_part:
        return events
    lo, hi = DAY_PARTS[day_part]
    kept = []
    for event in events:
        day = event.start.astimezone(zone).date()
        last_day = (max(event.end, event.start) - timedelta(seconds=1)).astimezone(zone).date()
        d = day
        while d <= last_day and d <= day + timedelta(days=14):
            start = datetime.combine(d, time(lo), tzinfo=zone)
            end = datetime.combine(d, time(hi), tzinfo=zone)
            if event.all_day or (event.start < end and max(event.end, event.start + timedelta(minutes=1)) > start):
                kept.append(event)
                break
            d += timedelta(days=1)
    return kept


class CalendarSearchTool(CalendarTool):
    action = CalendarActionName.SEARCH
    description = "Search the user's Google Calendar by words (title, description, location, guests), e.g. 'interview' or 'project'."
    input_schema = {
        "query": "string: words to search for", "start": "string: first day of a period (optional)", "end": "string: last day",
        "days": "integer 1-365: how far ahead to look (default 90)", "calendar": "string: a calendar NAME",
        "next": "boolean: only the next matching event",
    }
    requires_permission = False
    risk = RiskLevel.LOW

    def resolve(self, args: CalendarSearchArgs) -> Clarify | Ready:
        ctx, now = self._ctx, self._ctx.now()
        if args.start:
            first, last = self._day_window(args.start), self._day_window(args.end or args.start)
            for w in (first, last):
                if isinstance(w, Clarify):
                    return w
            start, end = first[0], last[1]  # type: ignore[index]
        else:
            start, end = now, now + timedelta(days=args.days or SEARCH_DAYS)
        if end <= start or (end - start) > timedelta(days=366):
            return Clarify("I can search up to a year at a time. What period do you mean?")
        return Ready({"query": args.query, "start": _iso(start), "end": _iso(end), "calendar": args.calendar, "next": args.next})

    def run(self, *, query: str, start: str, end: str, calendar: str | None, next: bool, origin_session: str | None = None) -> str:
        ctx, now = self._ctx, self._ctx.now()
        chosen = self._calendars_for(calendar)
        if isinstance(chosen, Clarify):
            return chosen.message
        text = clean(query, 100)
        listing = ctx.service.events_between(_from_iso(start), _from_iso(end), calendars=chosen, query=text)  # Google's own text search
        events = [e for e in listing.events if e.end >= now or e.all_day]
        if not events:
            return "I didn't find any events on your calendar matching that."
        info = {c.calendar_id: c for c in chosen}
        if next:
            return f"Your next matching event is {describe(events[0], now, ctx.zone, info)}."
        return f"I found {len(events)}: " + _spoken([describe(e, now, ctx.zone, info) for e in events]) + "." + (
            f" I only read the first {ctx.service.max_results}." if listing.truncated else "")


class CalendarGetEventTool(CalendarTool):
    action = CalendarActionName.GET_EVENT
    description = "Details of one calendar event: when, where, guests, meeting link, repeats. Describe it in words; ids are never used."
    input_schema = {"query": "string: words identifying the event", "on": "string: the day it is on (optional)", "calendar": "string: a calendar NAME"}
    requires_permission = False
    risk = RiskLevel.LOW

    def resolve(self, args: CalendarGetEventArgs) -> Ready:
        return Ready({"query": args.query, "on": args.on, "calendar": args.calendar})

    def run(self, *, query: str, on: str | None, calendar: str | None, origin_session: str | None = None) -> str:
        ctx, now = self._ctx, self._ctx.now()
        found = self._identify(query, on, calendar, writable=False)
        if isinstance(found, Clarify):
            return found.message
        e = found
        info = ctx.service.calendar_by_id(e.calendar_id)
        parts = [f"{one_line(e.summary or '(no title)', 100)}: {when_text(e, now, ctx.zone)}."]
        if e.location:
            parts.append(f"Location: {one_line(e.location, 100)}.")
        if e.attendees:
            names = ", ".join(one_line(a.display, 40) for a in e.attendees[:MAX_SPOKEN])
            parts.append(f"{len(e.attendees)} guest{'s' if len(e.attendees) != 1 else ''}: {names}{' and others' if len(e.attendees) > MAX_SPOKEN else ''}.")
        if e.recurrence:
            parts.append("It repeats " + "; ".join(describe_rrule(r) for r in e.recurrence if r.startswith("RRULE")) + ".")
        elif e.recurring_event_id:
            parts.append("It is one occurrence of a repeating event.")
        if e.meeting_link:
            parts.append("It has a video meeting link.")
        if e.status == "tentative":
            parts.append("It is marked tentative.")
        if info is not None and not info.primary:
            parts.append(f"It's on your {one_line(info.summary, 40)} calendar.")
        if e.description:
            parts.append("Notes: " + one_line(e.description, 200))
        return " ".join(parts)


# ---- create ----------------------------------------------------------------------------------------------------------------


def _time_only(phrase: str) -> tuple[int, int] | None:
    found, rest = extract_time_of_day(phrase)
    return found if found is not None and not " ".join(w for w in rest.split() if w not in ("until", "to", "till", "at", "by", "-")) else None


class CalendarCreateEventTool(CalendarTool):
    action = CalendarActionName.CREATE_EVENT
    description = (
        "Create an event on the user's Google Calendar (asks the user to confirm first). Needs a title and a start; a timed event "
        "also needs an end or duration (ask if missing); all_day=true only for full-day things (festival, holiday, birthday). "
        "Guests only from e-mail addresses the user gave. Nobody is emailed."
    )
    input_schema = {
        "title": "string, required", "start": "string, required: date/time exactly as said ('Friday at 10 AM', 'October 10')",
        "end": "string: end time as said ('until 4 PM')", "duration_minutes": "integer, optional (never assumed)",
        "all_day": "boolean: true only for a full-day event", "timezone": "IANA name, optional (default: the user's)",
        "location": "string, optional (never invented)", "description": "string, optional",
        "attendees": "list of e-mail addresses the user GAVE (never guessed from a name)",
        "recurrence": "string, only if the user asked for repeating: 'every Monday at 10 AM'",
        "repeat_count": "integer, optional", "repeat_until": "string, optional: last day",
        "calendar": "string: a calendar NAME (default: primary)", "allow_conflict": "boolean: only if the user chose to create despite an overlap",
    }
    requires_permission = True
    risk = RiskLevel.MEDIUM

    def resolve(self, args: CalendarCreateEventArgs) -> Clarify | Ready:
        ctx, now = self._ctx, self._ctx.now()
        zone = ZoneInfo(args.timezone) if args.timezone else ctx.zone
        parser = ctx.parser if zone == ctx.zone else TimeParser(zone)
        title = clean(args.title, 300)
        if not title:
            return Clarify("What should I call the event?")

        guests = self._guests(args.attendees)
        if isinstance(guests, Clarify):
            return guests

        calendar_id, calendar_name = "primary", "primary"
        try:  # the primary calendar's real id keeps the Phase 11 mapping id identical to what listings and updates use
            primary = ctx.service.find_calendar(None, writable=True)
            calendar_id = primary[0].calendar_id if primary else calendar_id
        except CalendarError:
            pass
        if args.calendar:
            found = self._calendars_for(args.calendar, writable=True)
            if isinstance(found, Clarify):
                return found
            calendar_id, calendar_name = found[0].calendar_id, found[0].summary

        rule: list[str] = []
        if args.recurrence:
            built = self._recurrence(args, parser, zone, now)
            if isinstance(built, Clarify):
                return built
            rule, start = built
            if args.all_day:
                return Clarify("Repeating all-day events aren't supported yet. Tell me a start time for the repeating event, or make it a single all-day event.")
        else:
            resolved = self._phrase(args.start, parser)
            if isinstance(resolved, Clarify):
                return resolved
            if not resolved.has_time and not args.all_day:
                return Clarify(f"What time should it start {format_when(resolved.value, now, zone, with_time=False)}?")  # type: ignore[arg-type]
            start = resolved.value  # type: ignore[assignment]

        if args.all_day:
            first = datetime.combine(start.date(), time(0, 0), tzinfo=zone)
            last_day = first.date()
            if args.end:
                stop = self._phrase(args.end, parser, first)
                if isinstance(stop, Clarify):
                    return stop
                last_day = stop.value.date()  # type: ignore[union-attr]
                if last_day < first.date():
                    return Clarify("That end date is before the start. What are the dates?")
            start, end = first, datetime.combine(last_day + timedelta(days=1), time(0, 0), tzinfo=zone)
            if end <= now:
                return Clarify("That date has already passed. What is the correct date?")
        else:
            if start < now:
                return Clarify("That time has already passed. What is the correct date and time?")
            end = self._end(args, start, parser, zone)
            if isinstance(end, Clarify):
                return end
        return Ready({
            "calendar_id": calendar_id, "calendar_name": calendar_name, "event_id": uuid4().hex, "summary": title,
            "start": _iso(start), "end": _iso(end), "all_day": args.all_day, "timezone": zone.key,
            "location": clean(args.location, 300) if args.location else "", "description": clean(args.description, 500) if args.description else "",
            "attendees": guests, "recurrence": rule, "allow_conflict": args.allow_conflict,
        }, self._prompt(title, start, end, args.all_day, zone, now, calendar_name, args.location, guests, rule))

    # -- resolve helpers --

    @staticmethod
    def _guests(raw: list[str]) -> list[str] | Clarify:
        guests: list[str] = []
        for item in raw[:MAX_ATTENDEES + 1]:
            address = item.strip().strip("<>")
            if valid_email(address):
                if address.lower() not in (g.lower() for g in guests):
                    guests.append(address)
            elif "@" in address:
                return Clarify(f"'{one_line(address, 40)}' doesn't look like a valid email address. Could you say it again?")
            else:
                return Clarify(f"I don't have an email address for '{one_line(address, 40)}', and I won't guess one. "
                               "Give me an email address to add them as a guest, or I can create the event without guests.")
        if len(guests) > MAX_ATTENDEES:
            return Clarify(f"That's more than {MAX_ATTENDEES} guests. Please give me fewer.")
        return guests

    def _recurrence(self, args, parser: TimeParser, zone: ZoneInfo, now: datetime) -> tuple[list[str], datetime] | Clarify:
        try:
            rec: Recurrence | None = parser.parse_recurrence(args.recurrence)
            if rec is None and args.start:
                rec = parser.parse_recurrence(f"{args.recurrence} {args.start}")
        except TimeParseError as exc:
            return Clarify(str(exc))
        if rec is None:
            return Clarify("I couldn't understand how often it should repeat. Try 'every Monday at 10 AM'.")
        first = next_occurrence(rec, now, zone)
        start = first
        resolved = resolve_when(parser, args.start, now) if args.start else None
        if resolved is not None and resolved.kind is WhenKind.EXACT and resolved.value is not None:
            if next_occurrence(rec, resolved.value - timedelta(seconds=1), zone) == resolved.value.astimezone(timezone.utc):
                start = resolved.value.astimezone(timezone.utc)
            else:
                return Clarify("That date isn't one of the repeating days. Which date should the first one be?")
        until = None
        if args.repeat_until:
            window = self._day_window(args.repeat_until)
            if isinstance(window, Clarify):
                return window
            until = window[1] - timedelta(seconds=1)
        try:
            rule = build_rrule(rec, count=args.repeat_count, until=until)
        except ValueError:
            return Clarify("I couldn't build that repeat schedule. How many times, or until when?")
        return [rule], start.astimezone(zone)

    def _end(self, args, start: datetime, parser: TimeParser, zone: ZoneInfo) -> datetime | Clarify:
        if args.duration_minutes:
            return start + timedelta(minutes=args.duration_minutes)
        if args.end:
            clock = _time_only(args.end)
            if clock is not None:
                end = datetime.combine(start.astimezone(zone).date(), time(*clock), tzinfo=zone)
            else:
                resolved = self._phrase(args.end, parser, start)
                if isinstance(resolved, Clarify):
                    return resolved
                if not resolved.has_time:
                    return Clarify("What time should it end?")
                end = resolved.value  # type: ignore[assignment]
            if end <= start:
                return Clarify("The end has to be after the start. When should it end?")
            return end
        kind = "meeting" if any(w in args.title.lower() for w in ("meeting", "call", "sync", "review", "interview")) else "event"
        return Clarify(f"How long should the {kind} be?")  # a duration is never assumed

    def _prompt(self, title, start, end, all_day, zone, now, calendar_name, location, guests, rule) -> str:
        when = format_when(start, now, zone, with_time=not all_day) + (", all day" if all_day else "")
        if not all_day and end > start:
            local_end = end.astimezone(zone)
            when += f" until {format_clock(local_end.hour, local_end.minute)}"
        text = f"Do you want me to create '{one_line(title, 60)}' on your Google Calendar ({one_line(calendar_name, 30)}) {when}"
        if location:
            text += f" at {one_line(location, 50)}"
        if guests:
            text += f", with {len(guests)} guest{'s' if len(guests) != 1 else ''} ({', '.join(guests[:3])}); no invitation email will be sent"
        if rule:
            text += f", repeating {describe_rrule(rule[0])}" + (" with no end date" if is_endless(rule[0]) else "")
        return text + "? Say yes to confirm."

    def run(self, *, calendar_id: str, calendar_name: str, event_id: str, summary: str, start: str, end: str, all_day: bool,
            timezone: str, location: str, description: str, attendees: list[str], recurrence: list[str], allow_conflict: bool,
            origin_session: str | None = None) -> str:
        ctx, now = self._ctx, self._ctx.now()
        start_dt, end_dt = _from_iso(start), _from_iso(end)
        assert start_dt is not None and end_dt is not None
        if not allow_conflict and not recurrence:
            clashes = ctx.service.conflicting_events(start_dt, end_dt, all_day)
            if clashes:
                return (f"That overlaps with {_overlap_text(clashes, now, ctx.zone)}. I haven't created it, and I won't move or change anything. "
                        "Tell me a different time, or say 'create it anyway'.")
        draft = CalendarEventDraft(event_id=event_id, summary=summary, start=start_dt, end=end_dt, all_day=all_day, timezone=timezone,
                                   location=location, description=description, attendees=attendees, recurrence=recurrence)
        created = ctx.service.create_event(calendar_id, draft)
        if ctx.sync is not None:
            ctx.sync.record_created(created)
        text = f"Okay, I've created '{one_line(created.summary, 60)}' on your calendar: {when_text(created, now, ctx.zone)}"
        if recurrence:
            text += f", repeating {describe_rrule(recurrence[0])}"
        text += "."
        if attendees:
            text += f" I added {len(attendees)} guest{'s' if len(attendees) != 1 else ''}, but Google sent no invitation emails."
        return text


# ---- update / cancel ---------------------------------------------------------------------------------------------------------------


class CalendarUpdateEventTool(CalendarTool):
    action = CalendarActionName.UPDATE_EVENT
    description = (
        "Change an existing Google Calendar event (asks the user to confirm first): rename, move it to a new day/time, change its "
        "length, location or notes. Describe the event in words; ids are never used. A moved event keeps its length."
    )
    input_schema = {
        "query": "string, required: words identifying the event", "on": "string: the day it is on", "calendar": "string: a calendar NAME",
        "new_title": "string", "start": "string: the NEW start as said ('4 PM', 'Friday', 'Friday at 4 PM')",
        "end": "string: new end", "duration_minutes": "integer", "location": "string: new location", "clear_location": "boolean",
        "description": "string", "whole_series": "boolean: every occurrence of a repeating event (title/location/notes only)",
        "allow_conflict": "boolean: only if the user chose to move it despite an overlap",
    }
    prompt_history_placeholder = PLACEHOLDER
    requires_permission = True
    risk = RiskLevel.MEDIUM

    def resolve(self, args: CalendarUpdateEventArgs) -> Clarify | Ready:
        ctx, now, zone = self._ctx, self._ctx.now(), self._ctx.zone
        found = self._identify(args.query, args.on, args.calendar, writable=True)
        if isinstance(found, Clarify):
            return found
        event = found
        target_id, etag = event.event_id, event.etag
        time_change = bool(args.start or args.end or args.duration_minutes)
        if args.whole_series and event.is_recurring:
            if time_change:
                return Clarify("I can change one occurrence's time, but changing the time of every repeat isn't supported yet. "
                               "Tell me which occurrence to move, or change only the title, location or notes for the whole series.")
            master = event.recurring_event_id or event.event_id
            try:
                current = ctx.service.get_event(event.calendar_id, master)
            except Exception:  # noqa: BLE001 - the master could not be read: do not guess
                return Clarify("I couldn't reach the repeating event itself, so I haven't changed anything.")
            target_id, etag = current.event_id, current.etag

        new_start, new_end = event.start, event.end
        if time_change:
            computed = self._new_times(event, args, zone, now)
            if isinstance(computed, Clarify):
                return computed
            new_start, new_end = computed

        changes: list[str] = []
        if args.new_title:
            changes.append(f"rename it to '{one_line(args.new_title, 60)}'")
        if time_change:
            probe = event.model_copy(update={"start": new_start, "end": new_end})
            changes.append("move it to " + when_text(probe, now, zone))
        if args.clear_location:
            changes.append("clear the location")
        elif args.location:
            changes.append(f"set the location to {one_line(args.location, 60)}")
        if args.description:
            changes.append("update the notes")
        info = ctx.service.calendar_by_id(event.calendar_id)
        where = f" on your {one_line(info.summary, 30)} calendar" if info is not None and not info.primary else ""
        scope = " (every occurrence)" if args.whole_series and event.is_recurring else " (this occurrence only)" if event.is_recurring else ""
        prompt = f"Do you want me to {', and '.join(changes)} for {describe(event, now, zone)}{where}{scope}? Say yes to confirm."
        return Ready({
            "calendar_id": event.calendar_id, "event_id": target_id, "etag": etag,
            "summary": clean(args.new_title, 300) if args.new_title else None,
            "start": _iso(new_start) if time_change else None, "end": _iso(new_end) if time_change else None,
            "all_day": event.all_day, "location": "" if args.clear_location else (clean(args.location, 300) if args.location else None),
            "description": clean(args.description, 500) if args.description else None, "timezone": zone.key,
            "allow_conflict": args.allow_conflict, "has_guests": bool(event.attendees),
        }, prompt)

    def _new_times(self, event: CalendarEvent, args, zone: ZoneInfo, now: datetime) -> tuple[datetime, datetime] | Clarify:
        old_start = event.start.astimezone(zone)
        new_start = event.start
        if args.start:
            clock = _time_only(args.start)
            if clock is not None and not event.all_day:
                new_start = datetime.combine(old_start.date(), time(*clock), tzinfo=zone)  # "to 4 PM": same day, new time
            else:
                resolved = self._phrase(args.start)
                if isinstance(resolved, Clarify):
                    return resolved
                value = resolved.value.astimezone(zone)  # type: ignore[union-attr]
                if resolved.has_time:
                    new_start = value
                elif event.all_day:
                    new_start = datetime.combine(value.date(), time(0, 0), tzinfo=zone)
                else:  # "Move it to Friday": the day changes, the time of day stays
                    new_start = datetime.combine(value.date(), old_start.time(), tzinfo=zone)
        if event.all_day:
            new_end = new_start + (event.end - event.start)
            return new_start, new_end
        duration = event.end - event.start
        if args.duration_minutes:
            new_end = new_start + timedelta(minutes=args.duration_minutes)
        elif args.end:
            clock = _time_only(args.end)
            if clock is None:
                return Clarify("Tell me the new end as a time, like 'until 4 PM'.")
            new_end = datetime.combine(new_start.astimezone(zone).date(), time(*clock), tzinfo=zone)
        else:
            new_end = new_start + duration  # a moved event keeps its length
        if new_end <= new_start:
            return Clarify("The end has to be after the start. What should the new times be?")
        if new_start < now and new_start != event.start:
            return Clarify("That time has already passed. What is the correct date and time?")
        return new_start, new_end

    def run(self, *, calendar_id: str, event_id: str, etag: str | None, summary: str | None, start: str | None, end: str | None,
            all_day: bool, location: str | None, description: str | None, timezone: str, allow_conflict: bool, has_guests: bool,
            origin_session: str | None = None) -> str:
        ctx, now = self._ctx, self._ctx.now()
        start_dt, end_dt = _from_iso(start), _from_iso(end)
        if start_dt and end_dt and not allow_conflict:
            clashes = ctx.service.conflicting_events(start_dt, end_dt, all_day, exclude=(calendar_id, event_id))
            if clashes:
                return (f"That would overlap with {_overlap_text(clashes, now, ctx.zone)}. I haven't changed anything. "
                        "Tell me a different time, or say 'move it anyway'.")
        patch = CalendarEventPatch(summary=summary, start=start_dt, end=end_dt, location=location, description=description,
                                   timezone=timezone if start_dt else None)
        updated = ctx.service.update_event(calendar_id, event_id, patch, etag)
        if ctx.sync is not None:
            ctx.sync.record_updated(updated)
        text = f"Okay, I've updated it: {describe(updated, now, ctx.zone)}."
        if has_guests:
            text += " Google sent no notification to the guests."
        return text


class CalendarCancelEventTool(CalendarTool):
    action = CalendarActionName.CANCEL_EVENT
    description = (
        "Delete an event from the user's Google Calendar (asks the user to confirm first). Describe it in words; ids are never "
        "used. For a repeating event only the one occurrence is removed unless whole_series is true."
    )
    input_schema = {"query": "string, required: words identifying the event", "on": "string: the day it is on (e.g. 'tomorrow')",
                    "calendar": "string: a calendar NAME", "whole_series": "boolean: every occurrence of a repeating event"}
    prompt_history_placeholder = PLACEHOLDER
    requires_permission = True
    risk = RiskLevel.MEDIUM

    def resolve(self, args: CalendarCancelEventArgs) -> Clarify | Ready:
        ctx, now, zone = self._ctx, self._ctx.now(), self._ctx.zone
        found = self._identify(args.query, args.on, args.calendar, writable=True)
        if isinstance(found, Clarify):
            return found
        event = found
        target_id, etag, scope = event.event_id, event.etag, ""
        if event.is_recurring:
            if args.whole_series:
                master = event.recurring_event_id or event.event_id
                try:
                    current = ctx.service.get_event(event.calendar_id, master)
                except Exception:  # noqa: BLE001
                    return Clarify("I couldn't reach the repeating event itself, so I haven't changed anything.")
                target_id, etag, scope = current.event_id, current.etag, " This deletes EVERY occurrence of the repeating event."
            else:
                scope = " Only this occurrence will be removed."
        info = ctx.service.calendar_by_id(event.calendar_id)
        where = f" from your {one_line(info.summary, 30)} calendar" if info is not None and not info.primary else " from your Google Calendar"
        prompt = f"Do you want me to cancel {describe(event, now, zone)}{where}?{scope} It will be deleted. Say yes to confirm."
        return Ready({"calendar_id": event.calendar_id, "event_id": target_id, "etag": etag, "has_guests": bool(event.attendees)}, prompt)

    def run(self, *, calendar_id: str, event_id: str, etag: str | None, has_guests: bool, origin_session: str | None = None) -> str:
        ctx = self._ctx
        ctx.service.delete_event(calendar_id, event_id, etag)
        if ctx.sync is not None:
            ctx.sync.record_cancelled(calendar_id, event_id)
        text = "Okay, I've deleted it from your Google Calendar."
        if has_guests:
            text += " Google sent no cancellation notices to the guests."
        return text


def build_calendar_tools(context: CalendarToolContext) -> list[CalendarTool]:
    return [
        CalendarListTool(context), CalendarEventsTool(context), CalendarSearchTool(context), CalendarGetEventTool(context),
        CalendarCreateEventTool(context), CalendarUpdateEventTool(context), CalendarCancelEventTool(context),
    ]
