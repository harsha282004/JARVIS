"""Workflow templates: fixed, reviewed step graphs over the operator tools. The deterministic planner picks and fills one; nothing here calls a model.

Each template is a function (params) -> steps. A step names a tool from the operator registry (or a browser tool), takes typed arguments, and receives earlier results
only through `From(step, path)`. Which optional systems take part is decided by availability at plan time (`params["available"]`), so a missing calendar means no
calendar step, not a failed workflow.
"""

from dataclasses import dataclass
from typing import Any, Callable

from workflows import facts as factlib
from workflows.models import From, Fact, SStatus, WStep, Workflow


class _Seq:
    def __init__(self):
        self.steps: list[WStep] = []

    def add(self, description: str, tool: str, arguments: dict[str, Any] | None = None, deps: list[str] | None = None, *, source: str, expected: str = "", verify: list[str] | None = None,
            optional: bool = False) -> str:
        sid = f"s{len(self.steps) + 1}"
        self.steps.append(WStep(sid, description, tool, arguments or {}, deps or [], expected, verify or [], optional, source, side_effect=False))
        return sid


def _has(params: dict[str, Any], system: str) -> bool:
    return system in params.get("available", set())


def _find_deadline(q: _Seq, params: dict[str, Any], *, cross_check: bool = True) -> tuple[From, list[str]]:
    """search email -> extract dated statements -> pick the one verified fact -> (optionally) compare with the calendar.
    Returns (a reference to the final fact, prerequisite step ids). After a comparison the final fact is the comparison's (it carries the user's choice when the sources
    disagreed); if the comparison was unavailable it falls back to the verified fact."""
    topic = params["topic"]
    s1 = q.add(f"Search email for '{topic}'", "gmail_search", {"query": topic, "limit": 10}, source="gmail", expected="matching emails", verify=["has:emails"])
    s2 = q.add("Find dates stated in those emails", "gmail_extract_deadlines", {"emails": From(s1, "emails"), "topic": topic}, [s1], source="gmail", expected="dates with their source sentences", verify=["has:facts"])
    s3 = q.add("Pick the one dependable deadline", "verify_deadline_fact", {"facts": From(s2, "facts")}, [s2], source="local", expected="one VERIFIED or HIGH_CONFIDENCE deadline", verify=["has:fact"])
    prereq = [s3]
    ref = From(s3, "fact")
    if cross_check and _has(params, "calendar"):
        s4 = q.add("Check the date against your calendar", "calendar_compare_fact", {"fact": From(s3, "fact")}, [s3], source="calendar", expected="no conflicting calendar entry", optional=True)
        prereq.append(s4)
        ref = From(s4, "fact", fallback=From(s3, "fact"))
    return ref, prereq


def deadline_to_task(params: dict[str, Any]) -> list[WStep]:
    q = _Seq()
    fact, pre = _find_deadline(q, params)
    q.add("Create the task", "task_create", {"fact": fact}, pre, source="tasks", expected="a task with the deadline as its due date", verify=["readback"])
    return q.steps


def deadline_to_reminder(params: dict[str, Any]) -> list[WStep]:
    q = _Seq()
    fact, pre = _find_deadline(q, params)
    t = q.add(f"Work out the reminder time ({params.get('days_before', 0)} days before)", "compute_reminder_time",
              {"fact": fact, "days_before": int(params.get("days_before", 0)), "hour": int(params.get("hour", 9))}, pre, source="local", expected="a future time", verify=["has:remind_at"])
    q.add("Schedule the reminder", "reminder_create", {"at": From(t, "remind_at"), "fact": fact}, [t, *pre], source="reminders", expected="a scheduled reminder", verify=["readback"])
    return q.steps


def deadline_task_and_reminder(params: dict[str, Any]) -> list[WStep]:
    q = _Seq()
    fact, pre = _find_deadline(q, params)
    q.add("Create the task", "task_create", {"fact": fact}, pre, source="tasks", expected="a task with the deadline as its due date", verify=["readback"])
    t = q.add(f"Work out the reminder time ({params.get('days_before', 0)} days before)", "compute_reminder_time",
              {"fact": fact, "days_before": int(params.get("days_before", 0)), "hour": int(params.get("hour", 9))}, pre, source="local", expected="a future time", verify=["has:remind_at"])
    q.add("Schedule the reminder", "reminder_create", {"at": From(t, "remind_at"), "fact": fact}, [t, *pre], source="reminders", expected="a scheduled reminder", verify=["readback"])
    return q.steps


def email_deadlines_to_tasks(params: dict[str, Any]) -> list[WStep]:
    q = _Seq()
    s1 = q.add("Find your important emails", "gmail_search_important", {"limit": 10}, source="gmail", verify=["has:emails"])
    s2 = q.add("Find dates stated in them", "gmail_extract_deadlines", {"emails": From(s1, "emails")}, [s1], source="gmail", verify=["has:facts"])
    q.add("Create tasks for the dependable deadlines", "task_create_batch", {"facts": From(s2, "facts")}, [s2], source="tasks", verify=["readback"])
    return q.steps


def important_email_review(params: dict[str, Any]) -> list[WStep]:
    q = _Seq()
    s1 = q.add("Find your important emails", "gmail_search_important", {"limit": 10}, source="gmail", verify=["has:emails"])
    q.add("Summarize what needs attention", "compose_workflow_report", {"kind": "important_emails"}, [s1], source="local")
    return q.steps


def meeting_preparation(params: dict[str, Any]) -> list[WStep]:
    q = _Seq()
    day = params.get("day", "tomorrow")
    s1 = q.add(f"Read your calendar for {day}", "calendar_events", {"day": day}, source="calendar", verify=["has:events"])
    deps = [s1]
    if _has(params, "gmail"):
        s2 = q.add("Find emails related to those events", "gmail_related_emails", {"events": From(s1, "events")}, [s1], source="gmail", optional=True)
        deps.append(s2)
    rep_id = q.add("Prepare the summary", "compose_workflow_report", {"kind": "meeting_prep"}, deps, source="local")
    if params.get("notify"):
        q.add("Notify you", "notify_user", {"text": From(rep_id, "text"), "priority": "normal", "key": f"meeting_prep:{day}"}, [rep_id], source="notifications")
    return q.steps


def daily_briefing(params: dict[str, Any]) -> list[WStep]:
    q = _Seq()
    ids: list[str] = []
    refs: dict[str, Any] = {}
    if _has(params, "calendar"):
        s = q.add("Read today's calendar", "calendar_events", {"day": "today"}, source="calendar", optional=True)
        ids.append(s)
        refs["events"] = From(s, "events", optional=True)
    if _has(params, "gmail"):
        s = q.add("Find your important emails", "gmail_search_important", {"limit": 10}, source="gmail", optional=True)
        ids.append(s)
        refs["emails"] = From(s, "emails", optional=True)
    if _has(params, "tasks"):
        s = q.add("Read your open tasks", "tasks_overview", {"days": 7}, source="tasks", optional=True)
        ids.append(s)
        refs["tasks"] = From(s, "tasks", optional=True)
        s = q.add("Find upcoming deadlines", "deadlines_overview", {"days": 7}, source="tasks", optional=True)
        ids.append(s)
        refs["deadlines"] = From(s, "deadlines", optional=True)
    b = q.add("Put together your briefing", "briefing_compose", refs, ids, source="local", expected="a prioritized briefing with reasons")
    if params.get("notify"):
        q.add("Notify you", "notify_user", {"text": From(b, "text"), "priority": "high", "key": "daily_briefing"}, [b], source="notifications")
    return q.steps


def weekly_review(params: dict[str, Any]) -> list[WStep]:
    q = _Seq()
    s1 = q.add("Read your open tasks", "tasks_overview", {"days": 7}, source="tasks", optional=True)
    s2 = q.add("Find deadlines this week", "deadlines_overview", {"days": 7}, source="tasks", optional=True)
    deps = [s1, s2]
    if _has(params, "gmail"):
        deps.append(q.add("Find your important emails", "gmail_search_important", {"limit": 10}, source="gmail", optional=True))
    q.add("Prepare the weekly review", "compose_workflow_report", {"kind": "weekly"}, deps, source="local")
    return q.steps


def deadlines_review(params: dict[str, Any]) -> list[WStep]:
    q = _Seq()
    days = int(params.get("days", 7))
    s1 = q.add(f"Find deadlines in the next {days} days", "deadlines_overview", {"days": days}, source="tasks")
    s2 = q.add("Read your open tasks", "tasks_overview", {"days": days}, source="tasks", optional=True)
    q.add("Summarize what to finish", "compose_workflow_report", {"kind": "deadlines"}, [s1, s2], source="local")
    return q.steps


def github_activity_review(params: dict[str, Any]) -> list[WStep]:
    q = _Seq()
    days = int(params.get("days", 7))
    s1 = q.add("Confirm which GitHub account is connected", "github_identity", {}, source="github", verify=["has:login"])
    s2 = q.add(f"Read activity in your repositories (last {days} days)", "github_activity", {"login": From(s1, "login"), "days": days}, [s1], source="github")
    deps = [s2]
    if params.get("add_tasks"):
        deps.append(q.add("Add the important items to your task list", "task_create_batch", {"facts": From(s2, "facts")}, [s2], source="tasks", verify=["readback"]))
    q.add("Summarize the activity", "compose_workflow_report", {"kind": "github"}, deps, source="local")
    return q.steps


def document_deadline_review(params: dict[str, Any]) -> list[WStep]:
    q = _Seq()
    s1 = q.add(f"Find dates in your documents about '{params['topic']}'", "documents_deadlines", {"query": params["topic"]}, source="documents", verify=["has:facts"])
    s2 = q.add("Pick the one dependable deadline", "verify_deadline_fact", {"facts": From(s1, "facts")}, [s1], source="local", optional=params.get("add_task") is None, verify=["has:fact"])
    deps = [s2]
    if params.get("add_task"):
        deps.append(q.add("Create the task", "task_create", {"fact": From(s2, "fact")}, [s2], source="tasks", verify=["readback"]))
    q.add("Summarize what the documents say", "compose_workflow_report", {"kind": "documents"}, deps, source="local")
    return q.steps


def email_to_browser(params: dict[str, Any]) -> list[WStep]:
    q = _Seq()
    s1 = q.add(f"Find the email about '{params['topic']}'", "gmail_search", {"query": params["topic"], "limit": 5}, source="gmail", verify=["has:emails"])
    s2 = q.add("Find the link in it", "email_links", {"emails": From(s1, "emails")}, [s1], source="gmail", verify=["has:url"])
    s3 = q.add("Open the page", "open_url", {"url": From(s2, "url")}, [s2], source="browser", expected="the page loads")
    q.add("Read what is on the page", "read_page", {}, [s3], source="browser", expected="the page's headings and buttons")
    return q.steps


def application_submit(params: dict[str, Any]) -> list[WStep]:
    """Prepare an application and stop at the final button: the click needs an explicit confirmation showing what will happen."""
    q = _Seq()
    s1 = q.add(f"Find the email about '{params['topic']}'", "gmail_search", {"query": params["topic"], "limit": 5}, source="gmail", verify=["has:emails"])
    s2 = q.add("Find the application link", "email_links", {"emails": From(s1, "emails")}, [s1], source="gmail", verify=["has:url"])
    s3 = q.add("Open the application page", "open_url", {"url": From(s2, "url")}, [s2], source="browser")
    s4 = q.add("Read the page and its requirements", "read_page", {}, [s3], source="browser")
    deps = [s4]
    if _has(params, "documents"):
        deps.append(q.add("Check what the documents say is required", "documents_requirements", {"query": params["topic"]}, [s4], source="documents", optional=True))
    if _has(params, "memory"):
        deps.append(q.add("Check what you've told me before (context only)", "memory_context", {"query": params["topic"]}, [s4], source="memory", optional=True))
    q.add("Submit the application", "click_element", {"name": params.get("button", "Submit"), "role": "button"}, deps, source="browser", expected="the page confirms the submission")
    return q.steps


@dataclass(frozen=True)
class TemplateDef:
    name: str
    description: str
    build: Callable[[dict[str, Any]], list[WStep]]
    systems: frozenset[str]       # required systems (unavailable -> the plan is refused, honestly)
    optional_systems: frozenset[str] = frozenset()
    writes: bool = False


TEMPLATES: dict[str, TemplateDef] = {t.name: t for t in [
    TemplateDef("important_email_review", "Review important emails and say what needs attention", important_email_review, frozenset({"gmail"})),
    TemplateDef("deadline_to_task", "Find a deadline in email and create a task for it", deadline_to_task, frozenset({"gmail", "tasks"}), frozenset({"calendar"}), True),
    TemplateDef("deadline_to_reminder", "Find a deadline in email and set a reminder before it", deadline_to_reminder, frozenset({"gmail", "reminders"}), frozenset({"calendar"}), True),
    TemplateDef("deadline_task_and_reminder", "Find a deadline in email, create a task and a reminder", deadline_task_and_reminder, frozenset({"gmail", "tasks", "reminders"}), frozenset({"calendar"}), True),
    TemplateDef("email_deadlines_to_tasks", "Turn the deadlines in important emails into tasks", email_deadlines_to_tasks, frozenset({"gmail", "tasks"}), frozenset(), True),
    TemplateDef("meeting_preparation", "Prepare for tomorrow's meetings with related emails", meeting_preparation, frozenset({"calendar"}), frozenset({"gmail"})),
    TemplateDef("daily_briefing", "Morning briefing from calendar, email, tasks and deadlines", daily_briefing, frozenset(), frozenset({"calendar", "gmail", "tasks"})),
    TemplateDef("weekly_review", "What this week holds", weekly_review, frozenset({"tasks"}), frozenset({"gmail"})),
    TemplateDef("deadlines_review", "Upcoming deadlines and what to finish", deadlines_review, frozenset({"tasks"})),
    TemplateDef("github_activity_review", "Recent activity in your GitHub repositories", github_activity_review, frozenset({"github"}), frozenset(), False),
    TemplateDef("document_deadline_review", "Find deadlines in your documents", document_deadline_review, frozenset({"documents"})),
    TemplateDef("email_to_browser", "Open the link from an email and read the page", email_to_browser, frozenset({"gmail", "browser"})),
    TemplateDef("application_submit", "Prepare an application from an email, stop before submitting", application_submit, frozenset({"gmail", "browser"}), frozenset({"documents"}), True),
]}
SYSTEM_LABEL = {"gmail": "Gmail", "calendar": "your calendar", "github": "GitHub", "documents": "your documents", "tasks": "your tasks", "reminders": "reminders", "browser": "the browser",
                "memory": "personal memory", "notifications": "notifications"}


# ---- reports ----------------------------------------------------------------------------------------------------------------------------

def _out(wf: Workflow, tool: str) -> dict[str, Any] | None:
    step = next((s for s in wf.steps if s.tool == tool and s.status is SStatus.DONE), None)
    return step.output if step else None


def _when(iso: str | None, zone) -> str:
    d = factlib.date_of(iso or "")
    return "" if d is None else d.astimezone(zone).strftime("%B %d").replace(" 0", " ")


def compose(kind: str, wf: Workflow, board: dict[str, Any], zone) -> str:
    """The final wording for the read-style templates, built only from what the steps actually returned."""
    if kind == "important_emails":
        o = _out(wf, "gmail_search_important") or {}
        emails = o.get("emails", [])
        if not emails:
            return "I checked and none of your recent emails look important."
        lines = [f"You have {len(emails)} important email{'s' if len(emails) != 1 else ''}."]
        for e in emails[:5]:
            why = (", " + e["reasons"][0]) if e.get("reasons") else ""
            lines.append(f"{e['title']} from {e['sender'] or 'someone'}{why}.")
        return " ".join(lines)
    if kind == "meeting_prep":
        events = (_out(wf, "calendar_events") or {}).get("events", [])
        rel = _out(wf, "gmail_related_emails") or {}
        if not events:
            return "You have no events on that day."
        parts = [f"{len(events)} event{'s' if len(events) != 1 else ''}: " + "; ".join(f"{e['title']}" + (f" at {factlib.date_of(e['start']).astimezone(zone):%I:%M %p}".replace(" 0", " ") if e.get("start") and not e.get("all_day") and factlib.date_of(e["start"]) else "") for e in events[:6]) + "."]
        if rel.get("emails"):
            by: dict[str, list[str]] = {}
            for m in rel["emails"]:
                by.setdefault(m.get("for_event", ""), []).append(m["title"])
            parts.append("Related emails: " + "; ".join(f"for {ev}: {', '.join(t[:2])}" for ev, t in by.items()) + ".")
        else:
            parts.append("I didn't find related emails." if _out(wf, "gmail_related_emails") is not None else "")
        return " ".join(p for p in parts if p)
    if kind in ("deadlines", "weekly"):
        d = (_out(wf, "deadlines_overview") or {}).get("deadlines", [])
        t = _out(wf, "tasks_overview") or {}
        parts = []
        if d:
            parts.append(f"{len(d)} deadline{'s' if len(d) != 1 else ''}: " + "; ".join(f"{x['title']} on {_when(x['due'], zone)}" + (" (from email)" if x["source"] == "gmail" else "") for x in d[:6]) + ".")
        else:
            parts.append("I don't see any deadlines coming up.")
        if t.get("count"):
            parts.append(f"You have {t['count']} open task{'s' if t['count'] != 1 else ''}" + (f", {t['overdue']} overdue" if t.get("overdue") else "") + ".")
        emails = (_out(wf, "gmail_search_important") or {}).get("emails", [])
        if kind == "weekly" and emails:
            parts.append(f"{len(emails)} important email{'s' if len(emails) != 1 else ''} need attention.")
        return " ".join(parts)
    if kind == "github":
        o = _out(wf, "github_activity") or {}
        who = o.get("login", "")
        att = o.get("attention", [])
        head = f"On the GitHub account {who}: {o.get('commits', 0)} commit{'s' if o.get('commits', 0) != 1 else ''} recently"
        if not att:
            return head + ", and nothing open that needs attention."
        return head + f", and {len(att)} item{'s' if len(att) != 1 else ''} to look at: " + "; ".join(f"{a['title']} in {a['repo']}" for a in att[:4]) + "."
    if kind == "documents":
        o = _out(wf, "documents_deadlines") or {}
        facts = [Fact.from_dict(f) for f in o.get("facts", [])]
        if not facts:
            return "I didn't find a date in your documents."
        return "Your documents mention: " + "; ".join(f"{f.title or 'a date'} on {_when(f.value, zone)} ({f.status.value.lower().replace('_', ' ')})" for f in facts[:5]) + "."
    return "Done."
