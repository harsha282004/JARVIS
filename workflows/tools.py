"""Operator tools: the typed, validated actions a workflow step may name, and the router that runs them.

    workflow step (tool + typed arguments) -> OperatorRouter.validate (schema, extra keys refused) -> risk/permission computed by code
        -> operator tool (Gmail/Calendar/GitHub/Documents/Memory/Tasks/Reminders via the existing services and the Integration Hub)
        -> browser tool (through the Phase 21 ToolRouter -> BrowserTools -> PermissionManager)

Reads are automatic. The only writes are creating a task and creating a reminder, and both are guarded: only from a fact that is VERIFIED or HIGH_CONFIDENCE, only in a
workflow the user asked for, deduplicated against what already exists and against the effect ledger, recorded write-ahead so a crash cannot double them, and read back
afterwards. There is no tool that sends email or messages, uploads, deletes, publishes or changes an account; those cannot be planned at all. Text from emails,
documents, pages and issues only ever appears in outputs as data; it never becomes a tool name, an argument key or a URL that was not validated.
"""

import re
import threading
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from agent.tasks.models import OPEN_STATUSES, ReminderStatus, TaskPriority
from autonomy.models import Risk
from autonomy.toolrouter import ToolRouter
from backend.core.logging import get_logger
from backend.core.metrics import metrics
from backend.core.security.trust import sanitize_external, scan_for_injection
from browser.urlsafe import validate_url
from integrations.hub.models import ErrorKind, Permission, TRANSIENT_KINDS
from workflows import facts as factlib
from workflows import priority as prio
from workflows.models import ACTIONABLE_STATUSES, Fact, FactStatus, FailureKind, From
from workflows.store import WorkflowStore

logger = get_logger(__name__)

_ID = r"^[A-Za-z0-9_.\-]{1,120}$"
_URL_RE = re.compile(r"https?://[^\s<>\"')\]]+", re.I)
_LINK_HINT = re.compile(r"\b(appl(?:y|ication)|register|registration|portal|submit|form|enrol|enroll|sign ?up|rsvp|confirm)\b", re.I)
_REQ_LINE = re.compile(r"^\s*(?:[-*•]|\d+[.)])\s+(.+\S)\s*$")
_REQ_HEAD = re.compile(r"(required documents?|documents? required|you (?:must|need to|will need to) (?:submit|provide|bring|upload|send)|please (?:submit|provide|bring|attach|upload)|"
                       r"(?:must|need to) (?:submit|provide|include)|checklist|what to (?:bring|submit))", re.I)


class _A(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class NoArgs(_A):
    pass


class LimitA(_A):
    limit: int = Field(default=10, ge=1, le=25)


class SearchA(_A):
    query: str = Field(min_length=1, max_length=120)
    limit: int = Field(default=10, ge=1, le=25)


class EmailsA(_A):
    emails: list[dict[str, Any]] = Field(max_length=25)
    topic: str | None = Field(default=None, max_length=80)


class FactsA(_A):
    facts: list[dict[str, Any]] = Field(max_length=40)
    minimum: Literal["HIGH_CONFIDENCE", "VERIFIED"] = "HIGH_CONFIDENCE"


class FactA(_A):
    fact: dict[str, Any]


class ReminderTimeA(_A):
    fact: dict[str, Any]
    days_before: int = Field(default=0, ge=0, le=60)
    hour: int = Field(default=9, ge=0, le=23)


class TaskCreateA(_A):
    title: str = Field(default="", max_length=200)      # empty: the fact's own title
    fact: dict[str, Any]


class ReminderCreateA(_A):
    message: str = Field(default="", max_length=300)    # empty: "Reminder: <title> is due <date>"
    at: str = Field(min_length=10, max_length=40)
    fact: dict[str, Any]


class GhA(_A):
    login: str = Field(min_length=1, max_length=60)
    days: int = Field(default=7, ge=1, le=60)


class BatchA(_A):
    facts: list[dict[str, Any]] = Field(max_length=25)
    items: list[dict[str, Any]] = Field(default_factory=list, max_length=25)


class DayA(_A):
    day: str = Field(default="today", pattern=r"^(?:today|tomorrow|\d{4}-\d{2}-\d{2})$")


class EventsA(_A):
    events: list[dict[str, Any]] = Field(max_length=25)
    limit: int = Field(default=3, ge=1, le=6)


class DaysA(_A):
    days: int = Field(default=7, ge=1, le=60)


class ItemsA(_A):
    items: list[dict[str, Any]] = Field(max_length=25)


class LinksA(_A):
    emails: list[dict[str, Any]] = Field(max_length=10)
    hint: str = Field(default="application", max_length=40)


class BriefingA(_A):
    emails: list[dict[str, Any]] = Field(default_factory=list, max_length=25)
    events: list[dict[str, Any]] = Field(default_factory=list, max_length=25)
    tasks: list[dict[str, Any]] = Field(default_factory=list, max_length=50)
    deadlines: list[dict[str, Any]] = Field(default_factory=list, max_length=50)
    detail: bool = False


class NotifyA(_A):
    text: str = Field(min_length=1, max_length=700)
    priority: Literal["low", "normal", "high"] = "normal"
    key: str = Field(default="", max_length=80)


class ReportA(_A):
    kind: str = Field(min_length=1, max_length=40)


@dataclass
class ToolOut:
    ok: bool
    data: dict[str, Any] = field(default_factory=dict)
    message: str = ""
    failure: FailureKind | None = None
    verified: bool = True
    warnings: list[str] = field(default_factory=list)
    reused: bool = False              # the object already existed: nothing was created
    choices: list[dict[str, Any]] = field(default_factory=list)   # candidates for a question to the user
    untrusted: bool = False


@dataclass
class OpSpec:
    name: str
    system: str               # gmail | calendar | github | documents | memory | tasks | reminders | notifications | local
    args: type[_A]
    description: str
    risk: Risk = Risk.READ_ONLY
    side_effect: bool = False
    retry_safe: bool = True
    handler: str = ""


SPECS: dict[str, OpSpec] = {s.name: s for s in [
    OpSpec("gmail_search_important", "gmail", LimitA, "Important and unread emails (classified by the Gmail analysis)", handler="h_gmail_important"),
    OpSpec("gmail_search", "gmail", SearchA, "Search email by words", handler="h_gmail_search"),
    OpSpec("gmail_extract_deadlines", "gmail", EmailsA, "Dates/deadlines stated in emails, classified with provenance", handler="h_extract"),
    OpSpec("verify_deadline_fact", "local", FactsA, "Pick the one actionable deadline fact or say why there is none", handler="h_verify"),
    OpSpec("calendar_compare_fact", "calendar", FactA, "Compare a fact's date with the calendar; a different date is reported, never resolved", handler="h_compare"),
    OpSpec("compute_reminder_time", "local", ReminderTimeA, "Reminder time = fact date minus N days at a fixed hour, checked to be in the future", handler="h_remind_time"),
    OpSpec("task_create", "tasks", TaskCreateA, "Create ONE task from a verified fact (deduplicated, provenance attached, read back)", Risk.LOW_RISK, True, False, "h_task_create"),
    OpSpec("task_create_batch", "tasks", BatchA, "Create tasks for actionable facts (each deduplicated and read back)", Risk.LOW_RISK, True, False, "h_task_batch"),
    OpSpec("reminder_create", "reminders", ReminderCreateA, "Create ONE reminder (deduplicated, read back)", Risk.LOW_RISK, True, False, "h_reminder_create"),
    OpSpec("calendar_events", "calendar", DayA, "The calendar for a day", handler="h_events"),
    OpSpec("gmail_related_emails", "gmail", EventsA, "Emails related to the given events (by their titles)", handler="h_related"),
    OpSpec("tasks_overview", "tasks", DaysA, "Open, overdue and upcoming tasks", handler="h_tasks"),
    OpSpec("deadlines_overview", "tasks", DaysA, "Upcoming deadlines: task due dates and dates found in emails", handler="h_deadlines"),
    OpSpec("github_identity", "github", NoArgs, "Which GitHub account is connected (proves 'my' repositories)", handler="h_gh_identity"),
    OpSpec("github_activity", "github", GhA, "Recent activity in the connected account's repositories: what needs attention", handler="h_gh_activity"),
    OpSpec("documents_search", "documents", SearchA, "Search the user's indexed documents", handler="h_docs_search"),
    OpSpec("documents_requirements", "documents", SearchA, "Documents or steps a document says are required", handler="h_docs_requirements"),
    OpSpec("documents_deadlines", "documents", SearchA, "Dates a document states (with file and page provenance), classified like email dates", handler="h_docs_deadlines"),
    OpSpec("memory_context", "memory", SearchA, "Relevant personal memory (context only: never overrides a current source)", handler="h_memory"),
    OpSpec("email_links", "gmail", LinksA, "Web addresses in an email, validated, most application-like first", handler="h_links"),
    OpSpec("briefing_compose", "local", BriefingA, "Priority synthesis and wording for the morning briefing", handler="h_briefing"),
    OpSpec("notify_user", "notifications", NotifyA, "Tell the user (voice/desktop, deduplicated, honoring do-not-disturb)", handler="h_notify"),
    OpSpec("compose_workflow_report", "local", ReportA, "Compose the final answer from what the workflow actually did", handler="h_report"),
]}
BROWSER_TOOLS = ("open_url", "read_page", "find_element", "click_element", "upload_file", "get_page_state")
ALL_TOOL_NAMES = frozenset(SPECS) | frozenset(BROWSER_TOOLS)
FORBIDDEN_NAMES = frozenset({"send_email", "reply_email", "forward_email", "send_message", "delete_email", "delete_task", "delete_file", "execute_shell", "run_command", "read_file",
                             "publish", "submit_form", "purchase", "share_document", "change_settings", "post_message"})


@dataclass
class OperatorContext:
    hub: Any = None
    tasks: Any = None
    reminders: Any = None
    memory: Any = None
    tool_router: ToolRouter | None = None
    store: WorkflowStore | None = None
    zone: Any = None
    clock: Any = None                       # () -> aware datetime
    notify: Any = None                      # (text, priority) -> None
    center: Any = None                      # NotificationCenter (for the briefing's unacknowledged notifications)
    effects_lock: threading.RLock = field(default_factory=threading.RLock)
    browser_lock: threading.RLock = field(default_factory=threading.RLock)
    notified: dict[str, float] = field(default_factory=dict)


def _fail_from_hub(result) -> tuple[FailureKind, str]:
    err = result.error or {}
    kind = err.get("type", "")
    msg = err.get("message", "That didn't work.")
    if kind == ErrorKind.AUTH_ERROR.value or kind == ErrorKind.CONFIGURATION_ERROR.value:
        return FailureKind.AUTHENTICATION, msg
    if kind == ErrorKind.PERMISSION_ERROR.value:
        return FailureKind.PERMISSION, msg
    if kind == ErrorKind.NOT_FOUND.value:
        return FailureKind.DATA_MISSING, msg
    if kind in {k.value for k in TRANSIENT_KINDS}:
        return FailureKind.TEMPORARY, msg
    if kind == ErrorKind.INVALID_REQUEST.value:
        return FailureKind.SECURITY_BLOCK, msg
    return FailureKind.EXTERNAL_SERVICE, msg


class OperatorRouter:
    """The Personal Operator's door. Wraps the Phase 21 ToolRouter for browser tools; everything else is an operator tool over the existing services."""

    def __init__(self, ctx: OperatorContext):
        self.ctx = ctx

    # ---- registry -----------------------------------------------------------------------------------------------------------------

    def known(self, tool: str) -> bool:
        return tool in ALL_TOOL_NAMES and tool not in FORBIDDEN_NAMES

    def system_of(self, tool: str) -> str:
        return SPECS[tool].system if tool in SPECS else "browser"

    def available(self, tool: str) -> tuple[bool, str]:
        c = self.ctx
        if not self.known(tool):
            return False, "That isn't an action I have."
        system = self.system_of(tool)
        if system == "browser":
            return (c.tool_router is not None and c.tool_router.available(tool)[0], "The browser agent isn't available.")
        if system in ("gmail", "calendar", "github", "documents"):
            if c.hub is None:
                return False, f"{system.capitalize()} isn't available."
            perm = {"gmail": Permission.SEARCH_EMAIL, "calendar": Permission.READ_EVENTS, "github": Permission.READ_REPOSITORIES, "documents": Permission.READ_DOCUMENTS}[system]
            ok, reason = c.hub.registry.allowed(system, perm)
            return ok, reason if not ok else ""
        if system == "tasks":
            return c.tasks is not None, "Tasks are turned off."
        if system == "reminders":
            return c.reminders is not None, "Reminders are turned off."
        if system == "memory":
            return c.memory is not None, "Personal memory isn't enabled."
        return True, ""

    def validate_shape(self, tool: str, arguments: dict[str, Any]) -> str | None:
        """At plan time: the tool exists, argument names belong to its schema, required ones are present (values may still be references)."""
        if not self.known(tool):
            return "That isn't an action I have."
        if tool in SPECS:
            fields = SPECS[tool].args.model_fields
            extra = set(arguments) - set(fields)
            if extra:
                return f"{tool} does not take {sorted(extra)[0]}"
            missing = [k for k, f in fields.items() if f.is_required() and k not in arguments]
            if missing:
                return f"{tool} needs {missing[0]}"
            literal = {k: v for k, v in arguments.items() if not isinstance(v, From)}
            try:
                SPECS[tool].args(**literal)
            except ValidationError as exc:
                for err in exc.errors():
                    loc = err["loc"][0] if err["loc"] else ""
                    if err["type"] == "missing" and loc in arguments:
                        continue                                           # supplied by a reference, checked when it is resolved
                    return f"{tool} has an invalid {loc}"
            return None
        return self.ctx.tool_router.validate(tool, {k: (v if not hasattr(v, "step") else "x") for k, v in arguments.items()}) if self.ctx.tool_router is not None else "The browser agent isn't available."

    def risk_of(self, tool: str, arguments: dict[str, Any], description: str = "") -> tuple[Risk, str]:
        if tool in SPECS:
            return SPECS[tool].risk, f"operator:{SPECS[tool].system}"
        if self.ctx.tool_router is None:
            return Risk.READ_ONLY, "browser_navigation"
        args = {k: (v if not hasattr(v, "step") else "x") for k, v in arguments.items()}
        return self.ctx.tool_router.risk_of(tool, args, description)

    def side_effect(self, tool: str) -> bool:
        return SPECS[tool].side_effect if tool in SPECS else tool in ("upload_file", "click_element")

    def retry_safe(self, tool: str) -> bool:
        return SPECS[tool].retry_safe if tool in SPECS else tool in ("open_url", "read_page", "find_element", "get_page_state")

    # ---- execution -----------------------------------------------------------------------------------------------------------------

    def call(self, tool: str, arguments: dict[str, Any], *, workflow, board: dict[str, Any], confirmed: bool = False) -> ToolOut:
        """Run one step's tool. Never raises."""
        ok, reason = self.available(tool)
        if not ok:
            return ToolOut(False, message=reason, failure=FailureKind.AUTHENTICATION if "isn't connected" in reason or "connect" in reason.lower() else FailureKind.DATA_MISSING)
        began = time.perf_counter()
        try:
            if tool in SPECS:
                spec = SPECS[tool]
                try:
                    args = spec.args(**arguments)
                except ValidationError:
                    return ToolOut(False, message="Those details aren't valid for this step.", failure=FailureKind.SECURITY_BLOCK)
                if spec.side_effect and workflow.requested_by != "user":
                    return ToolOut(False, message="Only a workflow you asked for may create tasks or reminders.", failure=FailureKind.SECURITY_BLOCK)
                out = getattr(self, spec.handler)(args, workflow, board)
            else:
                out = self._browser(tool, arguments, workflow, confirmed)
        except Exception as exc:  # noqa: BLE001 - a tool failure is a classified result, never an exception into the workflow loop
            logger.error("Operator tool %s failed (%s)", tool, type(exc).__name__)
            out = ToolOut(False, message=f"That step failed ({type(exc).__name__}).", failure=FailureKind.EXTERNAL_SERVICE)
        finally:
            metrics.observe(f"operator.tool.{tool}_ms", (time.perf_counter() - began) * 1000)
        return out

    def _browser(self, tool: str, arguments: dict[str, Any], workflow, confirmed: bool) -> ToolOut:
        router = self.ctx.tool_router
        with self.ctx.browser_lock:  # one workflow at a time may drive the browser session
            o = router.call(tool, arguments, session_id=workflow.session_id or workflow.workflow_id, blackboard={}, confirmed=confirmed)
        if o.needs_confirmation:
            return ToolOut(False, message=o.message, failure=FailureKind.PERMISSION, data={"needs_confirmation": True})
        if o.success:
            return ToolOut(True, {**o.data, "url": o.url}, o.message, verified=o.verified, untrusted=o.untrusted)
        kind = FailureKind.TEMPORARY if any(k in (o.error or "").lower() for k in ("too long", "couldn't connect", "resolve")) else FailureKind.VERIFICATION_FAILED
        if o.data.get("ambiguous"):
            return ToolOut(False, {"candidates": o.data.get("candidates", [])}, o.error, FailureKind.AMBIGUOUS, choices=list(o.data.get("candidates", [])))
        if o.data.get("captcha") or o.data.get("login_required"):
            kind = FailureKind.AUTHENTICATION
        return ToolOut(False, message=o.error or "That didn't work in the browser.", failure=kind)

    # ---- helpers --------------------------------------------------------------------------------------------------------------------

    def _now(self) -> datetime:
        return self.ctx.clock()

    def _hub(self, tool: str, args: dict[str, Any]):
        return self.ctx.hub.tools.call(tool, args)

    def _emails_from(self, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        out = []
        for i in items:
            md = i.get("metadata", {}) or {}
            out.append({"id": i.get("source_id") or i.get("id"), "title": sanitize_external(str(i.get("title", "")), 120), "sender": sanitize_external(str(md.get("sender", "")), 60),
                        "topic": md.get("topic"), "importance": md.get("importance"), "reasons": [sanitize_external(str(r), 80) for r in (md.get("importance_reasons") or [])][:3],
                        "unread": bool(md.get("unread")), "flagged": bool(md.get("injection_suspected")), "ts": i.get("source_timestamp")})
        return out

    # ---- Gmail --------------------------------------------------------------------------------------------------------------------------

    def h_gmail_important(self, a: LimitA, wf, board) -> ToolOut:
        r = self._hub("search_email", {"query": "in:inbox", "limit": 25})
        if not r.success:
            k, m = _fail_from_hub(r)
            return ToolOut(False, message=m, failure=k)
        emails = self._emails_from(r.data)
        important = [e for e in emails if str(e.get("importance", "")).upper() in ("CRITICAL", "IMPORTANT")][: a.limit]
        warn = ["These came from my saved copies because Gmail is unreachable."] if r.metadata.get("from_cache") else []
        return ToolOut(True, {"emails": important, "count": len(important), "email_ids": [e["id"] for e in important]}, f"Found {len(important)} important email{'s' if len(important) != 1 else ''}.", warnings=warn, untrusted=True)

    def h_gmail_search(self, a: SearchA, wf, board) -> ToolOut:
        r = self._hub("search_email", {"query": a.query, "limit": a.limit})
        if not r.success:
            k, m = _fail_from_hub(r)
            return ToolOut(False, message=m, failure=k)
        emails = self._emails_from(r.data)
        if not emails:
            return ToolOut(False, message=f"I didn't find any emails about {a.query}.", failure=FailureKind.DATA_MISSING)
        return ToolOut(True, {"emails": emails, "count": len(emails), "email_ids": [e["id"] for e in emails]}, f"Found {len(emails)} email{'s' if len(emails) != 1 else ''}.", untrusted=True,
                       warnings=["These came from my saved copies because Gmail is unreachable."] if r.metadata.get("from_cache") else [])

    def h_extract(self, a: EmailsA, wf, board) -> ToolOut:
        facts: list[Fact] = []
        warnings: list[str] = []
        topic_words = [w for w in re.findall(r"[a-z0-9]{3,}", (a.topic or "").lower())]
        for e in a.emails[:15]:
            mid = str(e.get("id", ""))
            if not re.match(_ID, mid):
                continue
            r = self._hub("read_email", {"message_id": mid})
            if not r.success:
                k, m = _fail_from_hub(r)
                if k in (FailureKind.AUTHENTICATION, FailureKind.PERMISSION):
                    return ToolOut(False, message=m, failure=k)
                warnings.append(f"I couldn't read one email ({m.rstrip('.')}).")
                continue
            flagged = bool((r.data.get("email", {}).get("metadata", {}) or {}).get("injection_suspected"))
            subject = str(r.data.get("email", {}).get("title", ""))
            for item in r.data.get("extracted", []):
                if item.get("kind") not in ("deadline", "event"):
                    continue
                f = factlib.classify_deadline(item, message_flagged=flagged)
                if f is None:
                    continue
                if topic_words and not (any(w in (f.title + " " + subject + " " + f.original_text).lower() for w in topic_words)):
                    continue
                f.notes.append(f"from the email '{sanitize_external(subject, 60)}'")
                facts.append(f)
        dicts = [f.to_dict() for f in facts]
        if not dicts:
            return ToolOut(False, message="I didn't find a date in those emails.", failure=FailureKind.DATA_MISSING, warnings=warnings)
        return ToolOut(True, {"facts": dicts, "count": len(dicts)}, f"Found {len(dicts)} date{'s' if len(dicts) != 1 else ''} in the emails.", warnings=warnings, untrusted=True)

    def h_verify(self, a: FactsA, wf, board) -> ToolOut:
        facts = [Fact.from_dict(f) for f in a.facts if isinstance(f, dict)]
        floor = {FactStatus.VERIFIED} if a.minimum == "VERIFIED" else ACTIONABLE_STATUSES
        good = [f for f in facts if f.status in floor and (factlib.date_of(f.value) is not None)]
        if not good:
            worst = next((f for f in facts if f.status is FactStatus.AMBIGUOUS), None) or next(iter(facts), None)
            if worst is None:
                return ToolOut(False, message="I didn't find a deadline.", failure=FailureKind.DATA_MISSING)
            return ToolOut(False, {"fact": worst.to_dict()}, factlib.explain_not_actionable(worst), FailureKind.AMBIGUOUS if worst.status is not FactStatus.UNVERIFIED else FailureKind.SECURITY_BLOCK)
        dates = {factlib.date_of(f.value).astimezone(self.ctx.zone).date() for f in good}  # type: ignore[union-attr]
        if len(dates) > 1:
            choices = [{"n": i + 1, "name": f"{f.title} on {factlib.date_of(f.value).astimezone(self.ctx.zone):%B %d}", "fact": f.to_dict()} for i, f in enumerate(sorted(good, key=lambda x: x.value)[:4])]
            return ToolOut(False, {"choices": choices}, "I found more than one date: " + "; ".join(f"{c['n']}, {c['name']}" for c in choices) + ". Which one do you mean?", FailureKind.AMBIGUOUS, choices=choices)
        best = factlib.prefer_current(good)
        assert best is not None
        return ToolOut(True, {"fact": best.to_dict()}, f"The deadline is {factlib.date_of(best.value).astimezone(self.ctx.zone):%B %d} ({best.status.value.lower().replace('_', ' ')}).")

    def h_compare(self, a: FactA, wf, board) -> ToolOut:
        fact = Fact.from_dict(a.fact)
        due = factlib.date_of(fact.value)
        if due is None:
            return ToolOut(False, message="That fact has no usable date.", failure=FailureKind.DATA_MISSING)
        r = self._hub("search_calendar", {"start": (due - timedelta(days=45)).isoformat(), "end": (due + timedelta(days=45)).isoformat(), "limit": 25})
        if not r.success:
            k, m = _fail_from_hub(r)
            return ToolOut(False, message=m, failure=k)
        conflict = factlib.find_conflict(fact, r.data, self.ctx.zone)
        if conflict:
            msg = (f"I found conflicting dates: the email says {datetime.fromisoformat(conflict['fact_date']):%B %d}, while your calendar has "
                   f"{datetime.fromisoformat(conflict['calendar_date']):%B %d} for '{conflict['calendar_title']}'. Which is right?")
            return ToolOut(False, {"conflict": conflict, "fact": fact.to_dict()}, msg, FailureKind.AMBIGUOUS, choices=[{"n": 1, "name": f"the email's date, {conflict['fact_date']}"}, {"n": 2, "name": f"the calendar's date, {conflict['calendar_date']}"}])
        return ToolOut(True, {"fact": fact.to_dict(), "conflict": None}, "The calendar doesn't disagree.")

    def h_remind_time(self, a: ReminderTimeA, wf, board) -> ToolOut:
        fact = Fact.from_dict(a.fact)
        due = factlib.date_of(fact.value)
        if due is None:
            return ToolOut(False, message="That fact has no usable date.", failure=FailureKind.DATA_MISSING)
        local = due.astimezone(self.ctx.zone)
        target = (local - timedelta(days=a.days_before)).replace(hour=a.hour, minute=0, second=0, microsecond=0)
        if target <= self._now():
            return ToolOut(False, {"remind_at": target.isoformat()}, f"That reminder time ({target:%B %d at %I:%M %p}) has already passed.", FailureKind.DATA_MISSING)
        return ToolOut(True, {"remind_at": target.isoformat(), "due": local.isoformat(), "day": target.date().isoformat()}, f"Reminder on {target:%B %d at %I:%M %p}.")

    # ---- writes (guarded) -------------------------------------------------------------------------------------------------------------------

    def _key(self, kind: str, fact: Fact, extra: str = "") -> str:
        return f"{kind}:{fact.source}:{fact.source_id}:{fact.value[:10]}:{extra}".lower()

    def _fact_ok(self, fact: Fact) -> ToolOut | None:
        if fact.status not in ACTIONABLE_STATUSES:
            return ToolOut(False, message=factlib.explain_not_actionable(fact), failure=FailureKind.AMBIGUOUS if fact.status is not FactStatus.UNVERIFIED else FailureKind.SECURITY_BLOCK)
        return None

    def h_task_create(self, a: TaskCreateA, wf, board) -> ToolOut:
        fact = Fact.from_dict(a.fact)
        if (bad := self._fact_ok(fact)) is not None:
            return bad
        return self._create_task(a.title or fact.title or "Follow up", fact.value if fact.name == "deadline" else "", fact, wf)

    def _create_task(self, title: str, due_iso: str, fact: Fact, wf) -> ToolOut:
        c = self.ctx
        due = factlib.date_of(due_iso) if due_iso else None
        key = self._key("task", fact, title[:40])
        with c.effects_lock:
            ledger = c.store.effect_lookup(key) if c.store else None
            if ledger and ledger["state"] == "done":
                try:
                    t = c.tasks.get_task(ledger["object_id"])
                    return ToolOut(True, {"task_id": t.task_id, "title": t.title, "due": due_iso, "reused": True}, f"The task '{t.title}' already exists.", reused=True)
                except Exception:  # noqa: BLE001 - the task was deleted since: fall through and check again
                    c.store.effect_forget(key)
            existing = self._find_task(title, due, key)
            if existing is not None:
                if c.store:
                    c.store.effect_done(key, "task", existing.task_id, wf.workflow_id)
                return ToolOut(True, {"task_id": existing.task_id, "title": existing.title, "due": due_iso, "reused": True}, f"The task '{existing.title}' already exists, so I didn't add a duplicate.", reused=True)
            if c.store:
                c.store.effect_begin(key, "task", wf.workflow_id)   # write-ahead: a crash after this line is recoverable
            meta = {"provenance": fact.provenance(), "workflow_id": wf.workflow_id, "idempotency_key": key}
            notes = f"From {fact.source} ({fact.source_id}); {fact.status.value.lower().replace('_', ' ')}."
            task = c.tasks.create_task(title, notes=notes, priority=TaskPriority.HIGH if (due and (due - self._now()) < timedelta(days=3)) else None, due_at=due, session_id=wf.session_id or None,
                                       source="workflow", metadata=meta)
            back = c.tasks.get_task(task.task_id)   # read it back
            if c.store:
                c.store.effect_done(key, "task", back.task_id, wf.workflow_id)
                c.store.link(("task", back.task_id), (fact.source, fact.source_id), f"created from {fact.name}", fact.confidence, "workflow")
        ok = back.title == title and (due is None or (back.due_at is not None and abs((back.due_at - due).total_seconds()) < 60))
        return ToolOut(ok, {"task_id": back.task_id, "title": back.title, "due": due_iso, "reused": False}, f"Created the task '{back.title}'.", verified=ok,
                       failure=None if ok else FailureKind.VERIFICATION_FAILED)

    def _find_task(self, title: str, due: datetime | None, key: str):
        tasks = self.ctx.tasks.list_tasks(statuses=set(OPEN_STATUSES), limit=200)
        for t in tasks:
            if (t.metadata or {}).get("idempotency_key") == key:
                return t
        norm = re.sub(r"[^a-z0-9]+", " ", title.lower()).strip()
        for t in tasks:
            if re.sub(r"[^a-z0-9]+", " ", t.title.lower()).strip() == norm and (due is None or t.due_at is None or abs((t.due_at - due).total_seconds()) < 86400):
                return t
        return None

    def h_task_batch(self, a: BatchA, wf, board) -> ToolOut:
        created, reused, skipped = [], [], []
        for d in a.facts:
            fact = Fact.from_dict(d)
            if fact.status not in ACTIONABLE_STATUSES:
                skipped.append(f"'{fact.title[:50]}' ({factlib.explain_not_actionable(fact).rstrip('.').lower()})")
                continue
            title = fact.title[:200] or "Follow up"
            out = self._create_task(title, fact.value if fact.name == "deadline" else "", fact, wf)
            if not out.ok:
                return ToolOut(False, {"count": len(created)}, out.message, out.failure)
            (reused if out.reused else created).append(out.data["task_id"])
        warnings = [f"I skipped {s}." for s in skipped]
        ok = bool(created or reused)
        return ToolOut(ok, {"task_ids": created + reused, "count": len(created), "reused": len(reused)}, f"Created {len(created)} task{'s' if len(created) != 1 else ''}" + (f"; {len(reused)} already existed" if reused else "") + ".",
                       failure=None if ok else FailureKind.DATA_MISSING, warnings=warnings, reused=bool(reused and not created))

    def h_reminder_create(self, a: ReminderCreateA, wf, board) -> ToolOut:
        c = self.ctx
        fact = Fact.from_dict(a.fact)
        if (bad := self._fact_ok(fact)) is not None:
            return bad
        at = factlib.date_of(a.at)
        due_on = factlib.date_of(fact.value)
        message = a.message or f"Reminder: {fact.title or 'a deadline'}" + (f" is due {due_on.astimezone(self.ctx.zone):%B %d}" if due_on else "")
        if at is None or at <= self._now():
            return ToolOut(False, message="That reminder time isn't in the future.", failure=FailureKind.DATA_MISSING)
        key = self._key("reminder", fact, at.isoformat()[:16])
        with c.effects_lock:
            ledger = c.store.effect_lookup(key) if c.store else None
            if ledger and ledger["state"] == "done":
                try:
                    r = c.reminders.get_reminder(ledger["object_id"])
                    return ToolOut(True, {"reminder_id": r.reminder_id, "remind_at": a.at, "reused": True}, "That reminder already exists.", reused=True)
                except Exception:  # noqa: BLE001
                    c.store.effect_forget(key)
            for r in c.reminders.list_reminders(statuses={ReminderStatus.SCHEDULED}, limit=200):
                if (r.metadata or {}).get("idempotency_key") == key or (r.message.strip().lower() == message.strip().lower() and abs((r.scheduled_at - at).total_seconds()) < 120):
                    if c.store:
                        c.store.effect_done(key, "reminder", r.reminder_id, wf.workflow_id)
                    return ToolOut(True, {"reminder_id": r.reminder_id, "remind_at": a.at, "reused": True}, "That reminder already exists, so I didn't add a duplicate.", reused=True)
            if c.store:
                c.store.effect_begin(key, "reminder", wf.workflow_id)
            r = c.reminders.create_reminder(message, at, session_id=wf.session_id or None, source="workflow",
                                            metadata={"provenance": fact.provenance(), "workflow_id": wf.workflow_id, "idempotency_key": key})
            back = c.reminders.get_reminder(r.reminder_id)
            if c.store:
                c.store.effect_done(key, "reminder", back.reminder_id, wf.workflow_id)
                c.store.link(("reminder", back.reminder_id), (fact.source, fact.source_id), "reminder for a deadline", fact.confidence, "workflow")
        ok = back.status is ReminderStatus.SCHEDULED and abs((back.scheduled_at - at).total_seconds()) < 60
        return ToolOut(ok, {"reminder_id": back.reminder_id, "remind_at": a.at, "reused": False}, f"Scheduled a reminder for {at.astimezone(self.ctx.zone):%B %d at %I:%M %p}.", verified=ok,
                       failure=None if ok else FailureKind.VERIFICATION_FAILED)

    # ---- Calendar ------------------------------------------------------------------------------------------------------------------------

    def _day_bounds(self, day: str) -> tuple[datetime, datetime, date]:
        today = self._now().astimezone(self.ctx.zone).date()
        d = today if day == "today" else today + timedelta(days=1) if day == "tomorrow" else date.fromisoformat(day)
        start = datetime.combine(d, datetime.min.time(), tzinfo=self.ctx.zone)
        return start, start + timedelta(days=1), d

    def h_events(self, a: DayA, wf, board) -> ToolOut:
        start, end, d = self._day_bounds(a.day)
        r = self._hub("search_calendar", {"start": start.isoformat(), "end": end.isoformat(), "limit": 25})
        if not r.success:
            k, m = _fail_from_hub(r)
            return ToolOut(False, message=m, failure=k)
        events = [{"id": e.get("id"), "title": sanitize_external(str(e.get("title", "")), 100), "start": e.get("source_timestamp"), "all_day": bool((e.get("metadata") or {}).get("all_day")),
                   "end": (e.get("metadata") or {}).get("end")} for e in r.data]
        return ToolOut(True, {"events": events, "count": len(events), "day": d.isoformat(), "event_ids": [e["id"] for e in events]}, f"{len(events)} event{'s' if len(events) != 1 else ''} on {d:%A}.", untrusted=True)

    def h_related(self, a: EventsA, wf, board) -> ToolOut:
        found: dict[str, dict[str, Any]] = {}
        per_event: list[dict[str, Any]] = []
        for e in a.events[:6]:
            title = str(e.get("title", ""))
            words = " ".join(w for w in re.findall(r"[A-Za-z0-9]{3,}", title)[:4])
            if not words:
                continue
            r = self._hub("search_email", {"query": words, "limit": a.limit})
            if not r.success:
                k, m = _fail_from_hub(r)
                if k in (FailureKind.AUTHENTICATION, FailureKind.PERMISSION):
                    return ToolOut(False, message=m, failure=k)
                continue
            mails = self._emails_from(r.data)
            per_event.append({"event": title, "emails": [m["id"] for m in mails]})
            for m in mails:
                found.setdefault(m["id"], {**m, "for_event": title})
        return ToolOut(True, {"emails": list(found.values()), "per_event": per_event, "count": len(found), "email_ids": list(found)}, f"Found {len(found)} related email{'s' if len(found) != 1 else ''}.", untrusted=True)

    # ---- Tasks / deadlines -------------------------------------------------------------------------------------------------------------------

    def h_tasks(self, a: DaysA, wf, board) -> ToolOut:
        now = self._now()
        open_tasks = self.ctx.tasks.list_tasks(statuses=set(OPEN_STATUSES), limit=100)
        rows = [{"id": t.task_id, "title": t.title, "due": t.due_at.isoformat() if t.due_at else None, "priority": int(t.priority), "overdue": bool(t.due_at and t.due_at < now)} for t in open_tasks]
        return ToolOut(True, {"tasks": rows, "count": len(rows), "overdue": sum(1 for r in rows if r["overdue"])}, f"{len(rows)} open task{'s' if len(rows) != 1 else ''}.")

    def h_deadlines(self, a: DaysA, wf, board) -> ToolOut:
        now = self._now()
        horizon = now + timedelta(days=a.days)
        rows: list[dict[str, Any]] = []
        for t in self.ctx.tasks.list_tasks(statuses=set(OPEN_STATUSES), limit=100) if self.ctx.tasks is not None else []:
            if t.due_at and t.due_at <= horizon:
                rows.append({"title": t.title, "due": t.due_at.isoformat(), "source": "tasks", "source_id": t.task_id, "status": "VERIFIED"})
        warnings: list[str] = []
        if self.ctx.hub is not None and self.ctx.hub.registry.allowed("gmail", Permission.SEARCH_EMAIL)[0]:
            from integrations.hub.models import ItemKind

            for item in self.ctx.hub.repo.search("", source="gmail", kind=ItemKind.DEADLINE, since=now - timedelta(days=1), limit=40):
                d = item.to_dict()
                f = factlib.classify_deadline(d)
                due = factlib.date_of(d.get("source_timestamp") or "")
                if f is None or due is None or due > horizon or due < now - timedelta(days=1):
                    continue
                if f.status not in ACTIONABLE_STATUSES:
                    warnings.append(f"A possible deadline '{f.title[:50]}' isn't stated clearly enough to include.")
                    continue
                if any(r["title"].lower() == f.title.lower() and r["due"][:10] == f.value[:10] for r in rows):
                    continue
                rows.append({"title": f.title, "due": f.value, "source": "gmail", "source_id": f.source_id, "status": f.status.value})
        rows.sort(key=lambda r: r["due"])
        return ToolOut(True, {"deadlines": rows, "count": len(rows)}, f"{len(rows)} deadline{'s' if len(rows) != 1 else ''} in the next {a.days} days.", warnings=warnings[:3])

    # ---- GitHub -----------------------------------------------------------------------------------------------------------------------------------

    def h_gh_identity(self, a: NoArgs, wf, board) -> ToolOut:
        try:
            login = self.ctx.hub.registry.adapter("github").identity()
        except Exception as exc:  # noqa: BLE001
            from integrations.hub.models import classify_error

            err = classify_error(exc, "GitHub")
            kind = FailureKind.AUTHENTICATION if err.kind in (ErrorKind.AUTH_ERROR, ErrorKind.CONFIGURATION_ERROR) else FailureKind.TEMPORARY if err.kind in TRANSIENT_KINDS else FailureKind.EXTERNAL_SERVICE
            return ToolOut(False, message="I can't confirm which GitHub account is connected, so I won't call any results yours. " + err.message, failure=kind)
        return ToolOut(True, {"login": sanitize_external(login, 60)}, f"Connected as {sanitize_external(login, 60)}.")

    def h_gh_activity(self, a: GhA, wf, board) -> ToolOut:
        login = a.login
        repos = self._hub("search_github", {"limit": 6})
        if not repos.success:
            k, m = _fail_from_hub(repos)
            return ToolOut(False, message=m, failure=k)
        attention: list[dict[str, Any]] = []
        facts: list[dict[str, Any]] = []
        commits = 0
        seen = 0
        for r in repos.data[:4]:
            name = r["source_id"]
            act = self._hub("get_repository_activity", {"repo": name, "days": a.days})
            if not act.success:
                continue
            seen += 1
            commits += int(act.metadata.get("commits", 0))
            for it in act.data:
                if it.get("kind") in ("issue", "pull_request"):
                    attention.append({"repo": name, "kind": it["kind"], "title": sanitize_external(str(it.get("title", "")), 100), "source_id": it.get("source_id"), "ts": it.get("source_timestamp"),
                                      "why": f"an open {'pull request' if it['kind'] == 'pull_request' else 'issue'} updated in the last {a.days} days"})
                    if it.get("source_timestamp"):
                        label = "pull request" if it["kind"] == "pull_request" else "issue"
                        facts.append(Fact("github_item", str(it["source_timestamp"]), f"Review {label} in {name}: {sanitize_external(str(it.get('title', '')), 90)}", FactStatus.VERIFIED, "github",
                                          f"{name}#{it.get('source_id', '')}", str(it["source_timestamp"]), "", "high", [f"read from the GitHub account {login}"]).to_dict())
        return ToolOut(True, {"login": login, "repos": [r["source_id"] for r in repos.data[:4]], "attention": attention, "facts": facts[:5], "commits": commits, "count": len(attention)},
                       f"{len(attention)} item{'s' if len(attention) != 1 else ''} in your repositories need attention; {commits} commit{'s' if commits != 1 else ''} this period.", untrusted=True)

    # ---- Documents / memory ---------------------------------------------------------------------------------------------------------------------

    def h_docs_search(self, a: SearchA, wf, board) -> ToolOut:
        r = self._hub("search_documents", {"query": a.query, "limit": min(a.limit, 10)})
        if not r.success:
            k, m = _fail_from_hub(r)
            return ToolOut(False, message=m, failure=k)
        docs = [{"id": d.get("source_id"), "title": sanitize_external(str(d.get("title", "")), 100), "page": (d.get("metadata") or {}).get("page"), "score": (d.get("metadata") or {}).get("score")} for d in r.data]
        if not docs:
            return ToolOut(False, message="I couldn't find that information in your authorized documents.", failure=FailureKind.DATA_MISSING)
        return ToolOut(True, {"documents": docs, "count": len(docs)}, f"Found {len(docs)} matching document{'s' if len(docs) != 1 else ''}.", untrusted=True)

    def h_docs_requirements(self, a: SearchA, wf, board) -> ToolOut:
        r = self._hub("search_documents", {"query": a.query, "limit": 5})
        if not r.success:
            k, m = _fail_from_hub(r)
            return ToolOut(False, message=m, failure=k)
        reqs: list[dict[str, Any]] = []
        warnings: list[str] = []
        for d in r.data[:3]:
            doc = self._hub("read_document", {"document_id": str(d.get("source_id"))})
            if not doc.success:
                continue
            text = str((doc.data or {}).get("summary", "")) + "\n" + "\n".join(str(v) for k, v in ((doc.data or {}).get("metadata") or {}).items() if k in ("text", "excerpt"))
            title = sanitize_external(str(d.get("title", "")), 80)
            in_block = False
            for line in text.splitlines():
                if _REQ_HEAD.search(line):
                    in_block = True
                    m = re.search(r":\s*(.+)$", line)
                    if m and not _REQ_LINE.match(line):
                        for piece in re.split(r",|;| and ", m.group(1)):
                            if 2 < len(piece.strip()) < 80:
                                reqs.append({"text": sanitize_external(piece.strip(" ."), 80), "document": title, "source_id": d.get("source_id"), "page": (d.get("metadata") or {}).get("page")})
                    continue
                lm = _REQ_LINE.match(line)
                if in_block and lm:
                    txt = sanitize_external(lm.group(1), 100)
                    if scan_for_injection(txt).flagged:
                        warnings.append("One line in a document looked like instructions to an assistant; I ignored it.")
                        continue
                    reqs.append({"text": txt, "document": title, "source_id": d.get("source_id"), "page": (d.get("metadata") or {}).get("page")})
                elif in_block and line.strip() == "":
                    in_block = False
        if not reqs:
            return ToolOut(False, message="I found documents, but none of them lists required documents.", failure=FailureKind.DATA_MISSING)
        return ToolOut(True, {"requirements": reqs[:15], "count": len(reqs[:15])}, f"Found {len(reqs[:15])} requirement{'s' if len(reqs[:15]) != 1 else ''}.", warnings=warnings[:2], untrusted=True)

    def h_docs_deadlines(self, a: SearchA, wf, board) -> ToolOut:
        from agent.intelligence.extraction import TextExtractor
        from agent.intelligence.models import SourceKind

        r = self._hub("search_documents", {"query": a.query, "limit": 5})
        if not r.success:
            k, m = _fail_from_hub(r)
            return ToolOut(False, message=m, failure=k)
        extractor = TextExtractor(self.ctx.zone, self.ctx.clock)
        facts: list[Fact] = []
        for d in r.data[:3]:
            doc_id = str((d.get("metadata") or {}).get("document_id") or d.get("external_id") or d.get("source_id"))
            doc = self._hub("read_document", {"document_id": doc_id})
            if not doc.success:
                continue
            text = str((doc.data or {}).get("summary", ""))
            title = sanitize_external(str(d.get("title", "")), 80)
            out = extractor.extract(text, source_type=SourceKind.DOCUMENT, source_id=doc_id, label=title, source_timestamp=None, subject=title)
            for c in out.commitments:
                if c.when is None or c.kind.value == "event" and False:
                    continue
                item = {"source_timestamp": c.when.isoformat(), "title": c.title, "summary": c.evidence, "confidence": getattr(c.confidence, "name", str(c.confidence)).lower(), "source_type": "documents",
                        "metadata": {"evidence": c.evidence, "message_id": doc_id, "injection_suspected": c.flagged}, "retrieved_at": None}
                f = factlib.classify_deadline(item, message_flagged=out.flagged)
                if f is not None:
                    f.notes.append(f"from the document '{title}'" + (f", page {(d.get('metadata') or {}).get('page')}" if (d.get("metadata") or {}).get("page") else ""))
                    facts.append(f)
        if not facts:
            return ToolOut(False, message="I found documents, but none of them states a date.", failure=FailureKind.DATA_MISSING)
        return ToolOut(True, {"facts": [f.to_dict() for f in facts[:10]], "count": len(facts[:10])}, f"Found {len(facts[:10])} date{'s' if len(facts[:10]) != 1 else ''} in your documents.", untrusted=True)

    def h_memory(self, a: SearchA, wf, board) -> ToolOut:
        found = self.ctx.memory.search(a.query, limit=5)
        rows = [{"id": getattr(m, "memory_id", ""), "content": sanitize_external(str(getattr(m, "content", "")), 160)} for m in found]
        return ToolOut(True, {"memories": rows, "count": len(rows), "context_only": True}, f"{len(rows)} related memor{'ies' if len(rows) != 1 else 'y'} (context only).", untrusted=True)

    # ---- links ------------------------------------------------------------------------------------------------------------------------------------

    def h_links(self, a: LinksA, wf, board) -> ToolOut:
        links: list[dict[str, Any]] = []
        for e in a.emails[:5]:
            mid = str(e.get("id", ""))
            if not re.match(_ID, mid):
                continue
            r = self._hub("read_email", {"message_id": mid})
            if not r.success:
                continue
            body = str(r.data.get("body_untrusted", ""))
            for m in _URL_RE.finditer(body):
                url = m.group(0).rstrip(".,;:!?")
                d = validate_url(url, allow_private=False, resolver=None)
                if not d.ok:
                    continue
                around = body[max(0, m.start() - 60): m.end() + 20]
                links.append({"url": d.url, "host": d.host, "hint": bool(_LINK_HINT.search(around) or _LINK_HINT.search(url)), "suspicious": d.suspicious or None, "message_id": mid})
        links.sort(key=lambda x: (not x["hint"], bool(x["suspicious"])))
        if not links:
            return ToolOut(False, message="I didn't find a link in that email.", failure=FailureKind.DATA_MISSING)
        best = [x for x in links if x["hint"]] or links
        if len({x["url"] for x in best}) > 1 and len({x["host"] for x in best}) > 1:
            choices = [{"n": i + 1, "name": x["host"], "url": x["url"]} for i, x in enumerate(best[:4])]
            return ToolOut(False, {"links": links[:8]}, "I found several links: " + "; ".join(f"{c['n']}, {c['name']}" for c in choices) + ". Which one?", FailureKind.AMBIGUOUS, choices=choices)
        top = best[0]
        warn = [top["suspicious"]] if top["suspicious"] else []
        return ToolOut(True, {"url": top["url"], "links": links[:8], "host": top["host"]}, f"Found a link on {top['host']}.", warnings=warn, untrusted=True)

    # ---- briefing / notify / report --------------------------------------------------------------------------------------------------------------

    def h_briefing(self, a: BriefingA, wf, board) -> ToolOut:
        zone = self.ctx.zone
        today = self._now().astimezone(zone).date()
        meetings = [{"id": e.get("id"), "title": e.get("title", ""), "start": factlib.date_of(e.get("start") or "")} for e in a.events if not e.get("all_day")]
        tasks = [{"id": t.get("id"), "title": t.get("title", ""), "due": factlib.date_of(t.get("due") or ""), "priority": t.get("priority", 2)} for t in a.tasks]
        deadlines = [{"title": d.get("title", ""), "due": factlib.date_of(d.get("due") or ""), "source": d.get("source", ""), "source_id": d.get("source_id", ""), "status": d.get("status")} for d in a.deadlines]
        notes: list[dict[str, Any]] = []
        if self.ctx.center is not None:
            try:
                notes = [{"id": n.notification_id, "title": n.title, "level": n.level, "acknowledged": bool(n.acknowledged_at)} for n in self.ctx.center.history(20)]
            except Exception:  # noqa: BLE001
                notes = []
        have = {t.get("id") for t in a.tasks}
        deadlines = [d for d in deadlines if not (d["source"] == "tasks" and d["source_id"] in have)]       # a task's own due date is not a second deadline
        items = prio.synthesize(today=today, zone=zone, meetings=meetings, tasks=tasks, deadlines=deadlines, emails=a.emails, notifications=notes)
        missing = list(board.get("_missing", []))
        if missing and not board.get("_reached"):
            return ToolOut(False, message="I couldn't reach " + " or ".join(missing) + ", so I have nothing to put in a briefing.", failure=FailureKind.EXTERNAL_SERVICE)
        text = prio.spoken_summary(items, missing=missing)
        return ToolOut(True, {"items": [i.to_dict() for i in items[:15]], "count": len(items), "text": text}, text, untrusted=False)

    def h_notify(self, a: NotifyA, wf, board) -> ToolOut:
        c = self.ctx
        key = (a.key or a.text)[:80]
        now = time.monotonic()
        if key in c.notified and now - c.notified[key] < 6 * 3600:
            return ToolOut(True, {"count": 0}, "I already told you about that recently, so I didn't repeat it.", reused=True)
        if c.notify is not None:
            c.notify(a.text, a.priority)
        c.notified[key] = now
        return ToolOut(True, {"count": 1}, "Told you.")

    def h_report(self, a: ReportA, wf, board) -> ToolOut:
        from workflows.templates import compose

        text = compose(a.kind, wf, board, self.ctx.zone)
        return ToolOut(True, {"text": text}, text)
