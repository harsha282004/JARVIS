"""The workflow planner: a request -> a validated Workflow (or a question, or an honest refusal). Deterministic first: no model is needed for any template.

    grammar (offline) picks a template and its parameters  ->  availability detection (which systems are connected)  ->  scope from the request
    ->  validate (tools exist, argument names, references only to declared dependencies, acyclic, limits)  ->  risk and permission computed by code

A model may *propose* a plan (`from_proposal`), but that JSON goes through exactly the same validation; the worst a hostile proposal can do is be refused. Text that came
from an email, page, document or issue is never planned from: the planner only ever sees the user's own words.
"""

import re
from dataclasses import dataclass, field
from typing import Any

from autonomy.models import RetryPolicy, Risk
from backend.core.metrics import metrics
from workflows.models import From, WStep, Workflow, WStatus
from workflows.templates import SYSTEM_LABEL, TEMPLATES, TemplateDef
from workflows.tools import ALL_TOOL_NAMES, FORBIDDEN_NAMES, OperatorRouter, SPECS

_NUM = {"a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "ten": 10, "the": 1}
_STOP_TOPIC = frozenset({"it", "them", "that", "deadline", "deadlines", "the", "a", "an", "my", "that", "this", "important", "latest", "new", "recent", "any", "unread", "related", "relevant", "please", "all", "our", "your", "those", "these", "some"})

_SEND = re.compile(r"\b(?:send|forward|reply|respond|email|mail|message|text|dm)\b.{0,40}\b(?:to|back to)\b.{0,60}(?:@|\bthem\b|\bhim\b|\bher\b|\beveryone\b|\ball\b)|"
                   r"\b(?:reply|respond)\s+to\b.{0,40}\b(?:e-?mails?|messages?)\b|\bforward\b.{0,30}\b(?:e-?mails?|messages?)\b|\bsend\b.{0,20}\b(?:an? )?(?:e-?mail|message)\b", re.I)
_BRIEF = re.compile(r"\b(?:(?:morning|daily|day'?s?) briefing|brief me|(?:give|prepare|make|get)(?: me)?(?: my| a| the)? (?:morning )?briefing|what(?:'s| is) on (?:for )?today|"
                    r"what do i need to (?:know|do|handle) today)\b", re.I)
_EMAIL_WORD = re.compile(r"\be-?mails?\b|\bmail\b|\binbox\b", re.I)
_TASK_WORD = re.compile(r"\b(?:tasks?|to-?dos?|task list|to-do list)\b", re.I)
_REMIND_WORD = re.compile(r"\bremind(?:er)?s?\b", re.I)
_DEADLINE_WORD = re.compile(r"\b(?:deadlines?|due (?:date|on|by)?|closing date|last date|due)\b", re.I)
_CAL_WORD = re.compile(r"\b(?:calendar|meetings?|schedule|events?|appointments?)\b", re.I)
_GH_WORD = re.compile(r"\b(?:github|git hub)\b", re.I)
_DOC_WORD = re.compile(r"\b(?:documents?|pdfs?|files?|notes)\b", re.I)
_WEB_WORD = re.compile(r"\b(?:link|website|web ?page|page|form|portal|application form)\b", re.I)
_DAYS_BEFORE = re.compile(r"\b(?:(?P<n>\d+|a|an|one|two|three|four|five|six|seven|ten)\s+(?P<u>days?|weeks?)|(?P<the>the day|a day))\s+(?:before|earlier|ahead|prior)", re.I)
_EMAIL_TOPIC = [   # most specific first
    re.compile(r"\b(?:from|in|of)\s+(?:the|my)\s+(?P<t>[a-z0-9&+\- ]{2,40}?)\s+(?:e-?mails?|mails?)\b", re.I),
    re.compile(r"\b(?:e-?mails?|mails?)\s+(?:about|regarding|re:?|on|from|concerning)\s+(?:the |my |a |an )?(?P<t>[a-z0-9&+\-' ]{2,40}?)(?=[,.;]|\s+(?:and|then|to|for|so|that|in|with)\b|$)", re.I),
    re.compile(r"\b(?:find|check|look (?:for|up|at)|search (?:for)?|get|open|read|see|pull up|locate)\s+(?:for\s+)?(?:the|my|that|this|an?|any)?\s*(?P<t>[a-z0-9&+\- ]{2,40}?)\s+(?:e-?mails?|mails?)\b", re.I),
    re.compile(r"\b(?:the |my )?(?P<t>(?!(?:the|that|this|my|a|an|its|their|your|upcoming|next)\b)[a-z0-9&+\-]{3,25}) (?:deadline|due date)\b", re.I),
    re.compile(r"\bappl(?:y|ication)\s+(?:for|to)\s+(?:the |my |a |an )?(?P<t>[a-z0-9&+\- ]{2,40}?)(?=\s+(?:from|in|using|via|through|with)\b|[,.;]|$)", re.I),
]
_PREP = re.compile(r"\b(?:prepare|prep|get me ready|get ready)\b.{0,30}\b(?:for|meetings?)\b|\b(?:calendar|meetings?|schedule)\b.{0,50}\b(?:related|and)\b.{0,20}\be-?mails?\b", re.I)
_REVIEW_WEEK = re.compile(r"\b(?:weekly review|review (?:my|the) week|plan (?:my|the) week|how does (?:my|the|this) week look|what(?:'s| is) (?:on )?this week)\b", re.I)
_CONTROL = re.compile(r"^\s*(?:(?:please )?(?:stop|cancel|never ?mind|abort|forget it)(?: (?:this|that|it|the (?:workflow|task|operation)|everything))?|cancel the workflow|stop the workflow)\s*[.!]*\s*$", re.I)

MAX_TOPIC = 60


@dataclass
class PlanOutcome:
    kind: str                      # plan | ask | refuse | unavailable | none
    workflow: Workflow | None = None
    message: str = ""
    missing: str = ""
    params: dict[str, Any] = field(default_factory=dict)


@dataclass
class Limits:
    max_steps: int = 14
    max_systems: int = 5
    max_tool_calls: int = 30
    max_duration_s: float = 240.0


def is_control(text: str) -> bool:
    return bool(_CONTROL.match(text))


def clean_topic(raw: str) -> str:
    words = [w for w in re.findall(r"[A-Za-z0-9&+\-']+", raw) if w.lower() not in _STOP_TOPIC]
    return " ".join(words)[:MAX_TOPIC].strip()


def extract_topic(text: str) -> str:
    for pat in _EMAIL_TOPIC:
        m = pat.search(text)
        if m:
            t = clean_topic(m.group("t"))
            if t and t.lower() not in ("email", "mail", "emails"):
                return t
    return ""


def days_before(text: str) -> int | None:
    m = _DAYS_BEFORE.search(text)
    if not m:
        return None
    if m.group("the"):
        return 1
    raw = m.group("n").lower()
    n = int(raw) if raw.isdigit() else _NUM.get(raw, 1)
    return min(60, n * (7 if m.group("u").lower().startswith("week") else 1))


def systems_mentioned(text: str) -> set[str]:
    out = set()
    for name, rx in (("gmail", _EMAIL_WORD), ("calendar", _CAL_WORD), ("tasks", _TASK_WORD), ("reminders", _REMIND_WORD), ("github", _GH_WORD), ("documents", _DOC_WORD), ("browser", _WEB_WORD)):
        if rx.search(text):
            out.add(name)
    return out


class WorkflowPlanner:
    def __init__(self, router: OperatorRouter, limits: Limits | None = None):
        self.router = router
        self.limits = limits or Limits()

    # ---- availability ----------------------------------------------------------------------------------------------------------------

    _PROBE = {"gmail": "gmail_search", "calendar": "calendar_events", "github": "github_identity", "documents": "documents_search", "tasks": "task_create", "reminders": "reminder_create",
              "browser": "open_url", "memory": "memory_context"}

    def availability(self) -> dict[str, tuple[bool, str]]:
        return {s: self.router.available(t) for s, t in self._PROBE.items()}

    # ---- entry ---------------------------------------------------------------------------------------------------------------------------

    def plan(self, text: str, session_id: str, *, last_fact: dict[str, Any] | None = None, requested_by: str = "user") -> PlanOutcome:
        """PlanOutcome.kind == "none" means: this is not a workflow request (let the other routers see it)."""
        began = __import__("time").perf_counter()
        try:
            return self._plan(text.strip(), session_id, last_fact, requested_by)
        finally:
            metrics.observe("workflow.planning_ms", (__import__("time").perf_counter() - began) * 1000)

    def _match(self, text: str, last_fact: dict[str, Any] | None) -> tuple[str, dict[str, Any]] | PlanOutcome | None:
        t = text.lower()
        sys_ = systems_mentioned(t)
        # sending anything on the user's behalf is never planned: there is no such tool. (Only refused when the request is workflow-shaped.)
        if _SEND.search(t) and (sys_ - {"gmail"} or re.search(r"\b(?:then|and|all|every|each)\b", t)):
            return PlanOutcome("refuse", message="I can read and summarize your email and set up tasks and reminders, but I don't send, reply to or forward email or messages on my own.")
        notify = bool(re.search(r"\b(?:notify me|send me a notification|notification|announce it|let me know by notification)\b", t))
        if _REVIEW_WEEK.search(t):
            return "weekly_review", {}
        if _CAL_WORD.search(t) and _EMAIL_WORD.search(t) and _PREP.search(t):        # "check tomorrow's calendar and related emails, then prepare my briefing"
            return "meeting_preparation", {"day": "tomorrow" if "tomorrow" in t else "today", "notify": notify}
        if _BRIEF.search(t):
            return "daily_briefing", {"notify": notify}
        topic = extract_topic(text)
        db = days_before(t)
        wants_task, wants_rem = bool(_TASK_WORD.search(t)) or bool(re.search(r"\b(?:add|put|create|make)\b.{0,30}\b(?:to-?do|task)\b", t)), bool(_REMIND_WORD.search(t))
        # follow-up on the previous fact ("turn that into a reminder", "remind me two days before that")
        if last_fact is not None and re.search(r"\b(?:that|it|the deadline)\b", t) and (wants_rem or wants_task) and not topic and not _EMAIL_WORD.search(t) and not (sys_ & {"github", "documents", "calendar"}):
            if wants_task and wants_rem:
                name = "fact_task_and_reminder"
            else:
                name = "fact_to_reminder" if wants_rem else "fact_to_task"
            return name, {"fact": last_fact, "days_before": db if db is not None else 0}
        # application from an email: prepare, stop before submitting
        if _EMAIL_WORD.search(t) and re.search(r"\b(?:apply|fill (?:in|out)|complete|register|submit)\b", t) and re.search(r"\b(?:appl(?:y|ication)|form|register)\b", t) and topic:
            return "application_submit", {"topic": topic}
        if _EMAIL_WORD.search(t) and _WEB_WORD.search(t) and re.search(r"\b(?:open|go to|visit|follow|click)\b", t) and topic:
            return "email_to_browser", {"topic": topic}
        if _EMAIL_WORD.search(t) and re.search(r"\b(?:important|urgent|priority)\b", t) and re.search(r"\b(?:deadlines?)\b", t) and wants_task and not topic:
            return "email_deadlines_to_tasks", {}
        # deadline in an email -> task / reminder
        if _EMAIL_WORD.search(t) and (_DEADLINE_WORD.search(t) or wants_rem or wants_task) and (wants_task or wants_rem):
            if not topic:
                return PlanOutcome("ask", message="Which email should I look at? For example, say the topic, like 'the internship email'.", missing="topic")
            p = {"topic": topic, "days_before": db if db is not None else 0}
            if wants_task and wants_rem:
                return "deadline_task_and_reminder", p
            return ("deadline_to_reminder" if wants_rem else "deadline_to_task"), p
        if re.search(r"\b(?:important|urgent|priority)\b", t) and _EMAIL_WORD.search(t) and re.search(r"\b(?:attention|what (?:i|do i) need|tell me|summari[sz]e|action|handle)\b", t):
            return "important_email_review", {}
        if _CAL_WORD.search(t) and _EMAIL_WORD.search(t) and _PREP.search(t):
            day = "tomorrow" if "tomorrow" in t else "today"
            return "meeting_preparation", {"day": day, "notify": notify}
        if _PREP.search(t) and re.search(r"\b(?:tomorrow|today)\b", t) and re.search(r"\b(?:meetings?|calendar|schedule)\b", t):
            return "meeting_preparation", {"day": "tomorrow" if "tomorrow" in t else "today", "notify": notify}
        if _GH_WORD.search(t) and re.search(r"\b(?:activity|work|commits?|issues|pull requests?|prs?)\b", t) and (wants_task or re.search(r"\b(?:this|last|past) (?:week|month)\b|\bimportant\b", t)) and (wants_task or "week" in t):
            return "github_activity_review", {"days": 30 if "month" in t else 7, "add_tasks": bool(re.search(r"\b(?:add|put|create|turn|make)\b", t) and wants_task)}
        if _DEADLINE_WORD.search(t) and re.search(r"\b(?:upcoming|coming up|this week|next week|finish|need to|due soon|what)\b", t) and not _EMAIL_WORD.search(t) and not _DOC_WORD.search(t) and (
                re.search(r"\b(?:check|show|tell|list|what|review)\b", t)):
            return "deadlines_review", {"days": 14 if "next week" in t or "two weeks" in t else 7}
        if _DEADLINE_WORD.search(t) and re.search(r"\b(?:documents?|pdfs?|files?)\b", t) and re.search(r"\b(?:find|check|what|search|look)\b", t):
            dtopic = ""
            for pat in (r"\b(?:about|regarding|concerning) (?:my |the )?(?P<t>[a-z0-9&+\- ]{2,40}?)(?: in (?:my |the )?(?:documents?|pdfs?|files?))?(?:\s+and\b.*)?[.?!]*$",
                        r"\b(?:the |my )?(?P<t>[a-z0-9&+\-]{3,25}) (?:deadline|due date)\b", r"\b(?:in|from) (?:my |the )?(?P<t>[a-z0-9&+\- ]{2,40}?) (?:documents?|pdfs?|files?)\b"):
                m = re.search(pat, t)
                dtopic = clean_topic(m.group("t")) if m else ""
                if dtopic:
                    break
            dtopic = dtopic or clean_topic(re.sub(r"\b(?:find|check|what|is|are|the|deadlines?|due|dates?|in|my|documents?|files?|pdfs?|and|create|a|task|for|it)\b", " ", t))
            if not dtopic:
                return PlanOutcome("ask", message="Which document or topic should I look in?", missing="topic")
            return "document_deadline_review", {"topic": dtopic, "add_task": True if wants_task else None}
        return None

    def _plan(self, text: str, session_id: str, last_fact: dict[str, Any] | None, requested_by: str) -> PlanOutcome:
        matched = self._match(text, last_fact)
        if matched is None:
            return PlanOutcome("none")
        if isinstance(matched, PlanOutcome):
            return matched
        name, params = matched
        if name in ("fact_to_reminder", "fact_to_task", "fact_task_and_reminder"):
            return self._fact_followup(name, params, text, session_id, requested_by)
        tdef = TEMPLATES[name]
        avail = self.availability()
        missing = [s for s in tdef.systems if not avail.get(s, (False, ""))[0]]
        if missing:
            s = missing[0]
            reason = avail[s][1] or f"{SYSTEM_LABEL[s]} isn't available."
            if name == "daily_briefing":
                return PlanOutcome("none")
            return PlanOutcome("unavailable", message=f"I can't do that yet: {reason.rstrip('.')}.", missing=s, params=params)
        available = {s for s, (ok, _) in avail.items() if ok}
        params = {**params, "available": available}
        steps = tdef.build(params)
        wf = Workflow(goal=text[:300], template=name, steps=steps, session_id=session_id, requested_by=requested_by)
        wf.params = {k: v for k, v in params.items() if k != "available"}
        wf.params["unavailable"] = sorted(s for s in tdef.optional_systems if s not in available)
        wf.scope = {s.source for s in steps if s.source and s.source != "local"}
        skipped = [s for s in tdef.optional_systems if s not in available]
        if skipped and name != "daily_briefing":
            wf.warnings.append("I can't check " + " or ".join(SYSTEM_LABEL[s] for s in skipped) + " right now, so that isn't included.")
        problem = self.validate(wf)
        if problem:
            return PlanOutcome("refuse", message=problem)
        self._finish(wf, tdef)
        return PlanOutcome("plan", wf)

    def _fact_followup(self, name: str, params: dict[str, Any], text: str, session_id: str, requested_by: str) -> PlanOutcome:
        avail = self.availability()
        need = {"fact_to_task": {"tasks"}, "fact_to_reminder": {"reminders"}, "fact_task_and_reminder": {"tasks", "reminders"}}[name]
        for s in need:
            if not avail[s][0]:
                return PlanOutcome("unavailable", message=f"I can't do that: {avail[s][1].rstrip('.')}.", missing=s)
        fact = params["fact"]
        steps: list[WStep] = []
        if name in ("fact_to_task", "fact_task_and_reminder"):
            steps.append(WStep("s1", "Create the task", "task_create", {"fact": fact}, [], "a task with the deadline as its due date", ["readback"], False, "tasks"))
        if name in ("fact_to_reminder", "fact_task_and_reminder"):
            n = len(steps)
            steps.append(WStep(f"s{n + 1}", "Work out the reminder time", "compute_reminder_time", {"fact": fact, "days_before": int(params["days_before"]), "hour": 9}, [], "a future time", ["has:remind_at"], False, "local"))
            steps.append(WStep(f"s{n + 2}", "Schedule the reminder", "reminder_create", {"at": From(f"s{n + 1}", "remind_at"), "fact": fact}, [f"s{n + 1}"], "a scheduled reminder", ["readback"], False, "reminders"))
        wf = Workflow(goal=text[:300], template=name, steps=steps, session_id=session_id, requested_by=requested_by)
        wf.scope = {s.source for s in steps if s.source != "local"}
        problem = self.validate(wf)
        if problem:
            return PlanOutcome("refuse", message=problem)
        self._finish(wf, None)
        return PlanOutcome("plan", wf)

    # ---- validation and risk (by code) ---------------------------------------------------------------------------------------------------

    def validate(self, wf: Workflow) -> str | None:
        """None if the plan may run; else a spoken reason. Never trusts a proposer's risk, permission or scope."""
        if not wf.steps:
            return "I don't have any steps for that."
        if len(wf.steps) > self.limits.max_steps:
            return "That's too many steps for one workflow, so I didn't start it."
        ids = [s.step_id for s in wf.steps]
        if len(set(ids)) != len(ids):
            return "That plan has duplicate steps, so I refused it."
        systems = {s.source for s in wf.steps if s.source not in ("local", "")}
        if len(systems) > self.limits.max_systems:
            return "That touches too many of your accounts at once, so I didn't start it."
        seen: set[str] = set()
        for s in wf.steps:
            if s.tool in FORBIDDEN_NAMES or s.tool not in ALL_TOOL_NAMES:
                return "That plan uses an action I don't have, so I refused it."
            problem = self.router.validate_shape(s.tool, s.arguments)
            if problem:
                return f"That plan isn't valid ({problem}), so I refused it."
            for dep in s.dependencies:
                if dep not in seen:                           # only earlier steps: this also makes the graph acyclic
                    return "That plan has a step that depends on a later or unknown step, so I refused it."
            for v in _refs(s.arguments):
                if v.step not in s.dependencies:
                    return "That plan reads a result it doesn't depend on, so I refused it."
            if s.source and s.source not in ("local", "browser") and self.router.system_of(s.tool) != s.source:
                return "That plan labels a step with the wrong system, so I refused it."
            if s.source and s.source not in wf.scope and s.source != "local":
                return "That plan reaches outside what you asked for, so I refused it."
            seen.add(s.step_id)
        return None

    def _finish(self, wf: Workflow, tdef: TemplateDef | None) -> None:
        risk = Risk.READ_ONLY
        for s in wf.steps:
            args = {k: v for k, v in s.arguments.items()}
            s.risk, s.permission = self.router.risk_of(s.tool, args, s.description)
            s.side_effect = self.router.side_effect(s.tool) or s.risk >= Risk.EXTERNAL_EFFECT
            s.retry_policy = RetryPolicy(max_retries=2 if self.router.retry_safe(s.tool) else 0, safe=self.router.retry_safe(s.tool))
            risk = max(risk, s.risk)
        wf.risk_level = risk
        wf.status = WStatus.READY
        wf.ack = f"On it: {tdef.description[0].lower() + tdef.description[1:]}." if tdef else "On it."
        wf.preview = self.preview(wf)

    def preview(self, wf: Workflow) -> str:
        acts = [f"{i + 1}. {s.description}" for i, s in enumerate(wf.steps) if s.tool != "compose_workflow_report"]
        gated = [s.description.lower() for s in wf.steps if s.risk.needs_confirmation]
        text = "Plan: " + "; ".join(acts) + "."
        if gated:
            text += " I'll ask you before I " + " and ".join(gated) + "."
        return text

    # ---- model proposals: same validation, nothing more --------------------------------------------------------------------------------------

    def from_proposal(self, goal: str, proposal: dict[str, Any], session_id: str) -> PlanOutcome:
        """A plan proposed by a model: {"steps": [{"id","description","tool","arguments","depends_on","optional"}]}. Arguments may reference earlier results as
        {"$from": "s1.emails"}. The proposal is data: unknown tools, extra argument names, references to undeclared dependencies, out-of-scope systems and
        anything forbidden are refused here, and risk/permission are recomputed."""
        try:
            raw = proposal["steps"]
            if not isinstance(raw, list):
                raise TypeError
            steps: list[WStep] = []
            for i, d in enumerate(raw[: self.limits.max_steps + 1]):
                args = {k: _parse_ref(v) for k, v in dict(d.get("arguments") or {}).items()}
                tool = str(d["tool"])
                steps.append(WStep(str(d.get("id") or f"s{i + 1}"), str(d.get("description", tool))[:120], tool, args, [str(x) for x in d.get("depends_on", [])], "", [], bool(d.get("optional", False)),
                                   self.router.system_of(tool) if self.router.known(tool) else ""))
        except (KeyError, TypeError, ValueError, AttributeError):
            return PlanOutcome("refuse", message="I couldn't turn that into a safe plan.")
        wf = Workflow(goal=goal[:300], template="proposed", steps=steps, session_id=session_id)
        wf.scope = {s.source for s in steps if s.source not in ("", "local")}
        avail = self.availability()
        for s in steps:
            if s.tool in ALL_TOOL_NAMES and not self.router.available(s.tool)[0]:
                return PlanOutcome("unavailable", message=f"I can't do that: {self.router.available(s.tool)[1].rstrip('.')}.")
        problem = self.validate(wf)
        if problem:
            return PlanOutcome("refuse", message=problem)
        if any(SPECS[s.tool].side_effect for s in steps if s.tool in SPECS):
            wf.warnings.append("This plan writes tasks or reminders; each write is checked and deduplicated.")
        self._finish(wf, None)
        return PlanOutcome("plan", wf)


def _parse_ref(v: Any) -> Any:
    if isinstance(v, dict) and set(v) == {"$from"} and isinstance(v["$from"], str):
        step, _, path = v["$from"].partition(".")
        if not re.fullmatch(r"s\d{1,2}", step) or not re.fullmatch(r"[a-z_.]{0,40}", path):
            raise ValueError("bad reference")
        return From(step, path)
    if isinstance(v, dict) and any(str(k).startswith("$") for k in v):
        raise ValueError("bad reference")
    return v


def _refs(arguments: dict[str, Any]) -> list[From]:
    out: list[From] = []
    for v in arguments.values():
        while isinstance(v, From):
            out.append(v)
            v = v.fallback
    return out
