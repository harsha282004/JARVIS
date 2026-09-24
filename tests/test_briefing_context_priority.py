"""Productivity context, briefing windows, task/deadline/reminder collection, deterministic priority analysis, conflicts and
preparation. Real task/reminder/event services over isolated SQLite; integration doubles only at the Calendar/Gmail/messaging boundary."""

from datetime import datetime, timedelta, timezone

import pytest

from agent.briefing.models import (
    BriefingItem,
    BriefingWindow,
    ConflictKind,
    ItemKind,
    PriorityLevel,
    ProductivityContext,
    SourceName,
    SourceRef,
    SourceState,
    View,
)
from agent.briefing.priority import CRITICAL_AT, HIGH_AT, NORMAL_AT, Facts, PriorityAnalyzer, level_for
from agent.briefing.windows import horizon_end, window_bounds
from agent.events.models import EventType
from agent.tasks.models import TaskPriority
from tests.briefing_helpers import IST, NOW, Bench, at
from tests.calendar_helpers import all_day_event, cal_event

REF = SourceRef(source=SourceName.TASKS, label="your task list")
ANALYZER = PriorityAnalyzer(IST)


def keys(items):
    return [i.title for i in items]


# ---- windows and timezone ------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("window, start, end", [
    (BriefingWindow.TODAY, at(4, 0), at(5, 0)),
    (BriefingWindow.TOMORROW, at(5, 0), at(6, 0)),
    (BriefingWindow.THIS_WEEK, at(4, 0), at(11, 0)),  # Monday 4th until the end of Sunday 10th
    (BriefingWindow.NEXT_7_DAYS, at(4, 0), at(11, 0)),
    (BriefingWindow.YESTERDAY, at(3, 0), at(4, 0)),
])
def test_window_bounds_use_the_users_local_calendar(window, start, end):
    assert window_bounds(window, NOW, IST) == (start, end)


def test_last_24_hours_and_the_lookahead_horizon():
    assert window_bounds(BriefingWindow.LAST_24_HOURS, NOW, IST) == (NOW - timedelta(hours=24), NOW)
    _, end = window_bounds(BriefingWindow.TODAY, NOW, IST)
    assert horizon_end(end, NOW, IST, 7) == at(11, 0) and horizon_end(end, NOW, IST, 1) == end
    _, week_end = window_bounds(BriefingWindow.NEXT_7_DAYS, NOW, IST)
    assert horizon_end(week_end, NOW, IST, 3) == week_end  # the horizon is never earlier than the window


def test_the_window_follows_the_timezone_not_utc(session_factory):
    b = Bench(session_factory)
    b.task("Late night", at(4, 23, 30))  # 18:00 UTC on the 4th: still today locally
    b.task("After midnight", at(5, 0, 30))  # 19:00 UTC on the 4th but 00:30 locally on the 5th: tomorrow
    ctx = b.collector.collect(BriefingWindow.TODAY)
    assert keys(ctx.tasks_due) == ["Late night"] and keys(ctx.tasks_upcoming) == ["After midnight"] and ctx.timezone == "Asia/Kolkata"


def test_window_flags():
    assert BriefingWindow.YESTERDAY.is_past and BriefingWindow.LAST_24_HOURS.is_past and not BriefingWindow.TODAY.is_past


# ---- context shape, empty sources ----------------------------------------------------------------------------------------------


def test_an_empty_system_gives_an_empty_context_with_honest_statuses(session_factory):
    b = Bench(session_factory)
    ctx = b.collector.collect(BriefingWindow.TODAY)
    assert isinstance(ctx, ProductivityContext) and (ctx.now, ctx.window) == (NOW, BriefingWindow.TODAY) and ctx.completed_today == 0
    for field in ("tasks_overdue", "tasks_due", "tasks_upcoming", "tasks_high", "reminders", "deadlines_due", "events", "emails_action", "messages", "conflicts", "preparation"):
        assert getattr(ctx, field) == [], field
    assert ctx.state_of(SourceName.TASKS) is SourceState.OK and ctx.state_of(SourceName.CALENDAR) is SourceState.NOT_CONFIGURED
    assert ctx.state_of(SourceName.GMAIL) is SourceState.NOT_CONFIGURED and ctx.state_of(SourceName.MESSAGING) is SourceState.NOT_CONFIGURED and ctx.unavailable == []


def test_the_context_holds_references_and_summaries_not_source_data(session_factory):
    b = Bench(session_factory)
    task = b.task("Submit report", at(4, 17), TaskPriority.HIGH)
    item = b.collector.collect(BriefingWindow.TODAY).tasks_due[0]
    assert set(BriefingItem.model_fields) == {"key", "kind", "title", "when", "ends", "all_day", "level", "score", "explicit_priority", "reasons", "source", "detail", "flags"}
    assert item.source == SourceRef(source=SourceName.TASKS, source_id=task.task_id, label="your task list") and item.key == f"task:{task.task_id}"
    assert "score" not in item.model_dump()  # the internal ordering number is never exposed


# ---- tasks ---------------------------------------------------------------------------------------------------------------------


def test_tasks_are_partitioned_overdue_today_upcoming_and_high(session_factory):
    b = Bench(session_factory)
    b.task("Overdue thing", at(4, 8, 30))  # due before now (09:00)
    b.task("Today thing", at(4, 15))
    b.task("Later this week", at(7, 12))
    b.task("Far future", at(30, 12))
    b.task("Undated high", None, TaskPriority.HIGH)
    b.task("No date", None)
    ctx = b.collector.collect(BriefingWindow.TODAY)
    assert keys(ctx.tasks_overdue) == ["Overdue thing"] and keys(ctx.tasks_due) == ["Today thing"] and keys(ctx.tasks_upcoming) == ["Later this week"]
    assert keys(ctx.tasks_high) == ["Undated high"]  # an unresolved high-priority task without a date is still surfaced
    assert "overdue" in ctx.tasks_overdue[0].flags and ctx.tasks_overdue[0].reasons[-1] == "it is overdue"


def test_completed_and_cancelled_tasks_are_excluded_but_completed_today_is_counted(session_factory):
    b = Bench(session_factory)
    done, gone = b.task("Done", at(4, 15)), b.task("Cancelled", at(4, 15))
    b.task("Open", at(4, 15))
    b.tasks.complete_task(done.task_id)
    b.tasks.cancel_task(gone.task_id)
    ctx = b.collector.collect(BriefingWindow.TODAY)
    assert keys(ctx.tasks_due) == ["Open"] and ctx.completed_today == 1  # a plain factual count, never a score
    assert b.collector.collect(BriefingWindow.TOMORROW).completed_today is None


def test_tomorrow_and_week_windows_partition_differently(session_factory):
    b = Bench(session_factory)
    b.task("Today", at(4, 15))
    b.task("Tomorrow", at(5, 15))
    b.task("Friday", at(8, 15))
    assert keys(b.collector.collect(BriefingWindow.TOMORROW).tasks_due) == ["Tomorrow"]
    assert keys(b.collector.collect(BriefingWindow.THIS_WEEK).tasks_due) == ["Today", "Tomorrow", "Friday"]
    assert keys(b.collector.collect(BriefingWindow.TODAY).tasks_upcoming) == ["Tomorrow", "Friday"]


def test_the_briefing_never_modifies_tasks(session_factory):
    b = Bench(session_factory)
    task = b.task("Report", at(4, 15), TaskPriority.LOW)
    before = b.tasks.get_task(task.task_id)
    b.brief(View.OVERVIEW)
    b.brief(View.FOCUS)
    after = b.tasks.get_task(task.task_id)
    assert (after.priority, after.status, after.due_at, after.title, after.updated_at) == (before.priority, before.status, before.due_at, before.title, before.updated_at)
    assert len(b.tasks.list_tasks(limit=50)) == 1  # nothing was created either


# ---- deadlines, events and reminders ---------------------------------------------------------------------------------------------


def test_deadlines_are_bucketed_deterministically(session_factory):
    b = Bench(session_factory)
    b.event("Passed", EventType.DEADLINE, due_at=at(4, 8))  # earlier today: not done, now overdue
    b.event("Today", EventType.ASSIGNMENT, due_at=at(4, 17))
    b.event("Tomorrow", EventType.APPLICATION, due_at=at(5, 17))
    b.event("Next week", EventType.DEADLINE, due_at=at(9, 17))
    b.event("Too far", EventType.DEADLINE, due_at=at(30, 17))
    ctx = b.collector.collect(BriefingWindow.TODAY)
    assert keys(ctx.deadlines_overdue) == ["Passed"] and keys(ctx.deadlines_due) == ["Today"] and keys(ctx.deadlines_upcoming) == ["Tomorrow", "Next week"]
    assert all(i.source.source == SourceName.EVENTS and i.source.label == "your events and deadlines" for i in ctx.deadlines_upcoming)
    tomorrow = b.collector.collect(BriefingWindow.TOMORROW)
    assert keys(tomorrow.deadlines_due) == ["Tomorrow"]


def test_completed_deadlines_and_unconfirmed_guesses_do_not_appear(session_factory):
    from agent.events.models import EventSource, SourceType
    from agent.memory.models import Confidence

    b = Bench(session_factory)
    done = b.event("Done", EventType.DEADLINE, due_at=at(4, 17))
    b.events.complete_event(done.event_id)
    b.event("Maybe", EventType.DEADLINE, due_at=at(4, 18), confidence=Confidence.LOW, source=EventSource(source_type=SourceType.GMAIL, source_id="m1"))
    assert b.collector.collect(BriefingWindow.TODAY).deadlines_due == []


def test_events_linked_to_tasks_and_calendar_mirrors_are_not_double_counted(session_factory):
    from agent.events.models import EventSource, SourceType

    b = Bench(session_factory, calendar_events=[cal_event("m1", "Standup", at(4, 10), at(4, 10, 30))])
    task = b.task("Prepare slides", at(4, 15))
    b.event("Prepare slides", EventType.DEADLINE, due_at=at(4, 15), task_id=task.task_id)
    b.event("Standup", EventType.MEETING, start_at=at(4, 10), source=EventSource(source_type=SourceType.GOOGLE_CALENDAR, source_id="me@example.com/m1"))
    ctx = b.collector.collect(BriefingWindow.TODAY)
    assert keys(ctx.events) == ["Standup"] and ctx.events[0].kind is ItemKind.CALENDAR_EVENT and ctx.deadlines_due == []  # each thing once, from its own source
    assert keys(ctx.tasks_due) == ["Prepare slides"]


def test_reminders_in_the_window_only_and_a_fired_one_is_not_repeated(session_factory):
    b = Bench(session_factory)
    b.reminders.create_reminder("Call John", at(4, 16))
    b.reminders.create_reminder("Tomorrow thing", at(5, 9))
    early = b.reminders.create_reminder("Early", at(4, 9, 30))
    ctx = b.collector.collect(BriefingWindow.TODAY)
    assert keys(ctx.reminders) == ["Early", "Call John"] and ctx.reminders[0].kind is ItemKind.REMINDER and ctx.reminders[0].source.label == "your reminders"
    b.clock.advance(minutes=35)  # 09:35: the 09:30 reminder is due and the Phase 9 scheduler delivers it
    b.reminders.mark_triggered(early.reminder_id)
    assert keys(b.collector.collect(BriefingWindow.TODAY).reminders) == ["Call John"]
    assert b.reminders.get_reminder(early.reminder_id).occurrences == 1  # the briefing did not trigger or re-trigger anything


def test_calendar_events_all_day_declined_cancelled_and_over(session_factory):
    from integrations.calendar.models import CalendarEventAttendee

    declined = cal_event("d", "Declined", at(4, 14), attendees=[CalendarEventAttendee(email="me@example.com", self_=True, response_status="declined")])
    b = Bench(session_factory, calendar_events=[
        cal_event("over", "Already over", at(4, 7), at(4, 8)), cal_event("a", "Project review", at(4, 10), at(4, 11)), all_day_event("f", "Hackathon", (2030, 3, 4)), declined,
        cal_event("tom", "Tomorrow meeting", at(5, 10), at(5, 11))])
    ctx = b.collector.collect(BriefingWindow.TODAY)
    assert sorted(keys(ctx.events)) == ["Hackathon", "Project review"] and keys(ctx.events_upcoming) == ["Tomorrow meeting"]
    assert next(e for e in ctx.events if e.title == "Hackathon").all_day is True
    assert ctx.events[0].source == SourceRef(source=SourceName.CALENDAR, source_id="me@example.com/a", label="your Google Calendar") or ctx.events[1].source.source_id == "me@example.com/a"


# ---- priority: deterministic and transparent ----------------------------------------------------------------------------------------------------


def score(kind=ItemKind.TASK, when=None, explicit=None, **kw):
    return ANALYZER.assess(Facts(kind=kind, when=when, explicit=explicit, **kw), NOW)


def test_the_documented_scoring_and_thresholds():
    assert score(explicit=TaskPriority.LOW).score == 0 and score(explicit=TaskPriority.MEDIUM).score == 10
    assert score(explicit=TaskPriority.HIGH).score == 20 and score(explicit=TaskPriority.CRITICAL).score == 30
    assert score(explicit=TaskPriority.MEDIUM, when=at(4, 8)).score == 35  # overdue +25
    assert score(explicit=TaskPriority.MEDIUM, when=at(4, 11)).score == 30  # within three hours +20
    assert score(explicit=TaskPriority.MEDIUM, when=at(4, 15)).score == 25  # today +15
    assert score(explicit=TaskPriority.MEDIUM, when=at(5, 15)).score == 20  # tomorrow +10
    assert score(explicit=TaskPriority.MEDIUM, when=at(7, 15)).score == 15  # within three days +5
    assert score(explicit=TaskPriority.MEDIUM, when=at(20, 15)).score == 10  # nothing for next month
    assert (level_for(CRITICAL_AT), level_for(CRITICAL_AT - 1), level_for(HIGH_AT), level_for(HIGH_AT - 1), level_for(NORMAL_AT), level_for(NORMAL_AT - 1)) == (
        PriorityLevel.CRITICAL, PriorityLevel.HIGH, PriorityLevel.HIGH, PriorityLevel.NORMAL, PriorityLevel.NORMAL, PriorityLevel.LOW)


def test_an_overdue_critical_task_outranks_a_low_task_due_next_week():
    overdue_critical = score(explicit=TaskPriority.CRITICAL, when=at(3, 12))
    low_next_week = score(explicit=TaskPriority.LOW, when=at(11, 12))
    assert overdue_critical.level is PriorityLevel.CRITICAL and low_next_week.level is PriorityLevel.LOW and overdue_critical.score > low_next_week.score
    assert overdue_critical.reasons == ("it is marked critical priority", "it is overdue")


def test_high_priority_due_tomorrow_reaches_high_and_reasons_are_factual():
    a = score(explicit=TaskPriority.HIGH, when=at(5, 12))
    assert a.level is PriorityLevel.HIGH and a.reasons == ("it is marked high priority", "it is due tomorrow")
    assert score(explicit=TaskPriority.HIGH, when=at(20, 12)).level is PriorityLevel.NORMAL  # explicit, but far away
    assert score(kind=ItemKind.DEADLINE, when=at(5, 12)).score == 30 and score(kind=ItemKind.DEADLINE, when=at(5, 12)).level is PriorityLevel.HIGH  # deadlines default to 20


def test_kind_and_event_type_defaults_are_fixed_rules():
    assert score(kind=ItemKind.EVENT, event_type=EventType.INTERVIEW).score == 20 and score(kind=ItemKind.EVENT, event_type=EventType.MEETING).score == 10
    assert score(kind=ItemKind.EVENT, event_type=EventType.INTERVIEW).reasons == ("it is an interview",)
    assert score(kind=ItemKind.EMAIL, action_email=True).score == 12 and score(kind=ItemKind.EMAIL).score == 10 and score(kind=ItemKind.MESSAGE).score == 8
    assert "guess" in score(kind=ItemKind.EMAIL, action_email=True).reasons[0]  # a classifier guess is never presented as fact


def test_a_started_event_is_not_more_urgent_and_all_day_has_no_hour_bonus():
    assert score(kind=ItemKind.CALENDAR_EVENT, when=at(4, 8)).score == 10  # already started: no bonus
    assert score(kind=ItemKind.CALENDAR_EVENT, when=at(4, 0), all_day=True).score == 25  # today, but no "within three hours" for all-day
    assert score(kind=ItemKind.CALENDAR_EVENT, when=at(4, 10)).reasons == ("it starts within three hours",)


def test_ordering_is_deterministic_and_wording_never_matters(session_factory):
    b = Bench(session_factory)
    for i in range(3):
        b.task(f"Same {i}", at(4, 15), TaskPriority.MEDIUM)
    b.task("URGENT!!! CRITICAL!!! do this NOW", at(20, 15), TaskPriority.LOW)  # shouting adds nothing
    first = b.collector.collect(BriefingWindow.TODAY)
    second = b.collector.collect(BriefingWindow.TODAY)
    assert [i.key for i in first.tasks_due] == [i.key for i in second.tasks_due] and sorted(keys(first.tasks_due)) == ["Same 0", "Same 1", "Same 2"]  # a stable order, ties broken by key
    assert first.tasks_upcoming == [] and all(i.level is not PriorityLevel.HIGH for i in first.tasks_due)  # the shouting title, due next month, ranks with nothing


def test_the_underlying_priority_is_never_changed_by_the_analysis(session_factory):
    b = Bench(session_factory)
    task = b.task("Low but overdue", at(4, 8), TaskPriority.LOW)
    item = b.collector.collect(BriefingWindow.TODAY).tasks_overdue[0]
    assert item.level >= PriorityLevel.NORMAL and item.explicit_priority is TaskPriority.LOW and b.tasks.get_task(task.task_id).priority is TaskPriority.LOW


# ---- conflicts and preparation ----------------------------------------------------------------------------------------------------------------


def test_calendar_overlaps_are_reported_not_resolved(session_factory):
    b = Bench(session_factory, calendar_events=[cal_event("a", "Project review", at(4, 10), at(4, 11)), cal_event("b", "Design sync", at(4, 10, 30), at(4, 11, 30)),
                                                cal_event("c", "Separate", at(4, 14), at(4, 15)), all_day_event("d", "Hackathon", (2030, 3, 4))])
    ctx = b.collector.collect(BriefingWindow.TODAY)
    assert [(c.kind, c.first_title, c.second_title) for c in ctx.conflicts] == [(ConflictKind.OVERLAP, "Project review", "Design sync")]  # the all-day entry is not a clash
    assert b.calendar_client.mutations() == []


def test_multiple_high_priority_deadlines_close_together_are_reported(session_factory):
    b = Bench(session_factory)
    b.task("Report", at(5, 9), TaskPriority.HIGH)
    b.event("Application", EventType.APPLICATION, due_at=at(5, 17))
    b.task("Distant", at(9, 9), TaskPriority.HIGH)
    ctx = b.collector.collect(BriefingWindow.TODAY)
    clusters = [c for c in ctx.conflicts if c.kind is ConflictKind.DEADLINE_CLUSTER]
    assert len(clusters) == 1 and {clusters[0].first_title, clusters[0].second_title} == {"Report", "Application"}


def test_preparation_links_only_existing_pending_tasks(session_factory):
    b = Bench(session_factory, calendar_events=[cal_event("i", "Technical interview", at(5, 10), at(5, 11))])
    b.task("Prepare resume", at(4, 20))
    b.task("Buy milk", at(4, 20))
    b.task("Prepare for something else", at(9, 12))  # due after the interview: not preparation for it
    ctx = b.collector.collect(BriefingWindow.TODAY)
    assert [(p.event_title, p.task_title, p.basis) for p in ctx.preparation] == [("Technical interview", "Prepare resume", "pending")]
    assert len(b.tasks.list_tasks(limit=50)) == 3  # nothing was invented or created


def test_a_shared_keyword_or_a_recorded_link_is_a_stronger_basis(session_factory):
    b = Bench(session_factory)
    interview = b.event("Acme interview", EventType.INTERVIEW, start_at=at(5, 10))
    b.task("Research Acme", at(4, 18))
    linked_task = b.task("Portfolio", at(4, 19))
    b.events.link_task(interview.event_id, linked_task.task_id)
    ctx = b.collector.collect(BriefingWindow.TODAY)
    assert {(p.task_title, p.basis) for p in ctx.preparation} == {("Portfolio", "linked"), ("Research Acme", "named")}


def test_no_preparation_without_a_real_upcoming_event(session_factory):
    b = Bench(session_factory)
    b.task("Prepare resume", at(4, 20))
    assert b.collector.collect(BriefingWindow.TODAY).preparation == []
    far = Bench(session_factory, calendar_events=[cal_event("i", "Interview", at(20, 10), at(20, 11))])
    assert far.collector.collect(BriefingWindow.TODAY).preparation == []  # weeks away: not within 48 hours


def test_high_priority_property_is_ordered_and_deduplicated(session_factory):
    b = Bench(session_factory)
    b.task("A", at(4, 15), TaskPriority.HIGH)
    b.task("B", at(5, 9), TaskPriority.CRITICAL)
    b.task("C", at(4, 15), TaskPriority.LOW)
    ctx = b.collector.collect(BriefingWindow.TODAY)
    assert keys(ctx.high_priority) == ["B", "A"] and len({i.key for i in ctx.high_priority}) == 2
