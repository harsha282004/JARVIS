"""agent.brain.semantic_fallback: the bounded, capability-based deterministic fallback for the small set of
high-confidence Gmail concepts, tried only after real LLM resolution has already failed (see agent/brain/brain.py).
Concept-based, not a lookup table of exact sentences: many real phrasings must normalize to the same match, and a
genuinely ambiguous request must resolve to nothing."""

import pytest

from agent.brain.semantic_fallback import resolve_gmail_fallback


LATEST_EMAIL_PHRASES = [
    "What is the last mail I received in my Gmail?",
    "What's my latest email?",
    "Show me my newest email.",
    "What was the most recent email I received?",
    "Read my latest email.",
    "Tell me about the newest message in my inbox.",
    "Did I receive anything recently?",
    "Did I get anything new?",
    "Check Gmail and show me the latest message.",
    "What's the last thing someone sent me?",
    "Can you check my inbox?",
    "check my gmail",
]


@pytest.mark.parametrize("phrase", LATEST_EMAIL_PHRASES)
def test_recognizes_the_latest_received_email_concept_across_many_phrasings(phrase):
    match = resolve_gmail_fallback(phrase)
    assert match is not None
    assert match.domain == "gmail" and match.operation == "get_latest_received_email"
    assert match.action == {"name": "gmail_get_message", "arguments": {"latest": True}}


@pytest.mark.parametrize("phrase", ["Show my unread emails.", "Do I have unread mail?", "Summarize my unread mail."])
def test_recognizes_the_unread_concept(phrase):
    match = resolve_gmail_fallback(phrase)
    assert match is not None and match.action == {"name": "gmail_search", "arguments": {"query": "is:unread"}}


def test_recognizes_a_search_topic():
    match = resolve_gmail_fallback("Find emails about my internship")
    assert match is not None and match.domain == "gmail" and match.operation == "search_emails"
    assert "internship" in match.action["arguments"]["query"]


def test_search_topic_never_becomes_a_fabricated_from_operator():
    """"from my professor" is not a real email address/name; guessing a from: filter would silently return nothing
    instead of searching -- the fallback must keep it as plain search words."""
    match = resolve_gmail_fallback("Search Gmail for emails from my professor.")
    assert match is not None and match.action["arguments"]["query"] == "my professor"
    assert not match.action["arguments"]["query"].startswith("from:")


@pytest.mark.parametrize("phrase", [
    "What should I do about my emails?", "Email is confusing.", "Do something with Gmail.", "Handle my inbox.",
    "", "   ", "Hello there", "What time is it?", "Turn off the lights",
])
def test_ambiguous_or_unrelated_text_matches_nothing(phrase):
    """The fallback must never guess: a genuinely ambiguous or unrelated request resolves to None, leaving it to the
    normal clarification/unsupported path."""
    assert resolve_gmail_fallback(phrase) is None


def test_search_topic_too_long_is_not_a_confident_match():
    long_topic = "about " + ("word " * 30)
    assert resolve_gmail_fallback(f"find emails {long_topic}") is None


def test_never_raises_on_odd_input():
    for bad in (None, "\x00\x01", "🎉" * 50, "a" * 5000):
        resolve_gmail_fallback(bad)  # must not raise
