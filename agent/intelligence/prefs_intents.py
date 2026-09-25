"""Understanding preference statements ("Don't notify me about newsletters") and storing them explicitly.

Deterministic patterns, no model: a statement is only recorded when it matches a known pattern exactly enough to know what to store, and
the reply always says what was stored so the user can correct it. Everything stored can be listed ("show my preferences") and changed
or removed ("start notifying me about newsletters again", "forget my newsletter preference").
"""

import re
from dataclasses import dataclass

from backend.core.preferences import PreferenceStore

_WORD_NUMBERS = {"a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "ten": 10}
_TIME = re.compile(r"^(?:at\s+)?(\d{1,2})(?::(\d{2}))?\s*([ap])\.?m?\.?$|^(\d{1,2}):(\d{2})$", re.I)


def parse_clock(text: str) -> str | None:
    """'10 PM' / '10:30pm' / '22:00' -> 'HH:MM'; None if it is not a time."""
    t = text.strip().lower().replace(" ", "")
    m = _TIME.match(t)
    if not m:
        return None
    if m.group(4) is not None:
        hours, minutes = int(m.group(4)), int(m.group(5))
    else:
        hours, minutes = int(m.group(1)), int(m.group(2) or 0)
        if not 1 <= hours <= 12:
            return None
        hours = hours % 12 + (12 if m.group(3) == "p" else 0)
    return f"{hours:02d}:{minutes:02d}" if hours < 24 and minutes < 60 else None


def _phrase(text: str) -> str:
    words = re.sub(r"[^a-z0-9 \-]", "", text.lower()).split()
    words = [w for w in words if w not in ("the", "any", "all", "my", "notifications", "notification", "alerts", "alert")]
    return " ".join(w[:-1] if len(w) > 4 and w.endswith("s") and not w.endswith("ss") else w for w in words)


@dataclass(frozen=True)
class PreferenceReply:
    text: str
    changed: bool


_RULES: list[tuple[str, re.Pattern[str]]] = [
    ("unmute", re.compile(r"^(?:start|resume|begin) (?:notifying|alerting|telling) me (?:about|for|regarding) (?P<what>.+?)(?: again)?$|^unmute (?P<what2>.+)$", re.I)),
    ("mute", re.compile(r"^(?:please )?(?:don'?t|do not|stop|never) (?:notify|notifying|alert|alerting|tell|telling|bother) me (?:about|for|regarding|with) (?P<what>.+)$|^mute (?P<what2>.+?)(?: notifications?)?$", re.I)),
    ("important", re.compile(r"^(?:please )?keep (?P<what>.+?) (?:notifications?|alerts?) (?:as )?important$|^(?:treat|mark|consider) (?P<what2>.+?) (?:notifications?|alerts?)? ?as important$", re.I)),
    ("lead", re.compile(r"^(?:please )?remind me (?P<n>\d+|a|an|one|two|three|four|five|six|seven|ten) (?P<unit>days?|hours?|weeks?) before (?P<what>.+)$", re.I)),
    ("voice_cutoff", re.compile(r"^(?:please )?(?:don'?t|do not|never) (?:speak|say|read out|announce|voice) (?:the )?(?:notifications?|alerts?|reminders?)? ?(?:out loud )?(?:after|past|from) (?P<t>.+)$|^no (?:spoken|voice) (?:notifications?|alerts?) (?:after|past|from) (?P<t2>.+)$", re.I)),
    ("quiet", re.compile(r"^(?:please )?(?:set )?(?:my )?quiet hours (?:to |from |between |are )?(?:from )?(?P<a>.+?) (?:to|until|till|and) (?P<b>.+)$", re.I)),
    ("workday", re.compile(r"^(?:set )?my (?:working|work) hours (?:are|to|from) (?P<a>.+?) (?:to|until|till|and) (?P<b>.+)$", re.I)),
    ("show", re.compile(r"^(?:show|list|read|tell) (?:me )?(?:all )?(?:my )?(?:notification )?(?:preferences|settings)$|^what (?:preferences|settings) (?:have you|do you|did you) (?:saved|stored|have|remember)", re.I)),
    ("forget", re.compile(r"^(?:forget|remove|delete|clear) (?:my )?(?P<what>.+?) preferences?$", re.I)),
    ("auto_tasks_on", re.compile(r"^(?:please )?(?:create|make|add) tasks? (?:from|for) (?:my )?emails? automatically$|^(?:automatically )?create tasks? from (?:my )?emails?$", re.I)),
    ("auto_tasks_off", re.compile(r"^(?:please )?(?:ask|check) (?:me )?before (?:creating|making|adding) tasks? (?:from|for) (?:my )?emails?$|^(?:don'?t|do not|stop) (?:automatically )?creat(?:e|ing) tasks? from (?:my )?emails?(?: automatically)?$", re.I)),
]


def looks_like_preference(text: str) -> bool:
    cleaned = " ".join(text.strip().rstrip(".!?").replace("’", "'").split())
    return any(p.match(cleaned) for _, p in _RULES)


def handle_preference(text: str, store: PreferenceStore) -> PreferenceReply | None:
    """A reply if `text` is a preference statement or question, else None (not ours)."""
    cleaned = " ".join(text.strip().rstrip(".!?").replace("’", "'").split())
    for name, pattern in _RULES:
        m = pattern.match(cleaned)
        if not m:
            continue
        g = {k: v for k, v in m.groupdict().items() if v}
        what = _phrase(g.get("what") or g.get("what2") or "")
        try:
            if name == "show":
                return PreferenceReply("Here are your preferences. " + ". ".join(store.describe()) + ".", False)
            if name == "mute" and what:
                store.mute(what)
                return PreferenceReply(f"Okay. I won't notify you about {what}. Critical alerts will still come through.", True)
            if name == "unmute" and what:
                store.unmute(what)
                return PreferenceReply(f"Okay. I'll notify you about {what} again.", True)
            if name == "important" and what:
                store.add_important(what)
                return PreferenceReply(f"Okay. I'll treat {what} notifications as important.", True)
            if name == "lead" and what:
                n = _WORD_NUMBERS.get(g["n"].lower()) or int(g["n"])
                unit = g["unit"].lower().rstrip("s")
                minutes = n * {"day": 1440, "hour": 60, "week": 10080}[unit]
                keyword = what if what.endswith(("deadline", "deadlines")) or "deadline" not in what else what
                store.set_reminder_lead(keyword, minutes)
                return PreferenceReply(f"Okay. I'll remind you {n} {unit}{'s' if n != 1 else ''} before {what}.", True)
            if name == "voice_cutoff":
                hhmm = parse_clock(g.get("t") or g.get("t2") or "")
                if hhmm is None:
                    return PreferenceReply("I didn't catch the time. Say it like 'after 10 PM'.", False)
                store.set_voice_cutoff(hhmm)
                return PreferenceReply(f"Okay. I won't speak notifications after {hhmm}. I'll show them instead. Critical alerts can still speak.", True)
            if name in ("quiet", "workday"):
                a, b = parse_clock(g["a"]), parse_clock(g["b"])
                if a is None or b is None:
                    return PreferenceReply("I didn't catch the times. Say them like '10 PM to 7 AM'.", False)
                if name == "quiet":
                    store.set_quiet_hours(a, b)
                    return PreferenceReply(f"Okay. Quiet hours are {a} to {b}. Only critical alerts will break through.", True)
                store.set_workday(a, b)
                return PreferenceReply(f"Okay. I'll plan your days between {a} and {b}.", True)
            if name == "forget" and what:
                store.unmute(what)
                store.remove_important(what)
                store.remove_reminder_lead(what)
                return PreferenceReply(f"Okay. I removed my saved preferences about {what}.", True)
            if name == "auto_tasks_on":
                store.set_auto_create_tasks(True)
                return PreferenceReply("Okay. I'll create tasks from emails automatically when the request is clear. I'll still ask when an email looks suspicious.", True)
            if name == "auto_tasks_off":
                store.set_auto_create_tasks(False)
                return PreferenceReply("Okay. I'll ask before creating a task from an email.", True)
        except ValueError as exc:
            return PreferenceReply(f"I couldn't save that: {exc}.", False)
    return None
