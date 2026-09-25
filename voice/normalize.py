"""Turning a raw transcript into something the assistant should act on.

Spoken input is untrusted text: it is cleaned here (fillers, spoken punctuation, the wake word, self-corrections) and
classified as a barge-in / cancel / hesitation *before* it can reach the agent, so "Stop" is never run as a task.
Nothing here executes anything or touches a tool.
"""

import re
from dataclasses import dataclass
from enum import StrEnum


class Control(StrEnum):
    NONE = "none"
    STOP = "stop"            # stop talking now ("Stop", "That's enough", "JARVIS stop")
    CANCEL = "cancel"        # abandon the current/pending request ("Cancel", "Never mind", "Actually don't do that")
    WAIT = "wait"            # pause, the user is thinking ("Wait", "Hold on")


_WAKE = r"(?:hey|hi|ok|okay)?\s*(?:jarvis|jarvice|service)"
_LEADING_WAKE = re.compile(rf"^\s*{_WAKE}\b[\s,.:;!-]*", re.I)
_FILLERS = re.compile(r"\b(?:u+h+m*|u+m+|e+r+m*|a+h+m*|hm+|mm+|you know|i mean)\b[\s,.…]*", re.I)
_ELLIPSIS = re.compile(r"\.{2,}|…")
_SPOKEN_PUNCT = {"comma": ",", "period": ".", "full stop": ".", "question mark": "?"}
_SPOKEN_PUNCT_RE = re.compile(r"\s*\b(" + "|".join(map(re.escape, _SPOKEN_PUNCT)) + r")\b\s*", re.I)
# A spoken correction of the previous request: "No, I meant tomorrow" / "Sorry, I meant 6 PM" / "Correction: 6 PM".
# It becomes the canonical "change that to <X>", which the conversation applies to the reminder it just created
# (cancel + recreate, never a second reminder). It is not applied blindly: with nothing recent to correct it is an ordinary sentence.
_SELF_CORRECTION = re.compile(r"^(?:(?:no|nope|sorry|actually|wait|oops)[, ]+)*(?:i\s+(?:meant|mean)|correction[:,]?|make\s+(?:it|that)|change\s+(?:it|that)\s+to)\s+(.+)$", re.I)
_RETRACT = re.compile(r"^(?:actually[, ]+)?(?:don'?t|do not)\s+do\s+that\b|^(?:actually[, ]+)?(?:never\s*mind|forget\s+(?:it|that))\b|^cancel\b(?:\s+that|\s+it|\s+the\s+\w+)?$", re.I)

_STOP_WORDS = re.compile(
    r"^(?:stop|stop\s+(?:it|talking|that|please)|be\s+quiet|quiet|shut\s+up|silence|that'?s\s+enough|enough|okay\s+stop|ok\s+stop|"
    r"stop\s+speaking|that\s+will\s+do|sop|stopp|stob|stap|top)$", re.I)  # last four: how a small recognizer often hears a lone "Stop"
_WAIT_WORDS = re.compile(r"^(?:wait|hold\s+on|hang\s+on|one\s+(?:moment|second|sec)|just\s+a\s+(?:moment|second|sec))$", re.I)
_CANCEL_WORDS = re.compile(r"^(?:cancel(?:\s+(?:that|it|this))?|never\s*mind|forget\s+(?:it|that)|abort|scratch\s+that|"
                           r"actually[, ]+(?:don'?t|do\s+not)\s+do\s+that|don'?t\s+do\s+that)$", re.I)


@dataclass(frozen=True)
class Normalized:
    text: str                # cleaned text to hand to the assistant ("" if there is nothing to act on)
    control: Control         # STOP/CANCEL/WAIT are handled by the voice layer, never sent to the agent
    corrected: bool = False  # the speaker corrected themselves mid-sentence
    changed: bool = False


def _tidy(text: str) -> str:
    text = re.sub(r"\s+([,.?!])", r"\1", text)
    text = re.sub(r"([,.?!])\1+", r"\1", text)
    return re.sub(r"\s+", " ", text).strip(" ,;:-")


def control_of(text: str) -> Control:
    """Classify an utterance that is *only* a control word. Longer sentences are never controls, so
    "cancel my dentist appointment" stays a real request that goes through the normal agent and confirmation path."""
    bare = re.sub(rf"^{_WAKE}\b[\s,.:;!-]*", "", text.strip().lower(), flags=re.I).strip(" ,.!?…")
    bare = re.sub(r"\s+", " ", bare)
    if not bare:
        return Control.NONE
    if _STOP_WORDS.match(bare):
        return Control.STOP
    if _WAIT_WORDS.match(bare):
        return Control.WAIT
    if _CANCEL_WORDS.match(bare):
        return Control.CANCEL
    return Control.NONE


def normalize(raw: str) -> Normalized:
    """Clean a transcript. Never invents words: it only removes fillers/wake word and applies a spoken self-correction."""
    if not raw or not raw.strip():
        return Normalized("", Control.NONE)
    control = control_of(raw)
    if control is not Control.NONE:
        return Normalized("", control, changed=True)
    text = raw.strip()
    text = _LEADING_WAKE.sub("", text)
    text = _SPOKEN_PUNCT_RE.sub(lambda m: _SPOKEN_PUNCT[m.group(1).lower()] + " ", text)
    text = _ELLIPSIS.sub(" ", text)
    text = _FILLERS.sub(" ", text)
    corrected = False
    match = _SELF_CORRECTION.match(_tidy(text))
    if match:
        text, corrected = f"change that to {match.group(1)}", True
    text = _tidy(text)
    if _RETRACT.match(text) or not re.search(r"\w", text):
        return Normalized("", Control.CANCEL if text else Control.NONE, changed=True)
    return Normalized(text, Control.NONE, corrected=corrected, changed=text != raw.strip())
