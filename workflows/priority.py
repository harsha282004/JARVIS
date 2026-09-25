"""Daily priority synthesis: what needs attention today, and WHY.

Inputs are explicit fields only: a due date or start time, a task's own priority, an email's importance classification (with the reasons the classifier recorded), an
explicit notification level. Nothing is inferred about the user's private life, and urgency is never invented: an item with no time and no priority is listed as
"no deadline", not promoted. Every surfaced item carries its reasons so "why is this on the list?" has a real answer.
"""

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any


@dataclass
class Item:
    kind: str                     # meeting | deadline | task | email | notification
    title: str
    score: float
    reasons: list[str] = field(default_factory=list)
    when: str = ""                # human wording of the time, if any
    source: str = ""
    source_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "title": self.title, "score": round(self.score, 1), "reasons": self.reasons, "when": self.when, "source": self.source, "source_id": self.source_id}


def _days_until(due: datetime | None, today: date, zone) -> int | None:
    return None if due is None else (due.astimezone(zone).date() - today).days


def _proximity(days: int | None) -> tuple[float, str]:
    if days is None:
        return 0.0, ""
    if days < 0:
        return 100.0, f"overdue by {-days} day{'s' if days != -1 else ''}"
    if days == 0:
        return 80.0, "due today"
    if days == 1:
        return 60.0, "due tomorrow"
    if days <= 3:
        return 40.0, f"due in {days} days"
    if days <= 7:
        return 25.0, f"due in {days} days"
    return 5.0, f"due in {days} days"


def _clock(d: datetime, zone) -> str:
    local = d.astimezone(zone)
    return local.strftime("%I:%M %p").lstrip("0")


def synthesize(*, today: date, zone, meetings: list[dict[str, Any]], tasks: list[dict[str, Any]], deadlines: list[dict[str, Any]], emails: list[dict[str, Any]],
               notifications: list[dict[str, Any]] | None = None) -> list[Item]:
    items: list[Item] = []
    for m in meetings:
        start = m.get("start")
        if start is None or _days_until(start, today, zone) != 0:
            continue
        items.append(Item("meeting", m["title"], 70.0, ["a meeting on your calendar today"], f"at {_clock(start, zone)}", "calendar", str(m.get("id", ""))))
    for t in tasks:
        score, why = _proximity(_days_until(t.get("due"), today, zone))
        reasons = [why] if why else ["open task with no due date"]
        prio = int(t.get("priority", 2))
        if prio >= 3:
            score += 10 * (prio - 2)
            reasons.append("you marked it high priority" if prio == 3 else "you marked it critical")
        items.append(Item("task", t["title"], score + 1.0, reasons, "", "tasks", str(t.get("id", ""))))
    for d in deadlines:
        score, why = _proximity(_days_until(d.get("due"), today, zone))
        if not why:
            continue
        reasons = [why, f"stated in {d.get('source', 'a source')}" + (" (verified)" if d.get("status") == "VERIFIED" else "")]
        items.append(Item("deadline", d["title"], score + 5.0, reasons, "", str(d.get("source", "")), str(d.get("source_id", ""))))
    for e in emails:
        imp = str(e.get("importance", "")).upper()
        base = {"CRITICAL": 65.0, "IMPORTANT": 45.0}.get(imp)
        if base is None:
            continue
        reasons = [f"classified {imp.lower()}"] + list(e.get("reasons", []))[:2]
        if e.get("unread"):
            reasons.append("unread")
        items.append(Item("email", e["title"], base, reasons, "", "gmail", str(e.get("id", ""))))
    for n in notifications or []:
        if int(n.get("level", 0)) >= 3 and not n.get("acknowledged"):
            items.append(Item("notification", n["title"], 50.0 + 5 * (int(n["level"]) - 3), ["a high-priority notification you haven't acknowledged"], "", "notifications", str(n.get("id", ""))))
    return sorted(items, key=lambda i: -i.score)


def spoken_summary(items: list[Item], *, greeting: str = "Good morning.", missing: list[str] | None = None) -> str:
    """One or two sentences with counts, then the top items with their reasons. Only what was actually read; missing sources are named, not filled in."""
    counts = {k: sum(1 for i in items if i.kind == k) for k in ("meeting", "email", "deadline", "task", "notification")}
    parts = []
    if counts["meeting"]:
        parts.append(f"{counts['meeting']} meeting{'s' if counts['meeting'] != 1 else ''} today")
    if counts["email"]:
        parts.append(f"{counts['email']} high-priority email{'s' if counts['email'] != 1 else ''}")
    if counts["deadline"]:
        parts.append(f"{counts['deadline']} upcoming deadline{'s' if counts['deadline'] != 1 else ''}")
    if counts["task"]:
        parts.append(f"{counts['task']} open task{'s' if counts['task'] != 1 else ''}")
    if counts["notification"]:
        parts.append(f"{counts['notification']} notification{'s' if counts['notification'] != 1 else ''} waiting")
    head = greeting + (" You have " + (", ".join(parts[:-1]) + (", and " if len(parts) > 1 else "") + parts[-1]) + "." if parts else " I don't see anything that needs your attention right now.")
    top = [i for i in items if i.kind != "task" or i.score >= 30][:3]
    if top:
        head += " Most pressing: " + "; ".join(f"{i.title}" + (f" ({i.when})" if i.when else "") + (f", {i.reasons[0]}" if i.reasons else "") for i in top) + "."
    if missing:
        head += " I couldn't check " + " or ".join(missing) + ", so that isn't included."
    return head
