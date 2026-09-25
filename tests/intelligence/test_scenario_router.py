"""The synthetic project-review scenario, end to end through the router, with REAL services over SQLite and fake API clients.

Asserts behavior: what is said, what is created, that nothing changes without confirmation, that results are verified and that no
language model is ever called.
"""

import pytest

from integrations.calendar.models import CalendarOutcomeUnknown
from tests.calendar_helpers import FakeCalendarClient, cal_event
from tests.intelligence_helpers import NoLLM, build_harness, email_raw, ist, scenario_harness


@pytest.fixture
def h(tmp_path):
    NoLLM.calls = 0
    return scenario_harness(tmp_path)


def test_whats_important_tomorrow_uses_actual_context(h):
    text = h.say("What's important tomorrow?")
    assert "JARVIS Project Review" in text and "11 AM" in text
    assert "Finish JARVIS documentation" in text and "pending" in text
    assert "An email also asks for it" in text  # the email deadline is merged with the task, not listed twice
    assert NoLLM.calls == 0


def test_focus_states_facts_before_suggestions(h):
    text = h.say("What should I focus on today?")
    fact_pos = text.index("is due tomorrow at 9 AM")
    sugg_pos = text.index("You may want to start with")
    assert fact_pos < sugg_pos
    assert "I can create a work block" in text
    assert h.calendar_client.mutations() == []  # a suggestion is not an action


def test_plan_does_not_touch_calendar_until_confirmed(h):
    plan = h.say("Plan my day")
    assert "I haven't changed your calendar" in plan
    assert "Finish JARVIS documentation" in plan
    assert h.calendar_client.mutations() == []
    prompt = h.say("Add it to my calendar")
    assert "Shall I go ahead?" in prompt and "9 AM to 10 AM" in prompt
    assert h.calendar_client.mutations() == []  # still nothing: waiting for the yes
    done = h.say("yes")
    assert done.startswith("Done.") and "confirmed" in done
    created = [c for c in h.calendar_client.mutations() if c[0] == "create_event"]
    assert len(created) == 2 and all(c[2].attendees == [] for c in created)


def test_plan_blocks_never_overlap_calendar_events(h):
    h.say("Plan my day")
    plan = h.service.last_plan
    event = (ist(25, 11), ist(25, 12))
    for b in plan.blocks:
        assert not (b.start < event[1] and event[0] < b.end and b.start.date() == event[0].date())


def test_vague_answer_does_not_authorize(h):
    h.say("Plan my day")
    h.say("add it to my calendar")
    assert h.say("maybe later, sure") is None  # not an answer: handled normally, nothing executed
    assert h.calendar_client.mutations() == []
    assert h.say("yes") is None  # the confirmation was dropped, a stray yes does nothing


def test_declined_confirmation_changes_nothing(h):
    h.say("Plan my day")
    h.say("add it to my calendar")
    assert h.say("no") == "Okay, I won't do that."
    assert h.calendar_client.mutations() == []


def test_double_yes_cannot_create_duplicates(h):
    h.say("Plan my day")
    h.say("add it to my calendar")
    h.say("yes")
    h.say("add it to my calendar")
    reply = h.say("yes")
    assert "already existed" in reply
    assert len([c for c in h.calendar_client.mutations() if c[0] == "create_event"]) == 2


def test_calendar_failure_is_reported_never_claimed_as_success(h):
    h.say("Plan my day")
    h.say("add it to my calendar")

    def boom(*a, **k):
        raise CalendarOutcomeUnknown("timeout")

    h.calendar_client.create_event = boom
    reply = h.say("yes")
    assert not reply.startswith("Done")
    assert "couldn't confirm" in reply


def test_created_event_is_read_back_before_claiming_success(h):
    h.say("Plan my day")
    h.say("add it to my calendar")
    h.calendar_client.create_event_orig = h.calendar_client.create_event

    def create_but_alter(cid, draft):  # the calendar stores something different from what was asked
        ev = h.calendar_client.create_event_orig(cid, draft)
        h.calendar_client.events[(cid, ev.event_id)] = ev.model_copy(update={"summary": "something else"})
        return ev

    h.calendar_client.create_event = create_but_alter
    reply = h.say("yes")
    assert not reply.startswith("Done")


def test_why_did_you_schedule_that_gives_evidence(h):
    h.say("Plan my day")
    text = h.say("Why did you schedule that?")
    assert "because" in text and "That came from" in text
    assert "chain" not in text.lower()


def test_where_did_you_get_that_names_sources(h):
    h.say("What's important tomorrow?")
    text = h.say("Where did you get that?")
    assert "your calendar" in text and ("an email" in text or "your task list" in text)


def test_prepare_for_project_review(h):
    text = h.say("Prepare me for tomorrow's project review")
    assert "JARVIS Project Review" in text and "Pending tasks" in text and "checklist" in text
    assert "I haven't changed anything" in text
    assert h.calendar_client.mutations() == []


def test_project_status_returns_actual_data(h):
    text = h.say("What is pending for my JARVIS project?")
    assert "Finish JARVIS documentation" in text and "JARVIS Project Review" in text


def test_unknown_project_is_reported_not_invented(h):
    assert "don't have a project" in h.say("What is pending for my Zebra project?")


def test_email_event_missing_from_calendar_is_offered_not_created(tmp_path):
    body = "Your interview is scheduled for Monday at 10 AM."
    h = build_harness(tmp_path, emails=[email_raw(subject="Interview", body=body)], calendar_events=[cal_event("x", "Gym", ist(25, 7), ist(25, 8))])
    text = h.say("What's important Monday?")
    assert "don't see a matching calendar event" in text and "Would you like me to add 'Interview'" in text
    assert h.calendar_client.mutations() == []
    assert h.say("yes").startswith("Done.")
    assert [c[2].summary for c in h.calendar_client.mutations()] == ["Interview"]


def test_calendar_unavailable_is_said_honestly(tmp_path):
    class Down(FakeCalendarClient):
        def list_events(self, *a, **k):
            raise ConnectionError("no network")

        def list_calendars(self):
            raise ConnectionError("no network")

    h = build_harness(tmp_path, emails=[email_raw()], calendar_client=Down())
    text = h.say("What's important tomorrow?")
    assert "couldn't check your calendar" in text
    assert "nothing scheduled" not in text  # silence is never reported as "nothing there"


def test_offline_mode_marks_cloud_sources_unavailable(tmp_path):
    h = scenario_harness(tmp_path, offline=True)
    text = h.say("What should I focus on today?")
    assert "couldn't check your calendar" in text
    assert h.calendar_client.calls == []  # no network call was made


def test_conflicting_calendar_events_are_reported_without_ranking(tmp_path):
    ev = [cal_event("a", "Team sync", ist(25, 16), ist(25, 17)), cal_event("b", "Dentist", ist(25, 16), ist(25, 16, 30))]
    h = build_harness(tmp_path, calendar_events=ev)
    text = h.say("Do I have any conflicts?")
    assert "two calendar events scheduled for tomorrow at 4 PM" in text and "Team sync" in text and "Dentist" in text
    assert "important" not in text.lower() and "should" not in text.lower()


def test_memory_conflict_says_both_and_chooses_none(tmp_path):
    from agent.memory.models import MemoryType

    ev = [cal_event("r", "Project review", ist(28, 11), ist(28, 12))]  # Monday
    h = build_harness(tmp_path, calendar_events=ev, memories=[("My project review is on Friday at 11 AM.", MemoryType.FACT)])
    text = h.say("Any conflicts?")
    assert "conflicting information" in text and "stored memory says" in text and "calendar shows" in text
    assert "says tomorrow" in text and "shows Monday" in text  # tomorrow is Friday in the scenario; neither side is chosen


def test_dependencies_block_and_unblock(tmp_path):
    from agent.tasks.models import TaskPriority

    h = build_harness(tmp_path, tasks=[("Collect dataset", None, TaskPriority.MEDIUM, None), ("Train model", ist(27, 12), TaskPriority.HIGH, None)])
    reply = h.say("Train model depends on collect dataset")
    assert "blocked until" in reply
    assert "waiting for" in h.say("What is blocking train model?")
    plan = h.say("Plan my day")
    assert "blocked until" in plan  # explained, not silently dropped
    collect = next(t for t in h.tasks.list_tasks() if t.title == "Collect dataset")
    h.tasks.complete_task(collect.task_id)
    h.service._invalidate()
    assert "Nothing is blocking" in h.say("What is blocking train model?")


def test_circular_dependency_refused(tmp_path):
    from agent.tasks.models import TaskPriority

    h = build_harness(tmp_path, tasks=[("Write report", None, TaskPriority.MEDIUM, None), ("Collect data", None, TaskPriority.MEDIUM, None)])
    h.say("Write report depends on collect data")
    assert "circle" in h.say("Collect data depends on write report")


def test_references_resolve_from_context(h):
    h.say("What about the project review?")
    assert "tomorrow at 11 AM" in h.say("When is it?")


def test_reference_without_context_falls_through(tmp_path):
    h = build_harness(tmp_path)
    assert h.say("when is it due?") is None  # nothing to refer to: not the intelligence layer's question


def test_tell_me_more_expands_last_briefing(h):
    brief = h.say("Good morning")
    assert "Say 'tell me more'" in brief
    more = h.say("tell me more")
    assert more.startswith("Here are the details") and len(more) > len(brief)


def test_evening_review_reports_real_counts(tmp_path):
    from agent.tasks.models import TaskPriority

    h = build_harness(tmp_path, tasks=[("Done thing", None, TaskPriority.MEDIUM, None)])
    t = h.tasks.list_tasks()[0]
    h.tasks.complete_task(t.task_id)
    text = h.say("evening review")
    assert "completed 1 task" in text and "Done thing" in text
    assert "%" not in text and "productiv" not in text.lower()


def test_activity_timeline_reconstructs_from_stored_events(h):
    h.say("Good morning")  # ingests the snapshot into the timeline
    text = h.say("What happened with my project today?")
    assert "Email received: JARVIS project review" in text and "Task created: Finish JARVIS documentation" in text
    assert "don't have any recorded activity" in h.say("What happened with my project yesterday?")


def test_preferences_are_stored_shown_and_changed(h):
    assert "won't notify you about newsletter" in h.say("Don't notify me about newsletters")
    assert "Not notifying about: newsletter" in h.say("show my preferences")
    assert "again" in h.say("start notifying me about newsletters again")
    assert "newsletter" not in h.say("show my preferences")
    assert "Quiet hours are 22:00 to 07:00" in h.say("set quiet hours from 10 PM to 7 AM")
    assert "after 22:00" in h.say("Don't speak notifications after 10 PM")
    h.say("Remind me one day before project deadlines")
    assert "Reminding you 1 day before project deadline" in " ".join(h.prefs.describe())


def test_no_llm_call_anywhere(h):
    for q in ["Plan my day", "What should I focus on today?", "Any conflicts?", "Good morning", "Prepare me for tomorrow's project review"]:
        h.say(q)
    assert NoLLM.calls == 0


def test_unrelated_utterances_fall_through(h):
    for q in ["What's the capital of France?", "Remind me tomorrow at 9 to call mom", "What tasks do I have today?", "yes"]:
        assert h.say(q) is None
