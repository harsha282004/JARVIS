"""Deterministic text normalization for deduplication and relevance matching."""

import re

MIN_KEYWORD_CHARS = 3

_STOPWORDS = frozenset(
    """a an the is are was were be been being am do does did done have has had having i me my mine we
    you your yours he she it its they them their this that these those what which who whom whose how
    when where why to of for in on at by with from about into as and or but if then so not no yes can
    could would should will shall may might must than too very just also please tell know user usually
    typically really often currently now""".split()
)


def normalize_content(text: str) -> str:
    """Lowercase, drop punctuation, collapse whitespace: the deduplication key."""
    return " ".join(re.sub(r"[^a-z0-9 ]+", " ", text.lower()).split())


def normalize_slot(text: str) -> str:
    """Canonical topic key: 'Favorite  Programming Language' -> 'programming language'."""
    words = [w for w in normalize_content(text).split() if w not in {"favorite", "favourite", "preferred"}]
    return " ".join(words)[:120]


def _stem(word: str) -> str:
    return word[:-1] if len(word) > 3 and word.endswith("s") and not word.endswith("ss") else word


def keywords(text: str) -> list[str]:
    """Distinct, lightly stemmed content words of a query (order preserved)."""
    seen: dict[str, None] = {}
    for word in normalize_content(text).split():
        if len(word) >= MIN_KEYWORD_CHARS and word not in _STOPWORDS:
            seen.setdefault(_stem(word))
    return list(seen)
