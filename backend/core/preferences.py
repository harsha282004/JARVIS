"""Explicit user preferences: what the user told JARVIS about notifications, timing and working hours.

Stored as plain JSON (`.jarvis/preferences.json`) so the user can view and change every one of them: nothing is inferred and
nothing is hidden. Examples of what it holds: "don't notify me about newsletters" (muted category), "keep hackathon
notifications important" (important keyword), "remind me one day before project deadlines" (reminder lead), "don't speak
notifications after 10 PM" (voice cutoff), quiet hours and the working day used for planning.
"""

import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

from backend.core.state_store import JsonFile


def parse_hhmm(value: str) -> time:
    hours, _, minutes = value.strip().partition(":")
    if not (hours.isdigit() and minutes.isdigit() and len(minutes) == 2 and int(hours) < 24 and int(minutes) < 60):
        raise ValueError("expected HH:MM")
    return time(int(hours), int(minutes))


def in_window(local_time: time, start: time, end: time) -> bool:
    """True if `local_time` is inside [start, end), where the window may cross midnight. start == end means no window."""
    if start == end:
        return False
    if start < end:
        return start <= local_time < end
    return local_time >= start or local_time < end


@dataclass
class Preferences:
    muted_categories: list[str] = field(default_factory=list)
    important_keywords: list[str] = field(default_factory=list)
    reminder_leads: dict[str, int] = field(default_factory=dict)  # keyword -> minutes before the deadline
    quiet_enabled: bool = True
    quiet_start: str = "22:00"
    quiet_end: str = "07:00"
    voice_cutoff: str | None = None  # "no spoken notifications after HH:MM (until quiet_end)"
    workday_start: str = "09:00"
    workday_end: str = "18:00"
    auto_create_tasks: bool = False  # false: JARVIS asks before creating a task found in an email

    @classmethod
    def from_dict(cls, data: dict) -> "Preferences":
        known = {k: v for k, v in (data or {}).items() if k in cls.__dataclass_fields__}
        prefs = cls(**known)
        for name in ("quiet_start", "quiet_end", "workday_start", "workday_end"):
            try:
                parse_hhmm(getattr(prefs, name))
            except ValueError:
                setattr(prefs, name, cls.__dataclass_fields__[name].default)  # a corrupted value falls back to the default
        if prefs.voice_cutoff is not None:
            try:
                parse_hhmm(prefs.voice_cutoff)
            except ValueError:
                prefs.voice_cutoff = None
        return prefs


def _norm(word: str) -> str:
    return re.sub(r"[^a-z0-9 ]", "", word.lower()).strip()


class PreferenceStore:
    def __init__(self, path: Path | None = None, defaults: Preferences | None = None):
        self._defaults = defaults or Preferences()
        self._file = JsonFile(path, {}) if path else None
        self._memory = Preferences.from_dict(asdict(self._defaults))

    def get(self) -> Preferences:
        if self._file is None:
            return self._memory
        stored = self._file.read()
        merged = {**asdict(self._defaults), **(stored if isinstance(stored, dict) else {})}
        return Preferences.from_dict(merged)

    def _save(self, prefs: Preferences) -> Preferences:
        if self._file is None:
            self._memory = prefs
        else:
            self._file.write(asdict(prefs))
        return prefs

    # ---- changes (each returns the new Preferences) ------------------------------------------------------------------

    def mute(self, category: str) -> Preferences:
        p, c = self.get(), _norm(category)
        if c and c not in p.muted_categories:
            p.muted_categories.append(c)
        return self._save(p)

    def unmute(self, category: str) -> Preferences:
        p, c = self.get(), _norm(category)
        p.muted_categories = [m for m in p.muted_categories if m != c]
        return self._save(p)

    def add_important(self, keyword: str) -> Preferences:
        p, k = self.get(), _norm(keyword)
        if k and k not in p.important_keywords:
            p.important_keywords.append(k)
        return self._save(p)

    def remove_important(self, keyword: str) -> Preferences:
        p, k = self.get(), _norm(keyword)
        p.important_keywords = [m for m in p.important_keywords if m != k]
        return self._save(p)

    def set_reminder_lead(self, keyword: str, minutes: int) -> Preferences:
        if minutes < 0 or minutes > 60 * 24 * 60:
            raise ValueError("reminder lead must be between 0 minutes and 60 days")
        p = self.get()
        p.reminder_leads[_norm(keyword)] = int(minutes)
        return self._save(p)

    def remove_reminder_lead(self, keyword: str) -> Preferences:
        p = self.get()
        p.reminder_leads.pop(_norm(keyword), None)
        return self._save(p)

    def set_voice_cutoff(self, hhmm: str | None) -> Preferences:
        p = self.get()
        if hhmm is not None:
            parse_hhmm(hhmm)
        p.voice_cutoff = hhmm
        return self._save(p)

    def set_quiet_hours(self, start: str, end: str, enabled: bool = True) -> Preferences:
        parse_hhmm(start), parse_hhmm(end)
        p = self.get()
        p.quiet_start, p.quiet_end, p.quiet_enabled = start, end, enabled
        return self._save(p)

    def set_workday(self, start: str, end: str) -> Preferences:
        s, e = parse_hhmm(start), parse_hhmm(end)
        if e <= s:
            raise ValueError("the working day must end after it starts")
        p = self.get()
        p.workday_start, p.workday_end = start, end
        return self._save(p)

    def set_auto_create_tasks(self, value: bool) -> Preferences:
        p = self.get()
        p.auto_create_tasks = bool(value)
        return self._save(p)

    # ---- questions ---------------------------------------------------------------------------------------------------

    def is_muted(self, *labels: str) -> bool:
        muted = self.get().muted_categories
        text = " ".join(_norm(label) for label in labels)
        return any(m and m in text for m in muted)

    def is_important(self, *labels: str) -> bool:
        keys = self.get().important_keywords
        text = " ".join(_norm(label) for label in labels)
        return any(k and k in text for k in keys)

    def reminder_lead_for(self, *labels: str) -> int | None:
        text = " ".join(_norm(label) for label in labels)
        leads = self.get().reminder_leads
        matches = [minutes for key, minutes in leads.items() if key and all(w in text for w in key.split())]
        return max(matches) if matches else None

    def in_quiet_hours(self, now: datetime, zone: ZoneInfo) -> bool:
        p = self.get()
        if not p.quiet_enabled:
            return False
        return in_window(now.astimezone(zone).time().replace(second=0, microsecond=0), parse_hhmm(p.quiet_start), parse_hhmm(p.quiet_end))

    def voice_allowed(self, now: datetime, zone: ZoneInfo) -> bool:
        """False after the user's voice cutoff (until quiet hours end) and during quiet hours."""
        p = self.get()
        local = now.astimezone(zone).time().replace(second=0, microsecond=0)
        if p.voice_cutoff and in_window(local, parse_hhmm(p.voice_cutoff), parse_hhmm(p.quiet_end)):
            return False
        return not self.in_quiet_hours(now, zone)

    def describe(self) -> list[str]:
        """Every stored preference as a sentence, for the user to review."""
        p = self.get()
        lines = []
        lines.append(f"Quiet hours: {p.quiet_start} to {p.quiet_end}" if p.quiet_enabled else "Quiet hours are off")
        lines.append(f"Working day: {p.workday_start} to {p.workday_end}")
        if p.voice_cutoff:
            lines.append(f"No spoken notifications from {p.voice_cutoff} until {p.quiet_end}")
        if p.muted_categories:
            lines.append("Not notifying about: " + ", ".join(p.muted_categories))
        if p.important_keywords:
            lines.append("Always treated as important: " + ", ".join(p.important_keywords))
        for key, minutes in sorted(p.reminder_leads.items()):
            lines.append(f"Reminding you {_minutes_text(minutes)} before {key}")
        lines.append("Tasks found in email are created automatically" if p.auto_create_tasks else "I ask before creating a task from an email")
        return lines


def _minutes_text(minutes: int) -> str:
    if minutes and minutes % 1440 == 0:
        days = minutes // 1440
        return f"{days} day{'s' if days != 1 else ''}"
    if minutes and minutes % 60 == 0:
        hours = minutes // 60
        return f"{hours} hour{'s' if hours != 1 else ''}"
    return f"{minutes} minutes"
