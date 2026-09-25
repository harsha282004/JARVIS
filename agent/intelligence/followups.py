"""Spoken follow-ups: what "it", "the first one", "the longest" or a bare "tomorrow" refers to.

The hub router remembers the last list it spoke (a day's schedule, a set of emails) for a few minutes; this mixin resolves short
follow-up sentences against that saved list only. It never guesses: with nothing recent, or when "it" is more than one thing, it says so
or returns None so the normal path answers. Nothing here is persisted or sent to the LLM.
"""

import re
from datetime import datetime, timedelta
from typing import Any

from agent.intelligence.models import fact
from agent.intelligence.phrasing import clock, day_word, quoted

_DAYS = "today|tomorrow|monday|tuesday|wednesday|thursday|friday|saturday|sunday"
_FU_FIRST = re.compile(r"^(?:what|which)(?: (?:meeting|event|one|email|mail))?(?: is| was)?(?: the)? (?P<w>first|earliest|last|latest|next)(?: (?:one|meeting|event|email))?$|^(?:the )?(?P<w2>first|earliest|last|latest) one$")
_FU_LONGEST = re.compile(r"^(?:what|which)(?: (?:meeting|event|one))?(?: is| was)? the (?P<w>longest|shortest)(?: (?:one|meeting|event))?$")
_FU_COUNT = re.compile(r"^how many(?: (?:meetings?|events?|emails?|of them))?(?: (?:is|are) (?:there|that))?$")
_FU_WHEN = re.compile(r"^(?:when|what time)(?: is| does| was)? (?:it|that)(?: (?:start|begin))?$|^when is that$")
_FU_LEN = re.compile(r"^(?:how long)(?: is| does| was)?(?: it| that)?(?: (?:last|take|go))?$|^what(?:'s| is) the duration$")
_FU_WHO = re.compile(r"^(?:who (?:sent|wrote) (?:it|that|the (?P<w>first|last|latest) one)|who is (?:it|that) from)$")
_FU_DAY = re.compile(rf"^(?:and |what about |how about |and what about )?(?P<d>{_DAYS}|this week|next week)$")
FOLLOW_UP_TTL = timedelta(minutes=4)


class Last:
    """The last list a spoken answer gave. Expires; held in memory only."""

    def __init__(self, kind: str, items: list[dict], label: str, day: str | None, at: datetime):
        self.kind, self.items, self.label, self.day, self.at = kind, items, label, day, at


class FollowUps:
    """Mixin for HubRouter: needs `_svc`, `_last`, `_record`, `_prov`, `_schedule`."""

    def _fresh(self, kind: str) -> Last | None:
        last = self._last
        if last is None or last.kind != kind or self._svc.now() - last.at > FOLLOW_UP_TTL:
            return None
        return last

    def _event_span(self, e: dict[str, Any]) -> tuple[datetime, datetime]:
        start = datetime.fromisoformat(e["source_timestamp"]).astimezone(self._svc.zone)
        end_raw = e["metadata"].get("end")
        end = datetime.fromisoformat(end_raw).astimezone(self._svc.zone) if end_raw else start
        return start, end

    def _follow_up(self, t: str) -> str | None:
        m = _FU_DAY.match(t)
        if m and self._last is not None and self._last.kind == "schedule" and self._svc.now() - self._last.at <= FOLLOW_UP_TTL:
            return self._schedule(m.group("d"), None)
        cur = self._fresh("schedule") or self._fresh("emails")
        if cur is None or not cur.items:
            return None
        noun = "meeting" if cur.kind == "schedule" else "email"
        m = _FU_FIRST.match(t)
        if m:
            which = (m.group("w") or m.group("w2")).lower()
            if cur.kind == "schedule":
                ordered = sorted(cur.items, key=lambda e: self._event_span(e)[0])
                pick = ordered[-1] if which in ("last", "latest") else ordered[0]
            else:
                pick = cur.items[-1] if which in ("last", "latest") else cur.items[0]
            return self._describe_pick(cur, pick, which)
        m = _FU_LONGEST.match(t)
        if m and cur.kind == "schedule":
            spans = [(e, self._event_span(e)) for e in cur.items if not e["metadata"].get("all_day")]
            if not spans:
                return "Those are all-day events, so I can't tell which is longest."
            pick, (s, e_) = (max if m.group("w") == "longest" else min)(spans, key=lambda p: p[1][1] - p[1][0])
            minutes = int((e_ - s).total_seconds() // 60)
            if minutes >= 60:
                hours = minutes // 60
                length = f"{hours} hour{'s' if hours != 1 else ''}" + (f" {minutes % 60} minutes" if minutes % 60 else "")
            else:
                length = f"{minutes} minutes"
            stmt = fact(f"Your calendar shows {quoted(pick['title'], 60)}.", self._prov(pick, f"calendar event {quoted(pick['title'], 40)}"))
            return self._record(f"The {m.group('w')} is {quoted(pick['title'], 60)}, {length}.", [stmt], f"{m.group('w')} meeting")
        if _FU_COUNT.match(t):
            n = len(cur.items)
            return f"That was {n} {noun}{'s' if n != 1 else ''}."
        if _FU_WHEN.match(t) or _FU_LEN.match(t):
            if cur.kind != "schedule" or len(cur.items) != 1:
                return None  # "it" is not one thing: the normal path asks which
            e = cur.items[0]
            s, e_ = self._event_span(e)
            if _FU_LEN.match(t):
                return f"{quoted(e['title'], 60)} lasts {int((e_ - s).total_seconds() // 60)} minutes." if e_ > s else f"I don't have an end time for {quoted(e['title'], 60)}."
            return f"{quoted(e['title'], 60)} is {day_word(s, self._svc.now(), self._svc.zone)} at {clock(s, self._svc.zone)}."
        m = _FU_WHO.match(t)
        if m and cur.kind == "emails":
            which = (m.group("w") or "").lower()
            if len(cur.items) != 1 and not which:
                return "Which email do you mean? Say the first one, or the last one."
            pick = cur.items[-1] if which in ("last", "latest") else cur.items[0]
            return f"It's from {pick['metadata'].get('sender', 'a sender I can not identify')}."
        return None

    def _describe_pick(self, cur: Last, pick: dict[str, Any], which: str) -> str:
        word = "first" if which in ("first", "earliest", "next") else "last"
        if cur.kind == "schedule":
            s, _ = self._event_span(pick)
            at = "all day" if pick["metadata"].get("all_day") else f"at {clock(s, self._svc.zone)}"
            stmt = fact(f"Your calendar shows {quoted(pick['title'], 60)}.", self._prov(pick, f"calendar event {quoted(pick['title'], 40)}"))
            return self._record(f"Your {word} one {cur.label} is {quoted(pick['title'], 60)} {at}.", [stmt], f"{word} meeting")
        md = pick["metadata"]
        stmt = fact(f"Gmail has an email {quoted(pick['title'], 60)}.", self._prov(pick, f"email {quoted(pick['title'], 40)}"))
        return self._record(f"The {word} one is {quoted(pick['title'], 60)}, from {md.get('sender', 'a sender')}.", [stmt], "email follow-up")
