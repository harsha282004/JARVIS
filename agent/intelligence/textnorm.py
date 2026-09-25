"""Deterministic text normalization and similarity for matching the same thing across sources.

"Project review meeting" (email), "Final Year Project Review" (calendar) and "Prepare project review slides" (task) share
distinctive words. Matching therefore works on normalized token sets: stop words, weekdays, months, times and generic
"meeting"-type words are removed, then two scores are combined (Jaccard for exact overlap, overlap coefficient for "one title
is a shortened form of the other"). A single shared generic word is never enough. No model is involved, so the same input always
gives the same answer and every match can be explained.
"""

import re

_TOKEN = re.compile(r"[a-z0-9]+")

STOP = frozenset("""a an the and or of for to on at in is are was be been will would shall should can could with from by before after
until till your you my our their this that these those it its as into about please kindly regarding re fwd fw update reminder
today tomorrow tonight yesterday next last coming monday tuesday wednesday thursday friday saturday sunday am pm noon midnight
morning afternoon evening night week weekend month day days hours hour minutes minute
jan feb mar apr may jun jul aug sep sept oct nov dec january february march april june july august september october november december
scheduled scheduling schedule due date time new final""".split())

# Words that say WHAT KIND of thing it is rather than WHICH one. They carry no identity on their own.
TYPE_WORDS = frozenset({"meeting", "meet", "call", "session", "sync", "catchup", "event", "appointment", "task", "todo", "item"})

# Weak content words: alone they never justify a match ("project" appears in every project-related title).
GENERIC = frozenset({"project", "review", "work", "deadline", "submission", "class", "course", "presentation", "slide"})

ACTION_VERBS = frozenset({
    "submit", "send", "finish", "complete", "prepare", "write", "finalize", "finalise", "upload", "email", "read", "revise",
    "practice", "practise", "fill", "sign", "pay", "register", "apply", "attend", "create", "make", "do", "begin", "collect",
    "deploy", "fix",
})

_IRREGULAR = {"slides": "slide", "docs": "documentation", "doc": "documentation", "presentation": "presentation"}


def _stem(word: str) -> str:
    if word in _IRREGULAR:
        return _IRREGULAR[word]
    if len(word) > 4 and word.endswith("ies"):
        return word[:-3] + "y"
    if len(word) > 3 and word.endswith("s") and not word.endswith("ss"):
        return word[:-1]
    return word


def tokens(text: str, *, drop_verbs: bool = False) -> frozenset[str]:
    """Normalized identity tokens of a title or sentence."""
    out = set()
    for raw in _TOKEN.findall((text or "").lower()):
        if raw.isdigit() or len(raw) < 2 or raw in STOP or raw in TYPE_WORDS:
            continue
        word = _stem(raw)
        if drop_verbs and word in ACTION_VERBS:
            continue
        out.add(word)
    return frozenset(out)


def distinctive(toks: frozenset[str]) -> frozenset[str]:
    return frozenset(t for t in toks if t not in GENERIC and t not in ACTION_VERBS)


def similarity(a: frozenset[str], b: frozenset[str]) -> float:
    """0..1. Both Jaccard and the overlap coefficient contribute, so "JARVIS project review" and "JARVIS Project Review meeting"
    match strongly while "Project review" and "Project deadline" do not."""
    if not a or not b:
        return 0.0
    shared = a & b
    if not shared:
        return 0.0
    jaccard = len(shared) / len(a | b)
    overlap = len(shared) / min(len(a), len(b))
    return 0.5 * jaccard + 0.5 * overlap


def same_thing_score(title_a: str, title_b: str, *, verbs: bool = False) -> tuple[float, frozenset[str]]:
    """(score, shared tokens). A match needs at least one shared DISTINCTIVE word, or two shared generic words with a high score;
    otherwise the score is capped below any merge threshold."""
    a, b = tokens(title_a, drop_verbs=not verbs), tokens(title_b, drop_verbs=not verbs)
    score = similarity(a, b)
    shared = a & b
    if score and not distinctive(shared) and len(shared) < 2:
        score = min(score, 0.3)
    da, db = distinctive(a), distinctive(b)
    if score and da and db and not (da & db):
        score = min(score, 0.3)  # each names something different ("JARVIS review" vs "Capstone review"): never the same thing
    return score, shared


def contains_phrase(text: str, phrase: str) -> bool:
    """Whole-word, case-insensitive containment of `phrase` in `text`."""
    if not phrase.strip():
        return False
    return re.search(rf"(?<![A-Za-z0-9]){re.escape(phrase.strip())}(?![A-Za-z0-9])", text or "", re.I) is not None
