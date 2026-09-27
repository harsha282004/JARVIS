"""Bounded, capability-based semantic fallback: a last resort for a request the LLM classified as `action_request`
but could not resolve to a concrete tool call.

Root cause this exists for: the same Groq request, at the same temperature, can non-deterministically return a
concrete action ({"name": "gmail_get_message", "arguments": {"latest": true}}) or an incomplete one (action: null)
for a request that is not actually ambiguous -- "What is the last mail I received in my Gmail?" has exactly one
reasonable reading. Retrying the LLM again (already bounded to one repair attempt in `AgentBrain.decide`) reduces how
often this happens but cannot eliminate sampling variance as a matter of principle. This module is the deterministic
safety net for the small set of HIGH-CONFIDENCE concepts worth guaranteeing, so a clearly supported request can never
be stranded by variance alone.

This is explicitly NOT a return to keyword routing as the primary mechanism:
  - the LLM's structured decision is still tried first, every time, and used whenever it resolves;
  - this only runs after that has already failed to produce a concrete action for an `action_request` intent;
  - it recognizes a handful of CONCEPTS (a phrase talks about the newest/latest email, or about unread mail, or asks
    to search/find email about something), not a lookup table of exact sentences -- "Show me my newest email",
    "What's the latest mail in my inbox?" and "Did I get anything recently?" all normalize to the same concept;
  - a request outside these concepts (including genuinely ambiguous ones, e.g. "what should I do about my emails?")
    resolves to nothing here either, and is left to the normal clarification/unsupported path -- this module never
    guesses at something it is not confident about;
  - every concept still produces the SAME validated `GmailAction` object real LLM resolution would (built through
    `integrations.gmail.intents.parse_gmail_action`), so it goes through the identical PermissionManager/tool-router
    path and untrusted-content handling as any other action. It is a fallback for WHICH capability to call, never a
    bypass of anything that decides whether the call may run.
"""

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class SemanticMatch:
    domain: str            # "gmail" (more domains can be added the same way if this proves out for them)
    operation: str         # a name from the domain's capability registry, e.g. "get_latest_received_email"
    action: dict           # the {"name", "arguments"} dict, ready for integrations.gmail.intents.parse_gmail_action


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9\s]", " ", (text or "").lower())).strip()


_MAIL_WORD = r"(?:e-?mails?|mails?|messages?|inbox)"
_GMAIL_WORD = r"(?:gmail|" + _MAIL_WORD + r")"
_RECENCY = r"(?:latest|newest|last|most recent|newest)"
_RECEIVED = r"(?:received|got|receive|get|came in|sent to me|arrived)"

# Ordered: the first pattern that matches wins. Each is a *concept* (recency + mail, unread + mail, ...), not a
# literal sentence, so many real phrasings normalize to the same match.
_GMAIL_PATTERNS: list[tuple[re.Pattern, str, dict]] = [
    # "what's the last mail I received", "show me my newest email", "what was the most recent email I got",
    # "what's the latest mail in my inbox", "read my latest email", "tell me about the newest message",
    # "did I get anything recently", "did I receive anything new", "what's the last thing someone sent me",
    # "check my gmail and tell me the newest message", "check my inbox" (bare -> latest is still the reasonable read)
    (re.compile(rf"\b{_RECENCY}\b.*\b{_MAIL_WORD}\b|\b{_MAIL_WORD}\b.*\b{_RECENCY}\b"),
     "get_latest_received_email", {"name": "gmail_get_message", "arguments": {"latest": True}}),
    (re.compile(rf"\b(?:did i|have i)\b.*\b{_RECEIVED}\b.*\b(?:anything|any\s*{_MAIL_WORD}|new)\b|"
                rf"\b(?:did i|have i)\b.*\b(?:{_MAIL_WORD})\b.*\b(?:recently|new)\b"),
     "get_latest_received_email", {"name": "gmail_get_message", "arguments": {"latest": True}}),
    (re.compile(rf"\bwhat.?s the last thing\b.*\bsent me\b"),
     "get_latest_received_email", {"name": "gmail_get_message", "arguments": {"latest": True}}),
    (re.compile(rf"\bcheck\b.*\b(?:gmail|inbox|mail|email)\b$"),
     "get_latest_received_email", {"name": "gmail_get_message", "arguments": {"latest": True}}),
    # unread
    (re.compile(rf"\bunread\b.*\b{_MAIL_WORD}\b|\b{_MAIL_WORD}\b.*\bunread\b"),
     "get_unread_emails", {"name": "gmail_search", "arguments": {"query": "is:unread"}}),
]

# "find/search emails (from|about|regarding) X" -- the topic itself is free text, still validated (and sanitized to
# the read-only search-operator whitelist) by parse_gmail_action/sanitize_query exactly like an LLM-proposed query.
# Deliberately plain words, never a guessed `from:` operator: "from my professor" is not an email address or name
# sanitize_query could validate as one, and a wrong from: filter would silently return nothing instead of searching.
_SEARCH_PATTERN = re.compile(rf"\b(?:find|search|look for)\b.*\b{_GMAIL_WORD}\b\s*(?:from|for|about|regarding)?\s*(.*)$")


def resolve_gmail_fallback(user_text: str) -> SemanticMatch | None:
    """A high-confidence Gmail concept in `user_text`, or None if nothing matches with enough confidence to act on.
    Never raises; a text that cannot be parsed at all is simply not a match."""
    text = _norm(user_text)
    if not text:
        return None
    for pattern, operation, action in _GMAIL_PATTERNS:
        if pattern.search(text):
            return SemanticMatch("gmail", operation, action)
    m = _SEARCH_PATTERN.search(text)
    if m:
        topic = m.group(1).strip()
        if topic and len(topic) <= 100:  # a topic this long is not a confident match; let it go to clarification
            return SemanticMatch("gmail", "search_emails", {"name": "gmail_search", "arguments": {"query": topic}})
    return None
