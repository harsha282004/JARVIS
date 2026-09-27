"""Natural-language time understanding: many real phrasings must all resolve to the same deterministic
GET_CURRENT_TIME answer (agent/intelligence/router.py's `_clock_answer`), read from the real clock/timezone, never
guessed by a language model -- and location questions resolve through agent/intelligence/worldtime.py's real IANA
timezone data, with multi-timezone countries always asking which city rather than silently picking one.

Uses the same fixed-clock harness as the existing time test (Thursday 2026-09-24 09:00 IST)."""

import pytest

from tests.intelligence_helpers import build_harness


@pytest.mark.parametrize("phrase", [
    "What time is it?", "What is the time?", "What's the time now?", "What is time now?",
    "Tell me the current time.", "Can you tell me what time it is?", "Do you know what time it is?",
    "What's the current time right now?", "what time now", "give me the time", "What time is it right now?",
])
def test_natural_language_time_variants_all_resolve_to_the_same_deterministic_answer(phrase, tmp_path):
    h = build_harness(tmp_path)
    assert h.say(phrase) == "It's 9:00 AM."


@pytest.mark.parametrize("phrase,place", [
    ("What time is it in London?", "London"), ("What's the time in Tokyo?", "Tokyo"),
    ("Tell me the current time in New York.", "New York"), ("What time is it in Japan?", "Japan"),
    ("What's the current time in New York?", "New York"), ("What time is it in Bengaluru?", "Bengaluru"),
])
def test_location_time_questions_use_a_real_timezone_not_the_locals(phrase, place, tmp_path):
    h = build_harness(tmp_path)
    reply = h.say(phrase)
    assert reply is not None and place.lower() in reply.lower()
    assert reply != "It's 9:00 AM."  # never just the local answer relabelled


@pytest.mark.parametrize("phrase", [
    "What time is it in Canada?", "What's the current time in the US?", "What time is it in Australia?",
    "What time is it in Russia?", "Tell me the current time in Brazil.",
])
def test_multi_timezone_countries_ask_for_clarification_never_pick_a_random_city(phrase, tmp_path):
    h = build_harness(tmp_path)
    reply = h.say(phrase)
    assert reply is not None
    assert "multiple time zones" in reply and "?" in reply


def test_unknown_location_says_so_honestly_instead_of_fabricating(tmp_path):
    h = build_harness(tmp_path)
    reply = h.say("What time is it in Narnia?")
    assert reply is not None and "don't have a time zone" in reply


def test_ordinary_time_of_meeting_question_is_never_treated_as_a_clock_question(tmp_path):
    """The deterministic time path must not swallow unrelated "time" questions about specific things."""
    h = build_harness(tmp_path)
    assert h.say("what time is the meeting") is None
    assert h.say("what time is my flight") is None
