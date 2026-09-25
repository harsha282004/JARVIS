"""The Personal Operator: owns workflows (start, answer, pause, resume, cancel, confirm, recover) and their conversational front door.

    request -> WorkflowPlanner (deterministic grammar; model proposals only through the same validation) -> Workflow -> WorkflowRunner thread
            -> OperatorRouter -> operator tools / browser tools (Phase 21 ToolRouter -> BrowserTools -> PermissionManager) -> verification -> result

The operator never calls an integration itself. It keeps a small amount of conversational context (the last extracted fact for "turn that into a reminder", the last
briefing for "tell me more") that is used only to *plan*, never to skip a check. Workflows recovered after a restart come back PAUSED with their external state
verified by the tools' effect ledger; nothing resumes by itself.
"""

import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from agent.intelligence.confirmation import ActionReport
from autonomy.models import Risk
from backend.core.logging import get_logger
from backend.core.metrics import metrics
from backend.core.redaction import redact
from backend.core.security.approval import ApprovalClass
from workflows import facts as factlib
from workflows.models import Fact, SStatus, WStatus, Workflow, FailureKind
from workflows.planner import WorkflowPlanner, is_control, systems_mentioned
from workflows.runner import OperatorConfig, WorkflowRunner
from workflows.store import WorkflowStore
from workflows.templates import TEMPLATES
from workflows.tools import OperatorRouter

logger = get_logger(__name__)

INLINE_WAIT_S = 6.0
_PAUSE = re.compile(r"^(?:pause|hold)(?: (?:the |that |this )?(?:workflow|job|work))$|^pause the workflow$", re.I)
_RESUME = re.compile(r"^(?:resume|continue|carry on|go on|keep going|try again|retry)(?: (?:the |that |this )?(?:workflow|job|work))?$", re.I)
_STATUS = re.compile(r"^(?:what(?:'s| is) the (?:workflow )?status|how(?:'s| is) (?:the |that )?workflow going|what workflow(?:s)? (?:is|are) (?:running|active)|are you (?:still )?working on (?:it|that|the workflow))$", re.I)
_MORE = re.compile(r"^(?:tell me more|more (?:detail|details)|why(?: is| are)? (?:that|those|it|these)(?: on (?:the|my) list)?|why those|explain(?: that| it)?|give me (?:more )?details?)$", re.I)
_WHY_FAILED = re.compile(r"^why did (?:that|it|the workflow) (?:fail|stop)|^what went wrong with (?:that|the workflow)$", re.I)
_UNFINISHED = re.compile(r"^(?:what|which) workflows? (?:are|were) (?:unfinished|incomplete|left|pending)|^any (?:unfinished|incomplete) workflows?", re.I)
_SOURCES = frozenset({"gmail", "calendar", "github", "documents", "browser"})
_CONNECTOR = re.compile(r"\b(?:then|and then|after that|afterwards)\b|,\s*and\s+(?:create|add|set|remind|prepare|tell|open|check)\b", re.I)
_FRESH_S = 30 * 60


@dataclass
class Reply:
    text: str
    workflow: Workflow | None = None


class PersonalOperator:
    def __init__(self, planner: WorkflowPlanner, router: OperatorRouter, store: WorkflowStore, config: OperatorConfig, *, confirmations=None, announce: Callable[[str, str], None] | None = None,
                 browser_stop: Callable[[], None] | None = None, llm_available: Callable[[], bool] | None = None, clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep, threaded: bool = True, inline_wait_s: float = INLINE_WAIT_S):
        self.planner, self.router, self.store, self.cfg = planner, router, store, config
        self._confirmations, self._announce, self._browser_stop = confirmations, announce, browser_stop
        self._llm_available = llm_available
        self._clock, self._sleep, self._threaded, self._inline_wait = clock, sleep, threaded, inline_wait_s
        self._lock = threading.RLock()
        self._start_lock = threading.RLock()
        self._workflows: dict[str, Workflow] = {}
        self._runners: dict[str, WorkflowRunner] = {}
        self._threads: dict[str, threading.Thread] = {}
        self._approved: dict[str, bool] = {}
        self._delivered: set[str] = set()
        self.last_fact: dict[str, Any] | None = None
        self.last_briefing: tuple[float, list[dict[str, Any]], str] | None = None
        self.recovered: list[str] = []

    # ---- observation ----------------------------------------------------------------------------------------------------------------------

    def get(self, workflow_id: str) -> Workflow | None:
        with self._lock:
            return self._workflows.get(workflow_id)

    def active(self) -> list[Workflow]:
        with self._lock:
            return [w for w in self._workflows.values() if not w.terminal]

    def running_count(self) -> int:
        with self._lock:
            return sum(1 for w in self._workflows.values() if w.status in (WStatus.RUNNING, WStatus.WAITING_FOR_CONFIRMATION, WStatus.PLANNING, WStatus.READY))

    def current(self) -> Workflow | None:
        act = self.active()
        return max(act, key=lambda w: w.updated_at) if act else None

    def last(self) -> Workflow | None:
        with self._lock:
            return max(self._workflows.values(), key=lambda w: w.created_at) if self._workflows else None

    def awaiting_user(self) -> bool:
        return any(w.status is WStatus.WAITING_FOR_USER for w in self.active())

    def connected_systems(self) -> dict[str, bool]:
        return {s: ok for s, (ok, _) in self.planner.availability().items()}

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            wfs = list(self._workflows.values())
        cur = self.current()
        return {"enabled": self.cfg.enabled, "current": cur.summary() if cur else None,
                "waiting": [w.summary() for w in wfs if w.status in (WStatus.WAITING_FOR_CONFIRMATION, WStatus.WAITING_FOR_USER, WStatus.WAITING_FOR_DATA, WStatus.PAUSED)],
                "failed": [w.summary() for w in wfs if w.status is WStatus.FAILED][-5:], "history": self.store.history(self.cfg.history_size), "systems": self.connected_systems(),
                "limits": {"max_duration_s": self.cfg.max_duration_s, "max_steps": self.cfg.max_steps, "max_tool_calls": self.cfg.max_tool_calls, "max_concurrent": self.cfg.max_concurrent,
                           "max_retries": self.cfg.max_retries}, "recovered": list(self.recovered), "audit": self.store.audit_recent(30)}

    # ---- starting ---------------------------------------------------------------------------------------------------------------------------

    def start(self, text: str, session_id: str, *, requested_by: str = "user") -> Reply | None:
        """A Reply if `text` is a workflow request (a plan, a question, a refusal); None if it is not one (let the other routers see it)."""
        if not self.cfg.enabled:
            return None
        if requested_by != "user" and not self.cfg.proactive:
            return None
        with self._start_lock:
            return self._start(text, session_id, requested_by)

    def _start(self, text: str, session_id: str, requested_by: str) -> Reply | None:
        outcome = self.planner.plan(text, session_id, last_fact=self.last_fact, requested_by=requested_by)
        if outcome.kind == "none":
            return self._unmatched(text)
        if outcome.kind in ("refuse", "unavailable", "ask"):
            self._remember_failed(text, outcome.message)
            return Reply(outcome.message)
        wf = outcome.workflow
        assert wf is not None
        return self._launch(wf)

    def _unmatched(self, text: str) -> Reply | None:
        """A compound request across several of the user's systems that no template covers needs a model to plan it. Without one, say so honestly."""
        t = text.lower()
        sysm = systems_mentioned(t)
        compound = len(sysm & _SOURCES) >= 2 or (len(sysm & _SOURCES) == 1 and len(sysm - _SOURCES) >= 1 and bool(_CONNECTOR.search(t)))
        if compound and _CONNECTOR.search(t) and not (self._llm_available and self._llm_available()):
            return Reply("I can handle that workflow, but the conversational model is currently unavailable.")
        return None

    def submit_proposal(self, goal: str, proposal: dict[str, Any], session_id: str) -> Reply:
        """A model-proposed plan: validated exactly like any other, executed only after schema, policy and permission checks."""
        with self._start_lock:
            outcome = self.planner.from_proposal(goal, proposal, session_id)
            if outcome.kind != "plan" or outcome.workflow is None:
                return Reply(outcome.message or "I couldn't turn that into a safe plan.")
            return self._launch(outcome.workflow)

    def _launch(self, wf: Workflow) -> Reply:
        with self._lock:
            twin = next((w for w in self._workflows.values() if not w.terminal and w.template == wf.template and w.params.get("topic") == wf.params.get("topic")
                         and w.params.get("days_before") == wf.params.get("days_before") and w.session_id == wf.session_id), None)
            if twin is not None:
                return Reply(f"I'm already working on that: {twin.goal[:80]}.", twin)          # deduplication: the same request twice is one workflow
            if self.running_count() >= self.cfg.max_concurrent:
                return Reply("I'm already running as many workflows as I'm allowed to. Let me finish one first, or say stop.")
            if len(self.active()) >= 3 * self.cfg.max_concurrent:
                return Reply("There are too many unfinished workflows. Say cancel to drop them, or finish the ones that are waiting for you.")
            self._workflows[wf.workflow_id] = wf
            for old in [k for k, w in self._workflows.items() if w.terminal][:-10]:
                self._workflows.pop(old, None)
                self._runners.pop(old, None)
        runner = WorkflowRunner(wf, self.router, self.store, self.cfg, confirm=self._confirm, on_update=self._on_update, clock=self._clock, sleep=self._sleep)
        with self._lock:
            self._runners[wf.workflow_id] = runner
        wf.touch(WStatus.READY)
        self.store.save_checkpoint(wf)
        self.store.audit(wf.workflow_id, "workflow_planned", template=wf.template, steps=len(wf.steps), risk=wf.risk_level.name, scope=",".join(sorted(wf.scope)))
        metrics.incr("workflow.started")
        return self._run(wf, runner, ack=True)

    def _run(self, wf: Workflow, runner: WorkflowRunner, *, ack: bool) -> Reply:
        if not self._threaded:
            runner.run()
            self._after(wf)
            return Reply(self._text(wf), wf)
        wf.ack_open = True  # type: ignore[attr-defined]
        t = threading.Thread(target=self._thread_main, args=(wf, runner), name=f"jarvis-workflow-{wf.workflow_id}", daemon=True)
        with self._lock:
            self._threads[wf.workflow_id] = t
        t.start()
        try:
            return self._inline(wf, ack)
        finally:
            wf.ack_open = False  # type: ignore[attr-defined]

    def _thread_main(self, wf: Workflow, runner: WorkflowRunner) -> None:
        runner.run()
        self._after(wf)
        for _ in range(100):
            if not getattr(wf, "ack_open", False):
                break
            time.sleep(0.01)
        if wf.workflow_id not in self._delivered and (wf.terminal or wf.status in (WStatus.WAITING_FOR_DATA, WStatus.WAITING_FOR_USER)):
            self._say(self._text(wf), "high", key=f"wf:{wf.workflow_id}:{wf.status.value}")

    def _inline(self, wf: Workflow, ack: bool) -> Reply:
        end = time.monotonic() + self._inline_wait
        while time.monotonic() < end:
            if wf.terminal or wf.status in (WStatus.WAITING_FOR_USER, WStatus.WAITING_FOR_CONFIRMATION, WStatus.WAITING_FOR_DATA):
                break
            time.sleep(0.02)
        if wf.terminal or wf.status in (WStatus.WAITING_FOR_USER, WStatus.WAITING_FOR_DATA, WStatus.WAITING_FOR_CONFIRMATION):
            self._delivered.add(wf.workflow_id)
            if wf.status is WStatus.WAITING_FOR_CONFIRMATION:
                self._delivered.discard(wf.workflow_id)
                return Reply((wf.question or "").strip(), wf)
            return Reply(self._text(wf), wf)
        return Reply(f"{wf.ack} I'll tell you when I'm done.", wf)

    def _text(self, wf: Workflow) -> str:
        if wf.status is WStatus.WAITING_FOR_USER and wf.question:
            return wf.question
        if wf.status is WStatus.WAITING_FOR_CONFIRMATION and wf.question:
            return wf.question
        if wf.result is not None:
            return wf.result.summary
        return wf.failure or "Done."

    def _after(self, wf: Workflow) -> None:
        """Bookkeeping when a run ends: remember useful context, never act on it."""
        with self._lock:
            facts = [f for f in wf.facts if f.status in factlib.ACTIONABLE_STATUSES]
            if facts and wf.status in (WStatus.COMPLETED, WStatus.WAITING_FOR_USER):
                chosen = next((s.output["fact"] for s in wf.steps if s.tool == "verify_deadline_fact" and s.status is SStatus.DONE and s.output.get("fact")), None)
                self.last_fact = chosen or facts[0].to_dict()
            brief = next((s for s in wf.steps if s.tool == "briefing_compose" and s.status is SStatus.DONE), None)
            if brief is not None:
                self.last_briefing = (time.monotonic(), brief.output.get("items", []), brief.output.get("text", ""))

    def _on_update(self, wf: Workflow) -> None:
        pass

    # ---- confirmation (the shared Phase 17 ConfirmationEngine) ----------------------------------------------------------------------------

    def _confirm(self, wf: Workflow, steps: list, summary: str) -> str:
        eng = self._confirmations
        if eng is None:
            return "declined"                                      # no confirmation channel: consequential steps never run
        sid = wf.session_id or wf.workflow_id
        runner = self._runners.get(wf.workflow_id)
        wait_end = self._clock() + self.cfg.confirmation_timeout_s
        while eng.has_pending(sid):                                # the session has one open question at a time: queue behind it
            if runner is not None and runner.cancel_event.is_set():
                return "cancelled"
            if self._clock() > wait_end:
                return "timeout"
            self._sleep(0.05)
        key = f"{wf.workflow_id}:{steps[0].step_id}"
        self._approved[key] = False

        def approve() -> ActionReport:
            self._approved[key] = True
            return ActionReport(True, "Okay, continuing.", True)

        cls = ApprovalClass.SENSITIVE_DESKTOP if max(s.risk for s in steps) >= Risk.SENSITIVE else ApprovalClass.EXTERNAL_MESSAGE
        eng.request(action_class=cls, tool="workflow.step", summary=summary, params={"workflow": wf.workflow_id, "steps": [s.step_id for s in steps]}, run=approve, session_id=sid, source="workflow")
        wf.touch(WStatus.WAITING_FOR_CONFIRMATION)      # only now: a "yes" that arrives from here on has a pending action to answer
        self.store.save_checkpoint(wf)
        self._say(summary, "high", key=f"wf-confirm:{key}")
        end = self._clock() + self.cfg.confirmation_timeout_s
        while True:
            if runner is not None and runner.cancel_event.is_set():
                eng.cancel(sid)
                return "cancelled"
            if self._approved.get(key):
                self._approved.pop(key, None)
                return "approved"
            if not eng.has_pending(sid):
                return "approved" if self._approved.pop(key, False) else "declined"
            if self._clock() > end:
                eng.cancel(sid)
                return "timeout"
            self._sleep(0.05)

    def confirm(self, workflow_id: str, approve: bool) -> Reply:
        """API path for the dashboard's Confirm/Decline: goes through the same engine (single use, bound to the exact steps)."""
        wf = self.get(workflow_id)
        if wf is None or wf.status is not WStatus.WAITING_FOR_CONFIRMATION or self._confirmations is None:
            return Reply("Nothing is waiting for confirmation.")
        sid = wf.session_id or wf.workflow_id
        answered = self._confirmations.respond("yes" if approve else "no", sid)
        return Reply(answered or "Nothing is waiting for confirmation.", wf)

    # ---- answers and controls ------------------------------------------------------------------------------------------------------------------

    def respond(self, text: str, session_id: str) -> Reply | None:
        """The user's answer to a waiting workflow (which date, which link). None if this isn't an answer."""
        wf = next((w for w in self.active() if w.status is WStatus.WAITING_FOR_USER and (not w.session_id or w.session_id == session_id)), None)
        if wf is None:
            return None
        runner = self._runners.get(wf.workflow_id)
        step = next((s for s in wf.steps if s.status is SStatus.FAILED and s.failure is FailureKind.AMBIGUOUS), None)
        if runner is None or step is None:
            return None
        from autonomy.manager import AutonomyManager

        n = AutonomyManager._pick(text.strip().strip(".!?"), wf.choices)
        if n is None:
            return Reply(wf.question or "Which one do you mean?", wf)
        output = self._answer_output(step, wf, n)
        if output is None:
            return Reply(wf.question or "Which one do you mean?", wf)
        runner.provide_answer(step.step_id, output)
        self.store.audit(wf.workflow_id, "user_answer", step=step.step_id, choice=n)
        return self._run(wf, runner, ack=False)

    @staticmethod
    def _answer_output(step, wf: Workflow, n: int) -> dict[str, Any] | None:
        choices = wf.choices
        if not 1 <= n <= len(choices):
            return None
        if step.tool == "verify_deadline_fact" and choices[n - 1].get("fact"):
            return {"fact": choices[n - 1]["fact"]}
        if step.tool == "calendar_compare_fact" and step.output.get("fact") and step.output.get("conflict"):
            fact = dict(step.output["fact"])
            if n == 2:
                due = factlib.date_of(fact["value"])
                new_day = step.output["conflict"]["calendar_date"]
                if due is not None:
                    fact["value"] = due.replace(year=int(new_day[:4]), month=int(new_day[5:7]), day=int(new_day[8:10])).isoformat()
                    fact["status"] = "VERIFIED"
                    fact["notes"] = list(fact.get("notes", [])) + ["date chosen by you from your calendar"]
            else:
                fact["status"] = "VERIFIED"
                fact["notes"] = list(fact.get("notes", [])) + ["date chosen by you from the email"]
            return {"fact": fact, "conflict": None, "resolved": "user"}
        if step.tool == "email_links" and choices[n - 1].get("url"):
            return {"url": choices[n - 1]["url"], "host": choices[n - 1].get("name", "")}
        return None

    def cancel(self, workflow_id: str | None = None, reason: str = "stopped by the user") -> str:
        """Stop future actions of the workflow (or every active one). What already happened stays in the history; nothing is undone; nothing resumes by itself."""
        targets = [self.get(workflow_id)] if workflow_id else self.active()
        targets = [w for w in targets if w is not None and not w.terminal]
        if not targets:
            return "There's nothing running to stop."
        for wf in targets:
            runner = self._runners.get(wf.workflow_id)
            if runner is not None:
                runner.cancel(reason)
            if self._confirmations is not None:
                self._confirmations.cancel(wf.session_id or wf.workflow_id)     # clear any pending confirmation
        if self._browser_stop is not None:
            try:
                self._browser_stop()
            except Exception:  # noqa: BLE001
                pass
        for wf in targets:
            t = self._threads.get(wf.workflow_id)
            if t is not None and t is not threading.current_thread() and t.is_alive():
                t.join(timeout=3.0)
            if not wf.terminal:                                 # waiting/paused workflows have no live thread: close them here
                self._close_cancelled(wf, reason)
        metrics.incr("workflow.cancellations")
        self.store.audit(targets[0].workflow_id, "workflow_cancelled", reason=reason)
        return "Okay, I stopped." + (" Nothing else will run." if len(targets) else "")

    def _close_cancelled(self, wf: Workflow, reason: str) -> None:
        runner = self._runners.get(wf.workflow_id)
        for s in wf.steps:
            if s.status in (SStatus.PENDING, SStatus.RUNNING):
                s.status, s.note = SStatus.SKIPPED, "stopped before it ran"
        wf.failure, wf.failure_kind, wf.finished_at, wf.question = reason, FailureKind.USER_CANCELLED, time.time(), None
        wf.touch(WStatus.CANCELLED)
        if runner is not None:
            wf.result = runner._result("cancelled")
        self.store.save_checkpoint(wf)
        self.store.record_history(wf)

    def pause(self) -> str:
        cur = next((w for w in self.active() if w.status in (WStatus.RUNNING, WStatus.WAITING_FOR_CONFIRMATION)), None)
        runner = self._runners.get(cur.workflow_id) if cur else None
        if runner is None or not runner.pause():
            return "There's no running workflow to pause."
        return "Paused. Say resume to carry on."

    def resume(self, workflow_id: str | None = None) -> str:
        wf = self.get(workflow_id) if workflow_id else next((w for w in self.active() if w.status in (WStatus.PAUSED, WStatus.WAITING_FOR_DATA)), None)
        if wf is None or wf.terminal:
            return "There's no paused workflow."
        runner = self._runners.get(wf.workflow_id)
        if runner is None:
            return "I can't resume that one; please ask again."
        if runner.paused:
            runner.resume()
            return "Resuming."
        if wf.status in (WStatus.PAUSED, WStatus.WAITING_FOR_DATA):
            wf.touch(WStatus.READY)
            self._run(wf, runner, ack=False)
            return "Okay, trying again." if wf.status is not WStatus.PAUSED else "Resuming."
        return "That workflow isn't paused."

    def describe(self) -> str:
        wf = self.current() or self.last()
        if wf is None:
            return "I'm not running any workflow."
        s = wf.summary()
        if not wf.terminal:
            extra = f" I'm waiting for you: {wf.question}" if wf.question else ""
            return f"I'm working on: {wf.goal[:100]}. {s['progress'][0]} of {s['progress'][1]} steps done" + (f", now: {s['current_step']}" if s["current_step"] else "") + f".{extra}"
        return f"My last workflow was: {wf.goal[:100]}. " + self._text(wf)

    def why_failed(self) -> str:
        wf = self.last()
        if wf is None or wf.status not in (WStatus.FAILED, WStatus.WAITING_FOR_DATA):
            return "Nothing has failed."
        return self._text(wf)

    def tell_more(self) -> str | None:
        """"Tell me more" after a briefing: the reasons behind each item, from the same synthesis (nothing new is invented)."""
        if self.last_briefing is None or time.monotonic() - self.last_briefing[0] > _FRESH_S:
            return None
        items = self.last_briefing[1]
        if not items:
            return "There isn't more to say: nothing needs your attention."
        return " ".join(f"{i['title']}: {'; '.join(i['reasons'])}." for i in items[:5])

    # ---- recovery ----------------------------------------------------------------------------------------------------------------------------

    def recover(self) -> list[str]:
        """After a restart: load unfinished workflows as PAUSED. Nothing runs. External state is verified by the tools' effect ledger at resume time, so an effect that
        may already have happened (a created task, a clicked button) is adopted or refused, never repeated."""
        found: list[str] = []
        for cp in self.store.load_incomplete():
            wid = cp["workflow_id"]
            if wid in self._workflows:
                continue
            wf = self._rebuild(cp)
            if wf is None:
                self.store.mark(wid, WStatus.CANCELLED, "could not be rebuilt after a restart")
                continue
            runner = WorkflowRunner(wf, self.router, self.store, self.cfg, confirm=self._confirm, on_update=self._on_update, clock=self._clock, sleep=self._sleep)
            self._rehydrate(wf, runner)
            with self._lock:
                self._workflows[wid] = wf
                self._runners[wid] = runner
            self.store.audit(wid, "workflow_recovered", status="PAUSED", completed=len(cp.get("completed_steps", [])))
            found.append(wid)
        self.recovered = found
        if found and self._announce is not None:
            wf = self._workflows[found[0]]
            self._say(f"I found an unfinished workflow from before: {wf.goal[:80]}. Say resume to continue it, or cancel to drop it.", "normal", key=f"wf-recovered:{found[0]}")
        return found

    def _rebuild(self, cp: dict[str, Any]) -> Workflow | None:
        name = cp.get("template")
        if name not in TEMPLATES:
            return None
        avail = {s for s, (ok, _) in self.planner.availability().items() if ok}
        try:
            steps = TEMPLATES[name].build({**cp.get("params", {}), "available": avail})
        except (KeyError, TypeError, ValueError):
            return None
        wf = Workflow(goal=cp.get("goal", ""), template=name, steps=steps, session_id=cp.get("session_id", ""), requested_by=cp.get("requested_by", "user"), workflow_id=cp["workflow_id"])
        wf.params = dict(cp.get("params", {}))
        wf.scope = {s.source for s in steps if s.source and s.source != "local"}     # recomputed from the rebuilt steps, never trusted from the file
        if self.planner.validate(wf) is not None:                                     # a damaged or edited checkpoint goes through the same validation as any plan
            return None
        self.planner._finish(wf, TEMPLATES[name])
        saved = {s["id"]: s for s in cp.get("steps", [])}
        for s in wf.steps:
            old = saved.get(s.step_id)
            if not old:
                continue
            status = SStatus(old["status"])
            if status in (SStatus.DONE, SStatus.SKIPPED):
                s.status, s.output, s.note, s.idempotency_key = status, old.get("output", {}), old.get("note", ""), old.get("idempotency_key", "")
            elif status is SStatus.RUNNING:
                s.note = "was in progress when JARVIS stopped"
                s.status = SStatus.PENDING
        for f in cp.get("facts", []):
            try:
                wf.facts.append(Fact.from_dict(f))
            except (KeyError, ValueError):
                pass
        wf.recovered = True
        wf.created_at = cp.get("created_at", wf.created_at)
        wf.touch(WStatus.PAUSED)
        wf.warnings.append("This was interrupted by a restart. Before I repeat anything I check what already exists.")
        return wf

    @staticmethod
    def _rehydrate(wf: Workflow, runner: WorkflowRunner) -> None:
        """Reads whose output wasn't persisted (email text is never written to disk) are simply read again; writes are never re-run (their ledger entry decides)."""
        by_id = {s.step_id: s for s in wf.steps}
        changed = True
        while changed:
            changed = False
            for s in wf.steps:
                if s.status is not SStatus.PENDING:
                    continue
                for v in s.arguments.values():
                    ref = v if hasattr(v, "step") else None
                    if ref is None:
                        continue
                    src = by_id[ref.step]
                    head = ref.path.split(".")[0] if ref.path else ""
                    if src.status is SStatus.DONE and head and head not in src.output and not src.side_effect:
                        src.status, src.output = SStatus.PENDING, {}
                        changed = True
        for s in wf.steps:
            if s.status is SStatus.DONE and s.output:
                runner.outputs[s.step_id] = s.output

    # ---- proactive suggestions ---------------------------------------------------------------------------------------------------------------------

    def suggest(self, text: str, session_id: str = "proactive") -> Reply | None:
        """Proactive intelligence may plan a workflow, but it runs suggestion-only: reads happen, no task/reminder/browser action is taken."""
        return self.start(text, session_id, requested_by="proactive")

    def on_deadline_event(self, event) -> None:
        """EventBus DEADLINE_DETECTED (Gmail): run a suggestion-only review (reads only) and tell the user what was found, once per deadline. It never creates a task or reminder:
        the user says "turn that into a task" and the normal, confirmed-by-request workflow does it."""
        if not (self.cfg.enabled and self.cfg.proactive) or event.payload.get("source") != "gmail":
            return
        now = time.monotonic()
        if now - getattr(self, "_last_proactive", -1e9) < 600 or self.active():
            return                                                                     # throttled; and never while the user's own workflow is running
        self._last_proactive = now
        try:
            reply = self.suggest("Turn the deadlines in my important emails into tasks", "proactive")
        except Exception as exc:  # noqa: BLE001 - a background suggestion must never disturb anything
            logger.error("Proactive workflow failed (%s)", type(exc).__name__)
            return
        wf = reply.workflow if reply is not None else None
        if wf is None:
            return
        if self._threaded:
            for _ in range(200):
                if wf.terminal:
                    break
                time.sleep(0.05)
        seen = self.router.ctx.notified
        for f in [x for x in wf.facts if x.status in factlib.ACTIONABLE_STATUSES]:
            key = f"proactive:{f.source_id}:{f.value[:10]}"
            if key in seen:
                continue
            seen[key] = time.monotonic()
            self.last_fact = f.to_dict()
            when = factlib.date_of(f.value)
            day = when.astimezone(self.router.ctx.zone).strftime("%B %d").replace(" 0", " ") if when else "an upcoming date"
            self._say(f"I noticed a deadline in an email: {f.title[:80]} on {day}. Say 'turn that into a task' if you want me to add it.", "normal", key=key)
            break

    # ---- housekeeping ------------------------------------------------------------------------------------------------------------------------------

    def _say(self, text: str, priority: str, key: str = "") -> None:
        if self._announce is not None and text:
            try:
                self._announce(text, priority)
            except Exception:  # noqa: BLE001
                pass

    def _remember_failed(self, goal: str, reason: str) -> None:
        self.store.audit("-", "request_refused", goal=redact(goal)[:120], reason=redact(reason)[:160])

    def shutdown(self) -> None:
        """Application exit: stop running workflows; their checkpoints stay PAUSED-recoverable and never resume by themselves."""
        for wf in self.active():
            runner = self._runners.get(wf.workflow_id)
            if runner is not None:
                runner.cancel("JARVIS was shutting down")
                if self._confirmations is not None:
                    self._confirmations.cancel(wf.session_id or wf.workflow_id)
        if self._browser_stop is not None:
            try:
                self._browser_stop()
            except Exception:  # noqa: BLE001
                pass
        for t in list(self._threads.values()):
            if t.is_alive() and t is not threading.current_thread():
                t.join(timeout=3.0)


class OperatorIntentRouter:
    """Recognises workflow requests and controls inside a conversation. Plugged into IntelligenceRouter ahead of the autonomy router."""

    def __init__(self, operator: PersonalOperator):
        self._op = operator

    def intercept_cancel(self, text: str, session_id: str) -> str | None:
        """Called before the confirmation engine: "Stop"/"Cancel the workflow" must end a workflow that is waiting for a yes, not merely decline that one step."""
        op = self._op
        if not is_control(re.sub(r"^(?:hey |hi |ok |okay )?jarvis[, ]*", "", text.strip(), flags=re.I)):
            return None
        act = [w for w in op.active() if w.status is WStatus.WAITING_FOR_CONFIRMATION and (not w.session_id or w.session_id == session_id)]
        return op.cancel(act[0].workflow_id) if act else None

    def handle(self, t: str, original: str, session_id: str) -> str | None:
        op = self._op
        try:
            if op.awaiting_user():
                answered = op.respond(original, session_id)
                if answered is not None:
                    return answered.text
            text = re.sub(r"^(?:hey |hi |ok |okay )?jarvis[, ]*", "", original.strip(), flags=re.I).strip(" .!?")
            if is_control(text) and op.active():
                return op.cancel()
            if _PAUSE.match(text) and op.active():
                return op.pause()
            if _RESUME.match(text) and any(w.status in (WStatus.PAUSED, WStatus.WAITING_FOR_DATA) for w in op.active()):
                return op.resume()
            if _STATUS.match(text) and op.last() is not None:
                return op.describe()
            if _WHY_FAILED.match(text) and op.last() is not None:
                return op.why_failed()
            if _UNFINISHED.match(text):
                rec = [w for w in op.active() if w.recovered]
                return ("Unfinished from before: " + "; ".join(w.goal[:70] for w in rec) + ". Say resume or cancel.") if rec else "There are no unfinished workflows."
            if _MORE.match(text):
                more = op.tell_more()
                if more is not None:
                    return more
            if not op.cfg.enabled:
                return None
            reply = op.start(original, session_id)
            return reply.text if reply is not None else None
        except Exception as exc:  # noqa: BLE001 - the conversation must survive
            logger.error("Workflow request failed (%s)", type(exc).__name__)
            return "Sorry, I couldn't carry that workflow out."

    def cancel_task(self) -> bool:
        if not self._op.active():
            return False
        self._op.cancel()
        return True

    def task_active(self) -> bool:
        return any(w.status in (WStatus.RUNNING, WStatus.WAITING_FOR_CONFIRMATION, WStatus.READY) for w in self._op.active())
