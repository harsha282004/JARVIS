"""Natural conversation over REAL services (SQLite, real tools/permissions/confirmations, real hub) with a scripted LLM only where the
agent brain is needed: clarification, elliptical answers, corrections, references to previous results, cancel, confirmations."""

import pytest

from agent.tasks.models import ReminderStatus
from tests.hub_helpers import build_hub_harness
from tests.intelligence_helpers import email_raw, ist
from tests.calendar_helpers import cal_event
from tests.task_helpers import IST
from tests.test_task_actions_engine import Stack, act
from voice.normalize import normalize


@pytest.fixture
def stack(session_factory):
    def make(*replies, **kw):
        return Stack(session_factory, *replies, **kw)

    return make


def scheduled(s):
    return [r for r in s.repo.list_reminders() if r.status is ReminderStatus.SCHEDULED] if hasattr(s.repo, "list_reminders") else \
        [r for r in s.reminders.upcoming_reminders(limit=20)]


# ---- clarification and elliptical answers -------------------------------------------------------------------------------------------

def test_reminder_with_a_time_is_created_and_read_back(stack):
    s = stack(act("create_reminder", message="study", when="6 PM"))
    reply = s.say("remind me at 6 PM to study")
    assert reply.startswith("Okay, I'll remind you") and "6:00 PM" in reply and "study" in reply
    assert [r.message for r in scheduled(s)] == ["study"]


def test_missing_time_is_asked_and_a_bare_part_of_day_completes_it_without_the_llm(stack):
    s = stack(act("create_reminder", message="study", when="tomorrow"))  # only ONE scripted LLM reply: the answer must not need the LLM
    assert s.say("remind me tomorrow to study") == "What time tomorrow?"
    assert s.engine.awaiting_answer() == "clarification"
    reply = s.say("Morning.")
    assert "tomorrow at 9:00 AM" in reply and "study" in reply
    assert len(s.llm.calls) == 1 and s.engine.awaiting_answer() is None
    assert [r.scheduled_at.astimezone(IST).hour for r in scheduled(s)] == [9]


def test_a_clock_time_answer_completes_the_question(stack):
    s = stack(act("create_reminder", message="call mom", when="tomorrow"))
    s.say("remind me tomorrow to call mom")
    assert "tomorrow at 6:00 PM" in s.say("6 PM")
    assert len(scheduled(s)) == 1


def test_a_new_request_is_not_mistaken_for_the_answer(stack):
    s = stack(act("create_reminder", message="study", when="tomorrow"), {"intent": "conversation", "response": "It is 2:30 PM."})
    s.say("remind me tomorrow to study")
    assert s.say("what time is it now") == "It is 2:30 PM."  # dropped the question, handled normally
    assert scheduled(s) == []  # nothing was created from a misheard answer
    assert s.engine.awaiting_answer() is None


def test_cancel_drops_the_open_question(stack):
    s = stack(act("create_reminder", message="study", when="tomorrow"), {"intent": "conversation", "response": "Sure, ask me anything."})
    s.say("remind me tomorrow to study")
    assert s.engine.cancel_pending() is True and s.engine.awaiting_answer() is None
    assert s.say("Morning") == "Sure, ask me anything."  # no longer an answer to anything
    assert scheduled(s) == []


def test_repeated_unusable_answers_stop_the_questioning(stack):
    s = stack(act("create_reminder", message="study", when="whenever"), *[{"intent": "conversation", "response": "Okay."}] * 6)
    s.say("remind me whenever to study")
    for _ in range(5):
        s.say("banana")
    assert s.engine.awaiting_answer() is None and scheduled(s) == []


# ---- corrections --------------------------------------------------------------------------------------------------------------------

def test_make_that_7_pm_replaces_the_reminder_and_does_not_duplicate_it(stack):
    s = stack(act("create_reminder", message="study", when="6 PM"))
    s.say("remind me at 6 PM to study")
    reply = s.say("Make that 7 PM")
    assert reply.startswith("Okay, I've changed it.") and "7:00 PM" in reply
    items = scheduled(s)
    assert len(items) == 1 and items[0].message == "study"  # exactly one, at the new time
    assert items[0].scheduled_at.astimezone(IST).hour == 19


def test_spoken_no_i_meant_tomorrow_moves_the_day_and_keeps_the_time(stack):
    s = stack(act("create_reminder", message="study", when="6 PM"))
    s.say("remind me at 6 PM to study")
    text = normalize("No, I meant tomorrow").text  # what the voice layer hands over
    assert text == "change that to tomorrow"
    reply = s.say(text)
    assert "tomorrow at 6:00 PM" in reply
    assert len(scheduled(s)) == 1


def test_correction_with_nothing_to_correct_is_an_ordinary_sentence(stack):
    s = stack({"intent": "conversation", "response": "I'm not sure what to change."})
    assert s.say("change that to 7 PM") == "I'm not sure what to change."
    assert scheduled(s) == []


def test_a_correction_never_touches_a_reminder_that_is_no_longer_scheduled(stack):
    s = stack(act("create_reminder", message="study", when="6 PM"), {"intent": "conversation", "response": "Nothing to change."})
    s.say("remind me at 6 PM to study")
    s.reminders.cancel_reminder(scheduled(s)[0].reminder_id)
    assert s.say("Make that 7 PM") == "Nothing to change."
    assert scheduled(s) == []


def test_a_correction_to_the_past_is_refused(stack):
    s = stack(act("create_reminder", message="study", when="6 PM"))
    s.say("remind me at 6 PM to study")
    assert "already passed" in s.say("Make that 2 PM")  # it is 2:30 PM now: the same day at 2 PM is gone, nothing is silently moved to tomorrow
    assert len(scheduled(s)) == 1 and scheduled(s)[0].scheduled_at.astimezone(IST).hour == 18


# ---- confirmations are still enforced (voice never lowers the bar) -------------------------------------------------------------------

def test_cancelling_a_reminder_by_voice_still_needs_a_spoken_yes(stack):
    s = stack(act("create_reminder", message="submit assignment", when="6 PM"), act("cancel_reminder", query="assignment"))
    s.say("remind me at 6 PM to submit assignment")
    prompt = s.say("cancel my assignment reminder")
    assert "cancel" in prompt.lower() and s.engine.awaiting_answer() == "confirmation"
    assert len(scheduled(s)) == 1  # nothing happened yet
    assert "Okay" in s.say("yes") and scheduled(s) == []


def test_a_confirmation_can_be_dropped_by_voice_cancel_and_a_later_yes_does_nothing(stack):
    s = stack(act("create_reminder", message="submit assignment", when="6 PM"), act("cancel_reminder", query="assignment"),
              {"intent": "conversation", "response": "There is nothing to confirm."})
    s.say("remind me at 6 PM to submit assignment")
    s.say("cancel my assignment reminder")
    assert s.engine.cancel_pending() is True
    assert s.say("yes") == "There is nothing to confirm."
    assert len(scheduled(s)) == 1


# ---- follow-ups over real calendar/email data ------------------------------------------------------------------------------------------

def hub(tmp_path):
    events = [cal_event("a", "Team sync", ist(24, 16), ist(24, 17)), cal_event("b", "Dentist", ist(24, 11), ist(24, 11, 30)),
              cal_event("c", "Project review", ist(24, 13), ist(24, 15)), cal_event("d", "Standup", ist(25, 9), ist(25, 9, 15))]
    return build_hub_harness(tmp_path, emails=[email_raw("m1", "JARVIS project review"), email_raw("m2", "Hackathon schedule", sender="Ann <ann@example.com>", hours_ago=3)],
                             calendar_events=events)


def test_which_meeting_is_first_and_longest_refer_to_the_schedule_just_read(tmp_path):
    h = hub(tmp_path)
    assert "Dentist" in h.say("What's my schedule today?")
    assert "Dentist" in h.say("What meeting is first?") and "11" in h.say("What meeting is first?")
    longest = h.say("Which meeting is the longest?")
    assert "Project review" in longest and "2 hours" in longest
    assert "Team sync" in h.say("which one is last")
    assert h.say("how many") == "That was 3 meetings."


def test_a_bare_day_after_a_schedule_answer_asks_the_same_question_for_that_day(tmp_path):
    h = hub(tmp_path)
    h.say("What's my schedule today?")
    assert "Standup" in h.say("And tomorrow?") and "Team sync" not in h.say("What about tomorrow")


def test_it_is_resolved_only_when_it_is_one_thing(tmp_path):
    h = hub(tmp_path)
    h.say("What's my schedule tomorrow?")  # one event: Standup
    assert "Standup" in h.say("when is it") and "15 minutes" in h.say("how long is it")
    h.say("What's my schedule today?")  # three events: "it" is ambiguous
    assert h.say("when is it") is None  # not guessed: the normal path asks or explains


def test_who_sent_it_after_an_email_list(tmp_path):
    h = hub(tmp_path)
    h.say("what emails do I have")
    assert "Which email" in h.say("who sent it")
    assert "from" in h.say("who sent the first one").lower() or "It's from" in h.say("who sent the first one")


def test_no_context_means_no_guess(tmp_path):
    h = hub(tmp_path)
    assert h.say("Which meeting is the longest?") is None
    assert h.say("what meeting is first") is None
    assert h.say("and tomorrow") is None


def test_follow_up_context_expires(tmp_path):
    h = hub(tmp_path)
    h.say("What's my schedule today?")
    h.base.clock.advance(minutes=10)
    assert h.say("What meeting is first?") is None


def test_follow_up_answers_carry_provenance_for_where_did_you_get_that(tmp_path):
    h = hub(tmp_path)
    h.say("What's my schedule today?")
    h.say("Which meeting is the longest?")
    assert "calendar" in (h.say("where did you get that?") or "").lower()


def test_follow_ups_do_not_call_the_llm_or_change_anything(tmp_path):
    h = hub(tmp_path)  # the harness's LLM refuses every call, so a pass proves none was made
    before = list(h.calendar_client.mutations())
    h.say("What's my schedule today?")
    h.say("What meeting is first?")
    h.say("Which meeting is the longest?")
    assert h.calendar_client.mutations() == before == []
