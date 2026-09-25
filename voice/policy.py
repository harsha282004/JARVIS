"""Voice policy: what may be spoken, how long, and when (Do Not Disturb, priorities, spoken summaries).

Pure functions and one small class; no audio, no I/O, so it is fully unit-testable. The engine asks; this decides.
"""

import re
from datetime import datetime, time

from agent.tasks.notifications import PRIORITIES, normalize_priority
from voice.settings import VoiceSettings


def _clock(value: str) -> time:
    hours, minutes = value.split(":")
    return time(int(hours), int(minutes))


def in_schedule(now: time, start: time, end: time) -> bool:
    """True if `now` is inside [start, end). A window that crosses midnight (22:00-07:00) is handled; start == end means never."""
    if start == end:
        return False
    return start <= now < end if start < end else (now >= start or now < end)


class VoicePolicy:
    """Decides, from the current settings, whether an announcement may be spoken."""

    def __init__(self, settings_provider, local_now):
        self._settings = settings_provider   # () -> VoiceSettings
        self._now = local_now                # () -> datetime in the user's time zone

    def dnd_active(self) -> bool:
        s: VoiceSettings = self._settings()
        if s.dnd_enabled:
            return True
        if s.dnd_schedule_enabled:
            try:
                return in_schedule(self._now().time(), _clock(s.dnd_start), _clock(s.dnd_end))
            except (ValueError, TypeError):
                return False
        return False

    def may_speak(self, priority: str) -> tuple[bool, str]:
        """(allowed, reason). Muting silences everything the user did not just ask for; DND silences non-critical
        announcements (critical passes only if the user allowed that); voice notifications can be switched off entirely."""
        s: VoiceSettings = self._settings()
        priority = normalize_priority(priority)
        if s.voice_muted:
            return False, "voice muted"
        if not s.voice_notifications and priority != "critical":
            return False, "voice notifications off"
        if self.dnd_active():
            if priority == "critical" and s.dnd_allow_critical:
                return True, "critical overrides do-not-disturb"
            return False, "do not disturb"
        return True, "allowed"

    def may_interrupt_conversation(self, priority: str) -> bool:
        """Only critical alerts may cut in while the user is mid-conversation; everything else waits."""
        return normalize_priority(priority) == "critical" and self.may_speak(priority)[0]


_SENTENCE = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9\"'(])")
_MARKDOWN = re.compile(r"[*_`#>]+|\[([^\]]+)\]\([^)]+\)")
_LIST_MARK = re.compile(r"^\s*(?:[-•]|\d+[.)])\s+", re.M)


def clean_for_speech(text: str) -> str:
    """Strip markup and symbols a speech engine would read aloud ("asterisk asterisk")."""
    text = _MARKDOWN.sub(lambda m: m.group(1) or "", text)
    text = _LIST_MARK.sub("", text)
    return re.sub(r"\s+", " ", text).strip()


def split_sentences(text: str) -> list[str]:
    return [p.strip() for p in _SENTENCE.split(clean_for_speech(text)) if p.strip()]


def spoken_version(text: str, max_chars: int) -> tuple[str, bool]:
    """(what to say, was it shortened). Short answers are spoken whole. Long ones are cut at a sentence boundary
    and end with a pointer to the full text; the complete answer always stays on the dashboard."""
    cleaned = clean_for_speech(text)
    if len(cleaned) <= max_chars:
        return cleaned, False
    kept: list[str] = []
    total = 0
    for sentence in split_sentences(cleaned):
        if kept and total + len(sentence) > max_chars:
            break
        kept.append(sentence)
        total += len(sentence) + 1
        if total >= max_chars:
            break
    head = " ".join(kept)
    if len(head) > max_chars:  # one enormous sentence
        head = head[:max_chars].rsplit(" ", 1)[0].rstrip(",;:") + "."
    return f"{head} The full details are on your dashboard.", True


def priority_rank(priority: str) -> int:
    return PRIORITIES.index(normalize_priority(priority))


def local_now_factory(zone):
    return lambda: datetime.now(zone)
