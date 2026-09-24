"""Deterministic matching of a spoken description to stored titles/messages.

Used to identify "my JARVIS documentation task" without letting the model
invent an id: the caller gets the candidates and must ask when there is more
than one. No fuzzy or semantic matching, so it never quietly picks the wrong item.
"""

import re

_STOPWORDS = frozenset({
    "the", "a", "an", "my", "our", "task", "tasks", "reminder", "reminders", "to", "please", "about", "for",
    "of", "on", "that", "this", "one", "called", "named", "i", "me", "it", "todo", "item", "at", "am", "pm",
})
_PREFIX_MIN = 4


def _words(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", text.lower())


def query_tokens(query: str) -> list[str]:
    return [w for w in _words(query) if w not in _STOPWORDS]


def _token_matches(wanted: str, have: list[str]) -> bool:
    return any(h == wanted or (len(wanted) >= _PREFIX_MIN and h.startswith(wanted)) for h in have)


def find_matches(query: str, candidates: list[tuple[str, str]]) -> list[str]:
    """`candidates` are (id, text). Returns the ids that match `query`, in the given order.

    A candidate whose words are exactly the query's words wins alone; otherwise every
    meaningful query word must appear in the candidate (whole word, or a prefix of >= 4 letters).
    An empty/meaningless query matches nothing.
    """
    wanted = query_tokens(query)
    if not wanted:
        return []
    exact = [cid for cid, text in candidates if query_tokens(text) == wanted]
    if exact:
        return exact
    return [cid for cid, text in candidates if all(_token_matches(w, _words(text)) for w in wanted)]
