"""Event/deadline models, EventService, temporal reasoning and conflict detection.

Runs on an isolated SQLite database (never your PostgreSQL); on a disposable PostgreSQL too when
JARVIS_TEST_DATABASE_URL is set (see conftest).
"""

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from agent.events.conflicts import ConflictKind, conflict_between, find_conflicts
from agent.events.models import (
    DUE_TYPES,
    EVENT_TRANSITIONS,
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
from agent.events.service import EventService, SyncOutcome
from agent.events.temporal import EventScope, countdown, days_until, scope_window
from agent.memory.models import Confidence
from agent.tasks.models import TaskPriority, TaskStatus
from backend.models.base import Base
from backend.models.events import EventRow
from tests.event_helpers import IST, NOW, Clock, explicit_source, gmail_source, ist, make_events


# ---- model ---------------------------------------------------------------------------------------------------------


def test_enums_match_the_specification():
    assert {t.name for t in EventType} == {"DEADLINE", "MEETING", "INTERVIEW", "EXAM", "ASSIGNMENT", "APPLICATION",
                                           "APPOINTMENT", "EVENT", "REMINDER", "OTHER"}
    assert {s.name for s in EventStatus} == {"UPCOMING", "ACTIVE", "COMPLETED", "CANCELLED", "MISSED", "UNKNOWN"}
    assert {s.name for s in SourceType} == {"CONVERSATION", "MEMORY", "RAG_DOCUMENT", "GMAIL", "TASK", "USER_EXPLICIT", "UNKNOWN"}
    assert DUE_TYPES == {EventType.DEADLINE, EventType.ASSIGNMENT, EventType.APPLICATION}
    assert EVENT_TRANSITIONS[EventStatus.COMPLETED] == frozenset() == EVENT_TRANSITIONS[EventStatus.CANCELLED]


def base(**kw):
    data = dict(title="Interview", timezone="Asia/Kolkata", created_at=NOW, updated_at=NOW, start_at=ist(2030, 3, 6, 10))
    data.update(kw)
    return Event(**data)


def test_event_validation():
    with pytest.raises(ValueError):
        base(start_at=None)  # needs a start or a due time
    with pytest.raises(ValueError):
        base(title="  ")
    with pytest.raises(ValueError):
        base(title="x" * 201)
    with pytest.raises(ValueError):
        base(start_at=datetime(2030, 3, 6, 10))  # naive timestamps are a bug, never guessed
    with pytest.raises(ValueError):
        base(timezone="Mars/Base")
    with pytest.raises(ValueError):
        base(end_at=ist(2030, 3, 6, 9))  # ends before it starts
    with pytest.raises(ValueError):
        base(start_at=None, due_at=ist(2030, 3, 6), end_at=ist(2030, 3, 7))  # an end needs a start
    with pytest.raises(ValueError):
        base(status=EventStatus.COMPLETED)
    with pytest.raises(ValueError):
        base(status="finished")
    e = base(title="  Team\x00 <b>meeting</b> ", description="  note ")
    assert e.title == "Team (b)meeting(/b)" and e.description == "note" and e.status is EventStatus.UPCOMING
    assert e.priority is None and e.confidence is Confidence.HIGH  # priority is never invented


def test_timestamps_are_normalized_to_utc_and_the_zone_is_kept():
    e = base(start_at=ist(2030, 3, 6, 10), due_at=None)
    assert e.start_at == datetime(2030, 3, 6, 4, 30, tzinfo=timezone.utc) and e.timezone == "Asia/Kolkata"
    assert not e.is_deadline and e.anchor == e.start_at
    d = base(start_at=None, due_at=ist(2030, 3, 6, 17))
    assert d.is_deadline and d.anchor == d.due_at


def test_provenance_is_explicit_and_unknown_when_missing():
    assert base().source.source_type is SourceType.UNKNOWN  # never guessed
    src = EventSource(source_type=SourceType.GMAIL, source_id="m1", reference="email <from> John\x00")
    assert src.reference == "email (from) John"


def test_dedupe_key_is_stable_and_not_semantic():
    a = make_dedupe_key("Final  Submission!", EventType.DEADLINE, ist(2030, 3, 20))
    assert a == make_dedupe_key("final submission", EventType.DEADLINE, ist(2030, 3, 20))
    assert a != make_dedupe_key("final submission", EventType.DEADLINE, ist(2030, 3, 21))
    assert a != make_dedupe_key("final submission", EventType.EXAM, ist(2030, 3, 20))
    assert a != make_dedupe_key("submission final", EventType.DEADLINE, ist(2030, 3, 20))  # word order counts: no fuzzy matching


# ---- create / read / update ----------------------------------------------------------------------------------------


def test_create_and_get_event_with_provenance(session_factory):
    events, _, clock = make_events(session_factory)
    result = events.create_event(
        "Internship interview", EventType.INTERVIEW, start_at=ist(2030, 3, 7, 11), end_at=ist(2030, 3, 7, 12),
        description="Your interview is scheduled for March 7 at 11 AM.", priority=TaskPriority.HIGH,
        source=gmail_source("m1"), confidence=Confidence.HIGH, metadata={"thread_id": "t1"},
    )
    assert result.created
    got = events.get_event(result.event.event_id)
    assert got == result.event
    assert (got.source.source_type, got.source.source_id, got.source.reference) == (SourceType.GMAIL, "m1", "email from John, dated March 1, 2030")
    assert got.timezone == "Asia/Kolkata" and got.priority is TaskPriority.HIGH and got.confidence is Confidence.HIGH
    assert got.created_at == clock() and got.metadata == {"thread_id": "t1"} and got.start_at == datetime(2030, 3, 7, 5, 30, tzinfo=timezone.utc)
    with pytest.raises(EventNotFound):
        events.get_event("missing")


def test_invalid_input_is_rejected_without_echoing_content(session_factory):
    events, *_ = make_events(session_factory)
    with pytest.raises(EventValidationError) as exc:
        events.create_event("secret-title " * 40, EventType.EVENT, start_at=ist(2030, 3, 6, 10))
    assert "secret-title" not in str(exc.value)
    with pytest.raises(EventValidationError):
        events.create_event("x", EventType.EVENT)  # no time at all
    with pytest.raises(EventValidationError):
        events.create_event("x", EventType.EVENT, start_at=datetime(2030, 3, 6, 10))
    assert events.list_scope(EventScope.ALL).events == []


def test_low_confidence_external_events_are_unconfirmed(session_factory):
    events, *_ = make_events(session_factory)
    low = events.create_event("Maybe due", EventType.DEADLINE, due_at=ist(2030, 3, 8, 23, 59), source=gmail_source("m9"),
                              confidence=Confidence.LOW).event
    assert low.status is EventStatus.UNKNOWN
    own = events.create_event("Mine", EventType.DEADLINE, due_at=ist(2030, 3, 8, 23, 59), source=explicit_source(),
                              confidence=Confidence.LOW).event
    assert own.status is EventStatus.UPCOMING  # the user's own statement is not held back
    assert events.confirm_event(low.event_id).status is EventStatus.UPCOMING
    with pytest.raises(InvalidEventTransition):
        events.confirm_event(low.event_id)


def test_update_event(session_factory):
    events, _, clock = make_events(session_factory)
    e = events.create_event("Meeting with guide", EventType.MEETING, start_at=ist(2030, 3, 5, 15), source=explicit_source()).event
    clock.advance(minutes=5)
    u = events.update_event(e.event_id, title="Project meeting", start_at=ist(2030, 3, 5, 16), end_at=ist(2030, 3, 5, 17),
                            priority=TaskPriority.CRITICAL, description="room 4")
    assert (u.title, u.priority, u.description) == ("Project meeting", TaskPriority.CRITICAL, "room 4")
    assert u.start_at == ist(2030, 3, 5, 16).astimezone(timezone.utc) and u.updated_at == clock()
    assert events.update_event(e.event_id, end_at=None).end_at is None  # explicit clear
    with pytest.raises(EventValidationError):
        events.update_event(e.event_id, title="  ")
    events.cancel_event(e.event_id)
    with pytest.raises(InvalidEventTransition):
        events.update_event(e.event_id, title="changed after cancelling")


def test_update_cannot_create_a_duplicate_of_another_event_from_the_same_source(session_factory):
    events, *_ = make_events(session_factory)
    events.create_event("A", EventType.MEETING, start_at=ist(2030, 3, 5, 10), source=gmail_source("m1"))
    b = events.create_event("B", EventType.MEETING, start_at=ist(2030, 3, 5, 10), source=gmail_source("m1")).event
    with pytest.raises(EventValidationError):
        events.update_event(b.event_id, title="A")


# ---- status transitions --------------------------------------------------------------------------------------------


def test_complete_and_cancel_set_their_timestamps_and_are_final(session_factory):
    events, _, clock = make_events(session_factory)
    a = events.create_event("Exam", EventType.EXAM, start_at=ist(2030, 3, 9, 10), source=explicit_source()).event
    b = events.create_event("Trip", EventType.EVENT, start_at=ist(2030, 3, 10, 10), source=explicit_source()).event
    done, cancelled = events.complete_event(a.event_id), events.cancel_event(b.event_id)
    assert done.status is EventStatus.COMPLETED and done.completed_at == clock()
    assert cancelled.status is EventStatus.CANCELLED and cancelled.cancelled_at == clock()
    for f in (events.complete_event, events.cancel_event, events.confirm_event):
        for e in (a, b):
            with pytest.raises(InvalidEventTransition):
                f(e.event_id)


def test_statuses_follow_time_without_any_background_job(session_factory):
    events, _, clock = make_events(session_factory)
    meeting = events.create_event("Standup", EventType.MEETING, start_at=ist(2030, 3, 4, 16), end_at=ist(2030, 3, 4, 17), source=explicit_source()).event
    deadline = events.create_event("Report due", EventType.DEADLINE, due_at=ist(2030, 3, 4, 18), source=explicit_source()).event
    events.refresh_statuses()
    assert events.get_event(meeting.event_id).status is EventStatus.UPCOMING
    clock.now = ist(2030, 3, 4, 16, 30).astimezone(timezone.utc)
    events.refresh_statuses()
    assert events.get_event(meeting.event_id).status is EventStatus.ACTIVE  # going on now
    clock.now = ist(2030, 3, 4, 19).astimezone(timezone.utc)
    events.refresh_statuses()
    assert events.get_event(meeting.event_id).status is EventStatus.MISSED  # over, never marked completed
    assert events.get_event(deadline.event_id).status is EventStatus.MISSED
    assert events.refresh_statuses() == 0  # idempotent
    assert events.complete_event(deadline.event_id).status is EventStatus.COMPLETED  # done late is still done


def test_rescheduling_a_missed_event_to_the_future_makes_it_upcoming_again(session_factory):
    events, _, clock = make_events(session_factory)
    e = events.create_event("Report due", EventType.DEADLINE, due_at=ist(2030, 3, 4, 15), source=explicit_source()).event
    clock.advance(hours=2)
    events.refresh_statuses()
    assert events.get_event(e.event_id).status is EventStatus.MISSED
    assert events.update_event(e.event_id, due_at=ist(2030, 3, 6, 15)).status is EventStatus.UPCOMING


# ---- queries and temporal reasoning ----------------------------------------------------------------------------------


def seed(events):
    src = explicit_source
    events.create_event("Yesterday's report", EventType.DEADLINE, due_at=ist(2030, 3, 3, 17), source=src())            # overdue
    events.create_event("Standup", EventType.MEETING, start_at=ist(2030, 3, 4, 18), source=src())                      # later today
    events.create_event("Guide meeting", EventType.MEETING, start_at=ist(2030, 3, 5, 15), source=src())               # tomorrow
    events.create_event("Internship interview", EventType.INTERVIEW, start_at=ist(2030, 3, 7, 11), source=src())     # Thursday
    events.create_event("Project submission", EventType.DEADLINE, due_at=ist(2030, 3, 8, 17), source=src())          # Friday
    events.create_event("Weekend trip", EventType.EVENT, start_at=ist(2030, 3, 9, 8), source=src())                  # Saturday
    events.create_event("Exam", EventType.EXAM, start_at=ist(2030, 3, 12, 10), source=src())                         # next week
    events.create_event("Registration closes", EventType.DEADLINE, due_at=ist(2030, 3, 30, 23, 59), source=src())   # far
    events.create_event("Maybe due", EventType.DEADLINE, due_at=ist(2030, 3, 6, 23, 59), source=gmail_source("x"),
                        confidence=Confidence.LOW)                                                                   # unconfirmed


def titles(listing):
    return [e.title for e in listing.events]


def test_scopes_use_the_users_local_calendar(session_factory):
    events, *_ = make_events(session_factory)
    seed(events)
    assert titles(events.list_scope(EventScope.TODAY)) == ["Standup"]
    assert titles(events.list_scope(EventScope.TOMORROW)) == ["Guide meeting"]
    assert titles(events.list_scope(EventScope.THIS_WEEK)) == ["Standup", "Guide meeting", "Internship interview", "Project submission", "Weekend trip"]
    assert titles(events.list_scope(EventScope.NEXT_WEEK)) == ["Exam"]
    assert titles(events.list_scope(EventScope.NEXT_7_DAYS)) == titles(events.list_scope(EventScope.THIS_WEEK))  # today is a Monday
    assert titles(events.list_scope(EventScope.UPCOMING))[:3] == ["Standup", "Guide meeting", "Internship interview"]  # default: 7 days
    assert "Registration closes" not in titles(events.list_scope(EventScope.UPCOMING))
    assert "Registration closes" in titles(events.list_scope(EventScope.ALL))


def test_unconfirmed_events_are_counted_not_listed_except_for_all(session_factory):
    events, *_ = make_events(session_factory)
    seed(events)
    week = events.list_scope(EventScope.THIS_WEEK)
    assert "Maybe due" not in titles(week) and week.unconfirmed == 1
    assert "Maybe due" in titles(events.list_scope(EventScope.ALL))


def test_overdue_lists_only_open_deadlines_past_due(session_factory):
    events, *_ = make_events(session_factory)
    seed(events)
    assert titles(events.list_scope(EventScope.OVERDUE)) == ["Yesterday's report"]  # meetings are never "overdue"
    events.complete_event(events.find_matching("yesterday report")[0].event_id)
    assert events.list_scope(EventScope.OVERDUE).events == []


def test_next_event_and_type_filters(session_factory):
    events, *_ = make_events(session_factory)
    seed(events)
    assert events.next_event().title == "Standup"
    assert events.next_event(EventType.INTERVIEW).title == "Internship interview"
    assert events.next_event(EventType.APPLICATION) is None
    assert titles(events.list_scope(EventScope.ALL, event_type=EventType.DEADLINE)) == ["Maybe due", "Project submission", "Registration closes"]
    assert all(e.event_type is EventType.MEETING for e in events.list_scope(EventScope.ALL, event_type=EventType.MEETING).events)


def test_result_limits_are_bounded(session_factory):
    events, *_ = make_events(session_factory, max_results=3)
    for i in range(8):
        events.create_event(f"Item {i}", EventType.EVENT, start_at=ist(2030, 3, 6, 8 + i), source=explicit_source())
    listing = events.list_scope(EventScope.THIS_WEEK, limit=100)
    assert len(listing.events) == 3 and listing.total == 8 and listing.truncated


def test_search_and_ambiguity(session_factory):
    events, *_ = make_events(session_factory)
    seed(events)
    assert [e.title for e in events.find_matching("project submission")] == ["Project submission"]
    assert [e.title for e in events.find_matching("my interview")] == ["Internship interview"]  # the type word matches too
    assert events.find_matching("quantum physics") == []
    events.create_event("Second interview", EventType.INTERVIEW, start_at=ist(2030, 3, 14, 11), source=explicit_source())
    assert len(events.find_matching("interview")) == 2  # the caller must ask, not guess
    assert len(events.find_matching("second interview")) == 1
    events.cancel_event(events.find_matching("second interview")[0].event_id)
    assert events.find_matching("second interview") == []
    assert len(events.find_matching("second interview", include_closed=True)) == 1


def test_scope_windows_and_countdowns_including_dst():
    zone = IST
    now = ist(2030, 3, 4, 14, 30)  # Monday
    assert scope_window(EventScope.TODAY, now, zone, 7) == (ist(2030, 3, 4), ist(2030, 3, 5))
    assert scope_window(EventScope.THIS_WEEK, now, zone, 7) == (ist(2030, 3, 4), ist(2030, 3, 11))
    assert scope_window(EventScope.NEXT_WEEK, now, zone, 7) == (ist(2030, 3, 11), ist(2030, 3, 18))
    sunday = ist(2030, 3, 10, 9)
    assert scope_window(EventScope.THIS_WEEK, sunday, zone, 7) == (ist(2030, 3, 10), ist(2030, 3, 11))  # only today is left
    assert scope_window(EventScope.UPCOMING, now, zone, 3) == (ist(2030, 3, 4), ist(2030, 3, 7))
    assert scope_window(EventScope.OVERDUE, now, zone, 7) is None and scope_window(EventScope.ALL, now, zone, 7) is None

    ny = ZoneInfo("America/New_York")  # clocks go forward on 2030-03-10; local days stay whole
    start, end = scope_window(EventScope.TODAY, datetime(2030, 3, 10, 15, 0, tzinfo=timezone.utc), ny, 7)
    assert end.astimezone(timezone.utc) - start.astimezone(timezone.utc) == timedelta(hours=23)  # the day was 23 hours long


def test_days_until_and_countdown_wording(session_factory):
    events, *_ = make_events(session_factory)
    seed(events)
    now = ist(2030, 3, 4, 14, 30)
    interview = events.find_matching("internship interview")[0]
    assert days_until(interview.anchor, now, IST) == 3
    assert countdown(interview, now, IST) == "in 3 days"
    assert countdown(events.find_matching("guide meeting")[0], now, IST) == "tomorrow"
    assert countdown(events.find_matching("standup")[0], now, IST) == "in about 4 hours"
    assert countdown(events.find_matching("yesterday report")[0], now, IST) == "yesterday"
    # a day is a LOCAL day: 20:00 UTC on Mar 4 is 01:30 on Mar 5 in Kolkata, i.e. tomorrow
    late = events.create_event("Late", EventType.EVENT, start_at=datetime(2030, 3, 4, 20, 0, tzinfo=timezone.utc), source=explicit_source()).event
    assert days_until(late.anchor, now, IST) == 1


# ---- duplicates ------------------------------------------------------------------------------------------------------


def test_the_same_source_processed_twice_stores_one_event(session_factory):
    events, *_ = make_events(session_factory)
    kw = dict(start_at=ist(2030, 3, 7, 11), source=gmail_source("m1"), confidence=Confidence.HIGH)
    first = events.create_event("Interview", EventType.INTERVIEW, **kw)
    second = events.create_event("Interview", EventType.INTERVIEW, **kw)
    assert first.created and not second.created and second.event.event_id == first.event.event_id
    assert len(events.list_scope(EventScope.ALL).events) == 1


def test_different_sources_or_times_stay_separate(session_factory):
    events, *_ = make_events(session_factory)
    a = events.create_event("Interview", EventType.INTERVIEW, start_at=ist(2030, 3, 7, 11), source=gmail_source("m1"))
    b = events.create_event("Interview", EventType.INTERVIEW, start_at=ist(2030, 3, 7, 11), source=gmail_source("m2"))  # another email
    c = events.create_event("Interview", EventType.INTERVIEW, start_at=ist(2030, 3, 8, 11), source=gmail_source("m1"))  # another time
    d = events.create_event("Interview", EventType.INTERVIEW, start_at=ist(2030, 3, 7, 11), source=explicit_source())  # told by the user
    assert all(r.created for r in (a, b, c, d)) and len(events.list_scope(EventScope.ALL).events) == 4  # no semantic merging


def test_readding_a_cancelled_event_revives_it_only_when_the_user_asks(session_factory):
    events, *_ = make_events(session_factory)
    e = events.create_event("Exam", EventType.EXAM, start_at=ist(2030, 3, 9, 10), source=explicit_source()).event
    events.cancel_event(e.event_id)
    again = events.create_event("Exam", EventType.EXAM, start_at=ist(2030, 3, 9, 10), source=explicit_source())
    assert not again.created and again.event.status is EventStatus.CANCELLED  # extraction never resurrects it
    revived = events.create_event("Exam", EventType.EXAM, start_at=ist(2030, 3, 9, 10), source=explicit_source(), revive_closed=True)
    assert revived.revived and revived.event.status is EventStatus.UPCOMING and revived.event.cancelled_at is None


def test_the_database_itself_enforces_source_uniqueness(session_factory):
    events, *_ = make_events(session_factory)
    e = events.create_event("Interview", EventType.INTERVIEW, start_at=ist(2030, 3, 7, 11), source=gmail_source("m1")).event
    with session_factory() as session:
        row = session.scalars(select(EventRow)).one()
        session.add(EventRow(
            id="f" * 32, title="x", event_type="event", status="upcoming", start_at=row.start_at, timezone="UTC", all_day=False,
            source_type=row.source_type, source_id=row.source_id, confidence=3, dedupe_key=row.dedupe_key,
            created_at=row.created_at, updated_at=row.updated_at, extra={},
        ))
        with pytest.raises(IntegrityError):
            session.commit()


# ---- persistence ---------------------------------------------------------------------------------------------------------


def test_events_survive_a_restart(tmp_path):
    url = f"sqlite:///{tmp_path / 'events.db'}"
    engine = create_engine(url)
    Base.metadata.create_all(engine)
    events, *_ = make_events(sessionmaker(bind=engine, expire_on_commit=False))
    e = events.create_event("Interview", EventType.INTERVIEW, start_at=ist(2030, 3, 7, 11), source=gmail_source("m1"),
                            priority=TaskPriority.HIGH, metadata={"k": "v"}).event
    engine.dispose()
    engine2 = create_engine(url)  # "restart"
    events2, *_ = make_events(sessionmaker(bind=engine2, expire_on_commit=False))
    assert events2.get_event(e.event_id) == e
    events2.complete_event(e.event_id)
    assert not events2.create_event("Interview", EventType.INTERVIEW, start_at=ist(2030, 3, 7, 11), source=gmail_source("m1")).created
    engine2.dispose()
    engine3 = create_engine(url)
    events3, *_ = make_events(sessionmaker(bind=engine3, expire_on_commit=False))
    assert events3.get_event(e.event_id).status is EventStatus.COMPLETED
    engine3.dispose()


# ---- tasks --------------------------------------------------------------------------------------------------------------------


def test_an_event_can_reference_an_existing_task_without_creating_one(session_factory):
    events, tasks, _ = make_events(session_factory)
    task = tasks.create_task("Submit internship application", due_at=ist(2030, 10, 5, 23, 59))
    e = events.create_event("Application deadline", EventType.APPLICATION, due_at=ist(2030, 10, 5, 23, 59), source=explicit_source()).event
    linked = events.link_task(e.event_id, task.task_id)
    assert linked.task_id == task.task_id and len(tasks.list_tasks()) == 1  # a reference, not a copy
    assert events.unlink_task(e.event_id).task_id is None
    with pytest.raises(EventValidationError):
        events.link_task(e.event_id, "no-such-task")
    tasks.complete_task(task.task_id)
    with pytest.raises(EventValidationError):
        events.link_task(e.event_id, task.task_id)  # finished tasks are not linked


def test_event_for_task_is_idempotent_and_creates_no_task(session_factory):
    events, tasks, _ = make_events(session_factory)
    task = tasks.create_task("Finish report", due_at=ist(2030, 3, 8, 17), priority=TaskPriority.HIGH)
    first = events.event_for_task(task.task_id)
    second = events.event_for_task(task.task_id)
    assert first.created and not second.created and first.event.event_id == second.event.event_id
    assert first.event.task_id == task.task_id and first.event.source.source_type is SourceType.TASK
    assert first.event.priority is TaskPriority.HIGH and first.event.is_deadline
    assert len(tasks.list_tasks()) == 1
    undated = tasks.create_task("Someday")
    with pytest.raises(EventValidationError):
        events.event_for_task(undated.task_id)


def test_deadline_synchronization_never_overwrites_a_differing_date(session_factory):
    events, tasks, _ = make_events(session_factory)
    e = events.create_event("Report due", EventType.DEADLINE, due_at=ist(2030, 3, 8, 17), source=explicit_source()).event
    assert events.task_due_state(e) is SyncOutcome.NO_TASK
    undated = tasks.create_task("Write report")
    events.link_task(e.event_id, undated.task_id)
    assert events.sync_task_due(e.event_id) is SyncOutcome.APPLIED  # the task had no date: it now has the event's
    assert tasks.get_task(undated.task_id).due_at == e.due_at
    assert events.sync_task_due(e.event_id) is SyncOutcome.IN_SYNC
    tasks.update_task(undated.task_id, due_at=ist(2030, 3, 9, 17))
    assert events.sync_task_due(e.event_id) is SyncOutcome.MISMATCH  # reported; the task keeps its own date
    assert tasks.get_task(undated.task_id).due_at == ist(2030, 3, 9, 17).astimezone(timezone.utc)


def test_deleting_a_task_keeps_the_event(session_factory):
    events, tasks, _ = make_events(session_factory)
    task = tasks.create_task("x", due_at=ist(2030, 3, 8, 17))
    e = events.event_for_task(task.task_id).event
    tasks.delete_task(task.task_id)
    assert events.get_event(e.event_id).task_id is None  # FK is ON DELETE SET NULL: the event survives


# ---- conflicts -------------------------------------------------------------------------------------------------------------------


def ev(title, start, end=None, all_day=False, status=EventStatus.UPCOMING, **kw):
    return base(title=title, start_at=start, end_at=end, all_day=all_day, status=status, **kw)


def test_overlapping_events_conflict():
    a = ev("A", ist(2030, 3, 5, 10), ist(2030, 3, 5, 11))
    b = ev("B", ist(2030, 3, 5, 10, 30), ist(2030, 3, 5, 11, 30))
    c = conflict_between(a, b, IST)
    assert c is not None and c.kind is ConflictKind.OVERLAP and (c.first.title, c.second.title) == ("A", "B")
    assert conflict_between(b, a, IST).first.title == "A"  # deterministic order


def test_non_overlapping_and_touching_events_do_not_conflict():
    a = ev("A", ist(2030, 3, 5, 10), ist(2030, 3, 5, 11))
    assert conflict_between(a, ev("B", ist(2030, 3, 5, 11), ist(2030, 3, 5, 12)), IST) is None  # back to back
    assert conflict_between(a, ev("C", ist(2030, 3, 5, 14), ist(2030, 3, 5, 15)), IST) is None
    assert conflict_between(a, ev("D", ist(2030, 3, 6, 10), ist(2030, 3, 6, 11)), IST) is None


def test_instants_conflict_only_when_inside_or_equal():
    a = ev("A", ist(2030, 3, 5, 10), ist(2030, 3, 5, 11))
    assert conflict_between(a, ev("inside", ist(2030, 3, 5, 10, 30)), IST) is not None
    assert conflict_between(a, ev("at the end", ist(2030, 3, 5, 11)), IST) is None
    assert conflict_between(ev("i1", ist(2030, 3, 5, 9)), ev("i2", ist(2030, 3, 5, 9)), IST) is not None
    assert conflict_between(ev("i1", ist(2030, 3, 5, 9)), ev("i2", ist(2030, 3, 5, 9, 1)), IST) is None


def test_all_day_events_conflict_softly_on_the_same_local_day():
    trip = ev("Trip", ist(2030, 3, 5), ist(2030, 3, 6), all_day=True)
    meeting = ev("Meeting", ist(2030, 3, 5, 15), ist(2030, 3, 5, 16))
    c = conflict_between(trip, meeting, IST)
    assert c is not None and c.kind is ConflictKind.ALL_DAY  # informational "same day", not a time overlap
    assert conflict_between(trip, ev("Tomorrow", ist(2030, 3, 6, 15), ist(2030, 3, 6, 16)), IST) is None
    assert conflict_between(trip, ev("Fair", ist(2030, 3, 5), ist(2030, 3, 6), all_day=True), IST).kind is ConflictKind.ALL_DAY
    # the day is the user's local day: 23:30 UTC is already the next day in Kolkata
    assert conflict_between(trip, ev("Late", datetime(2030, 3, 4, 23, 30, tzinfo=timezone.utc)), IST) is not None


def test_deadlines_cancelled_and_unconfirmed_events_never_conflict():
    a = ev("A", ist(2030, 3, 5, 10), ist(2030, 3, 5, 11))
    deadline = base(title="Due", start_at=None, due_at=ist(2030, 3, 5, 10, 30))
    assert conflict_between(a, deadline, IST) is None
    assert conflict_between(a, ev("X", ist(2030, 3, 5, 10, 30), status=EventStatus.UNKNOWN), IST) is None
    cancelled = ev("Y", ist(2030, 3, 5, 10, 30), status=EventStatus.CANCELLED, cancelled_at=NOW)
    assert conflict_between(a, cancelled, IST) is None
    assert conflict_between(a, a, IST) is None


def test_find_conflicts_lists_each_pair_once_in_order():
    a = ev("A", ist(2030, 3, 5, 10), ist(2030, 3, 5, 12))
    b = ev("B", ist(2030, 3, 5, 11), ist(2030, 3, 5, 13))
    c = ev("C", ist(2030, 3, 5, 11, 30), ist(2030, 3, 5, 12, 30))
    d = ev("D", ist(2030, 3, 6, 9), ist(2030, 3, 6, 10))
    pairs = [(x.first.title, x.second.title) for x in find_conflicts([d, c, b, a], IST)]
    assert pairs == [("A", "B"), ("A", "C"), ("B", "C")]


def test_the_service_reports_conflicts_and_changes_nothing(session_factory):
    events, *_ = make_events(session_factory)
    a = events.create_event("Interview", EventType.INTERVIEW, start_at=ist(2030, 3, 6, 10), end_at=ist(2030, 3, 6, 11), source=explicit_source()).event
    b = events.create_event("Guide meeting", EventType.MEETING, start_at=ist(2030, 3, 6, 10, 30), end_at=ist(2030, 3, 6, 11, 30), source=explicit_source()).event
    found = events.conflicts_for(b)
    assert [(c.first.event_id, c.second.event_id) for c in found] == [(a.event_id, b.event_id)]
    assert events.get_event(a.event_id) == a and events.get_event(b.event_id) == b  # nothing was rescheduled
    assert events.conflicts_for(events.create_event("Due", EventType.DEADLINE, due_at=ist(2030, 3, 6, 10, 30), source=explicit_source()).event) == []
