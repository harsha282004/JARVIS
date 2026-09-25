"""Personal activity timeline: a searchable record of what actually happened, reconstructed from real, timestamped source items.

Every entry has a real time (an email's received time, a task's created/completed time, a calendar event's start), a source and a
stable key, so ingesting the same snapshot twice adds nothing ("what happened yesterday?" never repeats or invents). Questions such as
"what happened with my project yesterday?" are answered only from these stored entries; if there are none the answer says so.

Only short labels are stored (a subject or task title), never message bodies.
"""

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

from agent.intelligence.models import Snapshot
from agent.intelligence.phrasing import clock, day_word
from agent.intelligence.textnorm import contains_phrase, tokens
from backend.core.state_store import JsonLines


@dataclass(frozen=True)
class TimelineEntry:
    key: str
    at: datetime
    kind: str  # email_received | task_created | task_completed | calendar_event | reminder_triggered | deadline_detected | action
    summary: str
    source_type: str
    source_id: str

    def to_dict(self) -> dict:
        return {"key": self.key, "at": self.at.isoformat(), "kind": self.kind, "summary": self.summary, "source_type": self.source_type, "source_id": self.source_id}

    @classmethod
    def from_dict(cls, d: dict) -> "TimelineEntry | None":
        try:
            return cls(d["key"], datetime.fromisoformat(d["at"]), d["kind"], d["summary"], d["source_type"], d["source_id"])
        except (KeyError, ValueError, TypeError):
            return None


class ActivityTimeline:
    def __init__(self, path: Path | None = None, retention_days: int = 90):
        self._store = JsonLines(path, max_bytes=3_000_000) if path else None
        self._mem: list[TimelineEntry] = []
        self._keys: set[str] = set()
        self._retention = timedelta(days=retention_days)
        for entry in self._load():
            self._keys.add(entry.key)
            self._mem.append(entry)

    def _load(self) -> list[TimelineEntry]:
        if self._store is None:
            return []
        return [e for d in self._store.read() if (e := TimelineEntry.from_dict(d)) is not None]

    # ---- recording -----------------------------------------------------------------------------------------------------

    def record(self, entry: TimelineEntry) -> bool:
        """Add an entry unless one with the same key exists. Returns True if it was added (idempotent)."""
        if entry.key in self._keys:
            return False
        self._keys.add(entry.key)
        self._mem.append(entry)
        if self._store is not None:
            self._store.append(entry.to_dict())
        return True

    def note_action(self, at: datetime, summary: str, source_id: str) -> bool:
        return self.record(TimelineEntry(f"action:{source_id}:{at.isoformat()}", at, "action", summary, "jarvis", source_id))

    def ingest(self, snap: Snapshot) -> int:
        """Record what the snapshot's own timestamps say happened. Only items inside the retention window."""
        cutoff = snap.now - self._retention
        added = 0
        for e in snap.emails:
            if e.received_at is not None and e.received_at >= cutoff:
                added += self.record(TimelineEntry(f"email:{e.message_id}", e.received_at, "email_received", f"Email received: {e.subject[:80] or '(no subject)'}", "email", e.message_id))
        for t in snap.tasks:
            if t.created_at is not None and t.created_at >= cutoff:
                added += self.record(TimelineEntry(f"task-created:{t.task_id}", t.created_at, "task_created", f"Task created: {t.title[:80]}", "task", t.task_id))
            if t.completed_at is not None and t.completed_at >= cutoff:
                added += self.record(TimelineEntry(f"task-done:{t.task_id}", t.completed_at, "task_completed", f"Task completed: {t.title[:80]}", "task", t.task_id))
        for c in snap.calendar:
            if c.start >= cutoff and c.start <= snap.now:
                added += self.record(TimelineEntry(f"cal:{c.calendar_id}/{c.event_id}", c.start, "calendar_event", f"Calendar event: {c.title[:80]}", "calendar", c.event_id))
        return added

    # ---- questions -----------------------------------------------------------------------------------------------------

    def for_day(self, day: date, zone, topic: str | None = None, related: frozenset[tuple[str, str]] = frozenset()) -> list[TimelineEntry]:
        """Entries on `day` (the user's local calendar day), oldest first. With `topic`, only entries that mention it or whose source
        (source_type, source_id) is in `related` (records the context graph connects to the topic)."""
        found = [e for e in self._mem if e.at.astimezone(zone).date() == day]
        if topic:
            t = tokens(topic)
            found = [e for e in found if contains_phrase(e.summary, topic) or (t and t <= tokens(e.summary)) or (e.source_type, e.source_id) in related]
        return sorted(found, key=lambda e: e.at)

    def describe_day(self, day: date, zone, now: datetime, topic: str | None = None, related: frozenset[tuple[str, str]] = frozenset()) -> str:
        entries = self.for_day(day, zone, topic, related)
        label = day_word(day, now, zone)
        about = f" about {topic}" if topic else ""
        if not entries:
            return f"I don't have any recorded activity{about} for {label}. I can only tell you what my sources actually recorded."
        lines = [f"{clock(e.at, zone)}: {e.summary}." for e in entries[:12]]
        more = f" And {len(entries) - 12} more." if len(entries) > 12 else ""
        return f"Here's what I have recorded{about} for {label}. " + " ".join(lines) + more

    def count(self) -> int:
        return len(self._mem)
