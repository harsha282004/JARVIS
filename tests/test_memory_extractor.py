"""Rule-based memory extraction: explicit statements only, conservative."""

import pytest

from agent.memory.extractor import RuleBasedExtractor, third_person
from agent.memory.models import Confidence, MemoryBasis, MemorySource, MemoryType
from agent.memory.policy import MemoryDecision, MemoryPolicy

ex = RuleBasedExtractor()


def one(text):
    found = ex.extract(text)
    assert len(found) == 1, found
    return found[0]


def test_explicit_preference():
    c = one("My favorite programming language is Java.")
    assert (c.type, c.content, c.slot) == (MemoryType.PREFERENCE, "User's favorite programming language is Java.", "programming language")
    assert c.source is MemorySource.EXPLICIT_USER_STATEMENT
    assert c.basis is MemoryBasis.EXPLICIT and c.confidence is Confidence.HIGH


def test_goal_from_the_spec_example():
    c = one("I am preparing for a software developer interview.")
    assert (c.type, c.content) == (MemoryType.GOAL, "User is preparing for a software developer interview.")


@pytest.mark.parametrize(
    "text,type_,content",
    [
        ("I prefer Python for backend development.", MemoryType.PREFERENCE, "User prefers Python for backend development."),
        ("I love Java", MemoryType.PREFERENCE, "User loves Java."),
        ("I hate meetings", MemoryType.PREFERENCE, "User dislikes meetings."),
        ("I want to become a software developer.", MemoryType.GOAL, "User wants to become a software developer."),
        ("My goal is to learn Rust", MemoryType.GOAL, "User's goal is to learn Rust."),
        ("I'm currently working on the JARVIS project.", MemoryType.CONTEXT, "User is currently working on the JARVIS project."),
        ("I am a final-year student.", MemoryType.PROFILE, "User is a final-year student."),
        ("My name is Asha", MemoryType.PROFILE, "User's name is Asha."),
        ("I study Computer Science.", MemoryType.FACT, "User studies Computer Science."),
        ("I live in Bengaluru", MemoryType.FACT, "User lives in Bengaluru."),
        ("Remember that my cat is named Tom.", MemoryType.FACT, "The user's cat is named Tom."),
    ],
)
def test_patterns(text, type_, content):
    c = one(text)
    assert (c.type, c.content) == (type_, content)


@pytest.mark.parametrize(
    "text",
    [
        "What programming language do I prefer?",
        "Do I like Java?",
        "Tell me about Python.",
        "Maybe I prefer Java.",
        "I think I like Python",
        "I might want to learn Go.",
        "If I prefer Java then what?",
        "I am a bit tired.",
        "I love it.",
        "Hello JARVIS, how are you today",
        "She prefers Java.",
        "",
    ],
)
def test_questions_hedges_and_non_statements_produce_nothing(text):
    assert ex.extract(text) == []


def test_only_the_statement_is_kept_not_the_whole_utterance():
    found = ex.extract("Hey, thanks for that. I prefer Java for backend work. What time is it?")
    assert [c.content for c in found] == ["User prefers Java for backend work."]


def test_correction_with_retraction():
    c = one("Actually, my favorite language is Python, not Java.")
    assert c.source is MemorySource.USER_CORRECTION and c.basis is MemoryBasis.EXPLICIT
    assert c.content == "User's favorite language is Python." and c.retracts == "Java"


def test_correction_cue_without_retraction():
    c = one("I now prefer Java.")
    assert c.source is MemorySource.USER_CORRECTION and c.slot == "prefer"


def test_every_extracted_candidate_is_explicit_never_inferred():
    for text in ["I love Java", "I study math", "Remember that the sky is blue", "I am an engineer"]:
        assert all(c.basis is MemoryBasis.EXPLICIT for c in ex.extract(text))


def test_extraction_is_deterministic():
    text = "I prefer Python for backend development."
    assert ex.extract(text) == ex.extract(text)


def test_overlong_statements_are_skipped_not_truncated():
    assert ex.extract("I love " + "very " * 100 + "much") == []


def test_extracted_secrets_are_rejected_by_policy_not_stored():
    for text in ["Remember that my password is hunter2.", "My favorite pin is 4242 and the api key is abc"]:
        for c in ex.extract(text):
            assert MemoryPolicy().evaluate(c).decision is MemoryDecision.REJECT


def test_extracted_sensitive_facts_need_confirmation():
    c = one("Remember that I was diagnosed with diabetes.")
    assert MemoryPolicy().evaluate(c).decision is MemoryDecision.CONFIRM


def test_third_person_rewrite():
    assert third_person("I'm learning and my laptop is mine") == "The user is learning and the user's laptop is mine"
