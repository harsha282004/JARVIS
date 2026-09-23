"""Rule-based extraction of memory candidates from what the USER said.

Conservative on purpose: only clear first-person statements matching a
fixed pattern become candidates. Questions, hedged or hypothetical
sentences, and everything else are ignored, and the whole utterance is never
stored. It runs on the user's words only, never on model output, so the LLM
cannot cause a memory to be written. Every candidate is EXPLICIT; inference
is not attempted (and would be capped at LOW / confirmation-only anyway).
"""

import re
from collections.abc import Callable

from agent.memory.models import (
    Confidence,
    MemoryBasis,
    MemoryCandidate,
    MemorySource,
    MemoryType,
)
from agent.memory.normalize import normalize_slot

_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+|\n+")
_FILLER = re.compile(r"^(?:(?:actually|well|so|also|oh|um|uh|okay|ok|hey|jarvis|yes|yeah|no)\b[\s,]*)+", re.IGNORECASE)
_CORRECTION_CUE = re.compile(
    r"\b(actually|correction|i meant|changed my mind|no longer|not anymore|anymore|these days|i now|is now|are now|instead of|rather than|switched to)\b|,\s*not\s",
    re.IGNORECASE,
)
_HEDGE = re.compile(
    r"\b(maybe|perhaps|probably|might|possibly|i think|i guess|not sure|sometimes|if i|wish|hopefully|supposedly)\b",
    re.IGNORECASE,
)
_QUESTION_START = re.compile(
    r"^(what|which|who|whom|whose|when|where|why|how|do|does|did|is|are|am|can|could|would|should|will|tell me|remind me)\b",
    re.IGNORECASE,
)
_OLD_VALUE = r"(?:,?\s*(?:not|instead of|rather than)\s+(?P<old>[^,.]+))?"
_NOT_PROFILE = re.compile(r"^(bit|little|lot|few|fan|huge|big|kind|sort|much|way|good|bad|great)\b", re.IGNORECASE)


def _clean(value: str) -> str:
    return " ".join(value.strip(" \t\"'.,;:!").split())


def third_person(text: str) -> str:
    """'my cat is named Tom' -> "the user's cat is named Tom"."""
    text = re.sub(r"\bI'm\b|\bI am\b", "the user is", text, flags=re.IGNORECASE)
    text = re.sub(r"\bI've\b|\bI have\b", "the user has", text, flags=re.IGNORECASE)
    text = re.sub(r"\bI'll\b|\bI will\b", "the user will", text, flags=re.IGNORECASE)
    text = re.sub(r"\bmy\b", "the user's", text, flags=re.IGNORECASE)
    text = re.sub(r"\b(?:me|myself)\b|\bI\b", "the user", text)
    return text[0].upper() + text[1:] if text else text


class _Rule:
    def __init__(
        self,
        pattern: str,
        type_: MemoryType,
        render: Callable[[re.Match[str]], str],
        slot: Callable[[re.Match[str]], str | None] = lambda m: None,
        guard: Callable[[re.Match[str]], bool] = lambda m: True,
    ):
        self.pattern = re.compile(pattern, re.IGNORECASE)
        self.type = type_
        self.render = render
        self.slot = slot
        self.guard = guard


_LIKE_VERBS = {
    "like": "likes", "love": "loves", "enjoy": "enjoys", "adore": "adores",
    "hate": "dislikes", "dislike": "dislikes",
}

_RULES: list[_Rule] = [
    _Rule(
        r"^my (?:favou?rite|preferred) (?P<slot>[\w\s-]{2,50}?) (?:is|are|would be)(?: now)? (?P<val>[^,]+?)" + _OLD_VALUE + r"$",
        MemoryType.PREFERENCE,
        lambda m: f"User's favorite {_clean(m['slot'])} is {_clean(m['val'])}",
        lambda m: normalize_slot(m["slot"]),
    ),
    _Rule(
        r"^i (?:now |really |generally |usually |much )?prefer (?P<val>.+?)(?: (?:for|when|in) (?P<dom>[^,]+?))?" + _OLD_VALUE + r"$",
        MemoryType.PREFERENCE,
        lambda m: f"User prefers {_clean(m['val'])}" + (f" for {_clean(m['dom'])}" if m["dom"] else ""),
        lambda m: normalize_slot(m["dom"]) if m["dom"] else "prefer",
    ),
    _Rule(
        r"^i (?:really |absolutely |truly )?(?P<verb>like|love|enjoy|adore|hate|dislike) (?P<val>[^,]+?)" + _OLD_VALUE + r"$",
        MemoryType.PREFERENCE,
        lambda m: f"User {_LIKE_VERBS[m['verb'].lower()]} {_clean(m['val'])}",
        guard=lambda m: len(m["val"].split()) <= 12 and not re.match(r"^(it|that|this|you)\b", m["val"], re.I),
    ),
    _Rule(
        r"^i(?: am|'m) (?:currently |now )?(?:preparing|studying|training) for (?P<x>.+)$",
        MemoryType.GOAL,
        lambda m: f"User is preparing for {_clean(m['x'])}",
    ),
    _Rule(
        r"^i (?:want|hope|plan|aim|intend|would like) to (?P<x>.+)$",
        MemoryType.GOAL,
        lambda m: f"User wants to {_clean(m['x'])}",
        guard=lambda m: True,
    ),
    _Rule(
        r"^my (?:goal|dream|aim|plan) is (?:to )?(?P<x>.+)$",
        MemoryType.GOAL,
        lambda m: f"User's goal is to {_clean(m['x'])}",
    ),
    _Rule(
        r"^i(?: am|'m) (?:currently |now )?(?:working on|building|developing|making) (?P<x>.+)$",
        MemoryType.CONTEXT,
        lambda m: f"User is currently working on {_clean(m['x'])}",
        lambda m: "current work",
    ),
    _Rule(
        r"^my name is (?P<x>[\w' -]{1,40})$",
        MemoryType.PROFILE,
        lambda m: f"User's name is {_clean(m['x'])}",
        lambda m: "name",
    ),
    _Rule(
        r"^i(?: am|'m) (?P<x>(?:a|an) [\w' -]{2,60})$",
        MemoryType.PROFILE,
        lambda m: f"User is {_clean(m['x'])}",
        guard=lambda m: not _NOT_PROFILE.match(re.sub(r"^(?:a|an)\s+", "", m["x"], flags=re.I)) and len(m["x"].split()) <= 7,
    ),
    _Rule(
        r"^i(?: am|'m)? ?(?:study|studying|studied) (?P<x>.+)$",
        MemoryType.FACT,
        lambda m: f"User studies {_clean(m['x'])}",
        lambda m: "study",
    ),
    _Rule(
        r"^i work as (?P<x>.+)$",
        MemoryType.FACT,
        lambda m: f"User works as {_clean(m['x'])}",
        lambda m: "occupation",
    ),
    _Rule(
        r"^i work (?:at|for) (?P<x>.+)$",
        MemoryType.FACT,
        lambda m: f"User works at {_clean(m['x'])}",
        lambda m: "employer",
    ),
    _Rule(
        r"^i live in (?P<x>.+)$",
        MemoryType.FACT,
        lambda m: f"User lives in {_clean(m['x'])}",
        lambda m: "residence",
    ),
    _Rule(
        r"^i speak (?P<x>.+)$",
        MemoryType.FACT,
        lambda m: f"User speaks {_clean(m['x'])}",
    ),
]

_REMEMBER = re.compile(r"^(?:please )?remember(?: that)? (?P<x>.+)$", re.IGNORECASE)


class RuleBasedExtractor:
    def extract(self, user_text: str) -> list[MemoryCandidate]:
        candidates: list[MemoryCandidate] = []
        for sentence in _SENTENCE_SPLIT.split(user_text.strip()):
            candidate = self._from_sentence(sentence.strip())
            if candidate is not None:
                candidates.append(candidate)
        return candidates

    def _from_sentence(self, sentence: str) -> MemoryCandidate | None:
        if not sentence or sentence.endswith("?"):
            return None
        correction = bool(_CORRECTION_CUE.search(sentence))
        body = _clean(_FILLER.sub("", sentence)) if sentence else ""
        if not body or _QUESTION_START.match(body) or _HEDGE.search(body):
            return None
        explicit_command = False
        remembered = _REMEMBER.match(body)
        if remembered:
            explicit_command = True
            body = _clean(remembered["x"])
            if not body or _HEDGE.search(body):
                return None

        for rule in _RULES:
            match = rule.pattern.match(body)
            if match and rule.guard(match):
                return self._build(rule.type, rule.render(match), rule.slot(match), match, correction)
        if explicit_command:
            # "Remember that <anything>": the user asked for it explicitly.
            return self._build(MemoryType.FACT, third_person(body), None, None, correction)
        return None

    @staticmethod
    def _build(
        type_: MemoryType, content: str, slot: str | None, match: re.Match[str] | None, correction: bool
    ) -> MemoryCandidate | None:
        old = match.groupdict().get("old") if match else None
        retracts = _clean(old) if old else None
        try:
            return MemoryCandidate(
                type=type_,
                content=content if content.endswith(".") else content + ".",
                source=MemorySource.USER_CORRECTION if (correction or retracts) else MemorySource.EXPLICIT_USER_STATEMENT,
                basis=MemoryBasis.EXPLICIT,
                confidence=Confidence.HIGH,
                slot=slot or None,
                retracts=retracts,
            )
        except ValueError:
            return None  # e.g. over-long content: skip rather than truncate
