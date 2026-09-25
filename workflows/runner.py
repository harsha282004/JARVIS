"""The workflow runner: executes a validated Workflow's steps in dependency order through the OperatorRouter, one thread per workflow.

What it guarantees (each one has a test):
  * A step runs only when every step it depends on succeeded; otherwise it is BLOCKED and the result says which part didn't happen and why (Step 4 never runs after Step 3 fails).
  * Data flows only through `From(step, path)` references to steps it declared as dependencies; URLs are validated when they are resolved.
  * A step at EXTERNAL_EFFECT risk or above is not run until the user confirms through the shared ConfirmationEngine, with all such pending steps grouped into one clear question.
  * Only reads are retried. A write is attempted once; the tools record it in the effect ledger (begun before, done after) so a crash or a resume can never repeat it.
  * Cancel is checked before every step and while waiting; the write lock is held across the check and the write, so a stop cannot interleave with a half-done effect.
  * Workflows started by proactive intelligence are suggestion-only: write steps are skipped and reported as suggestions.
  * "Done" is only said when every side-effect step was verified by reading the result back.
"""

import re
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable

from autonomy.models import Risk
from backend.core.logging import get_logger
from backend.core.metrics import metrics
from browser.urlsafe import validate_url
from workflows.models import From, SStatus, WStatus, WStep, Workflow, WorkflowResult, FailureKind
from workflows.store import WorkflowStore
from workflows.tools import OperatorRouter, ToolOut, SPECS
from workflows.templates import SYSTEM_LABEL

logger = get_logger(__name__)

WAITING_KINDS = frozenset({FailureKind.TEMPORARY, FailureKind.AUTHENTICATION, FailureKind.EXTERNAL_SERVICE})
RETRYABLE_KINDS = frozenset({FailureKind.TEMPORARY, FailureKind.EXTERNAL_SERVICE, FailureKind.AUTHENTICATION, FailureKind.PERMISSION})
OPTIONAL_TOLERATED = frozenset({FailureKind.TEMPORARY, FailureKind.EXTERNAL_SERVICE, FailureKind.AUTHENTICATION, FailureKind.PERMISSION, FailureKind.DATA_MISSING, FailureKind.VERIFICATION_FAILED})


@dataclass
class OperatorConfig:
    enabled: bool = True
    max_concurrent: int = 2
    max_duration_s: float = 240.0
    max_steps: int = 14
    max_tool_calls: int = 30
    max_retries: int = 2
    max_systems: int = 5
    confirmation_timeout_s: float = 120.0
    history_size: int = 12
    proactive: bool = True


class _Stop(Exception):
    def __init__(self, status: WStatus, reason: str, kind: FailureKind | None = None):
        super().__init__(reason)
        self.status, self.reason, self.kind = status, reason, kind


def resolve(arguments: dict[str, Any], outputs: dict[str, dict[str, Any]], steps: dict[str, WStep]) -> tuple[dict[str, Any], str | None, bool]:
    """(arguments, problem, skip). `skip` True: a needed result isn't available (its step was skipped), so this step is skipped too, not failed."""
    out: dict[str, Any] = {}
    for key, value in arguments.items():
        if not isinstance(value, From):
            out[key] = value
            continue
        while value.fallback is not None and outputs.get(value.step) is None:
            value = value.fallback
        src = steps.get(value.step)
        got: Any = outputs.get(value.step)
        if got is None:
            if value.optional:
                out[key] = []
                continue
            return {}, f"the result of '{src.description if src else value.step}' isn't available", True
        for part in [p for p in value.path.split(".") if p]:
            if isinstance(got, dict) and part in got:
                got = got[part]
            else:
                if value.optional:
                    got = []
                    break
                return {}, f"'{src.description if src else value.step}' didn't produce {value.path}", False
        if key == "url":
            d = validate_url(got if isinstance(got, str) else "", allow_private=False, resolver=None)
            if not d.ok:
                return {}, "that web address isn't allowed", False
            got = d.url
        out[key] = got
    return out, None, False


def _rule_ok(rule: str, out: ToolOut) -> bool:
    if rule == "readback":
        return out.verified
    if rule.startswith("has:"):
        return out.data.get(rule[4:]) is not None     # present (an empty list is a real answer: "nothing on your calendar"); tools fail DATA_MISSING when emptiness means failure
    return True


class WorkflowRunner:
    def __init__(self, wf: Workflow, router: OperatorRouter, store: WorkflowStore, cfg: OperatorConfig, *, confirm: Callable[[Workflow, list[WStep], str], str],
                 on_update: Callable[[Workflow], None] | None = None, clock: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep):
        self.wf, self.router, self.store, self.cfg = wf, router, store, cfg
        self._confirm, self._on_update, self._clock, self._sleep = confirm, on_update, clock, sleep
        self.cancel_event = threading.Event()
        self._paused = threading.Event()
        self.outputs: dict[str, dict[str, Any]] = {}
        self._steps = {s.step_id: s for s in wf.steps}
        self._confirmed: set[str] = set()
        self._began = clock()
        self._answer: tuple[str, dict[str, Any]] | None = None

    # ---- controls ----------------------------------------------------------------------------------------------------------------------

    def cancel(self, reason: str = "stopped by the user") -> None:
        self.wf.cancel_reason = reason
        self.cancel_event.set()

    def pause(self) -> bool:
        if self.wf.terminal or self.wf.status in (WStatus.WAITING_FOR_USER, WStatus.WAITING_FOR_CONFIRMATION):
            return False
        self._paused.set()
        return True

    def resume(self) -> None:
        self._paused.clear()

    @property
    def paused(self) -> bool:
        return self._paused.is_set()

    def provide_answer(self, step_id: str, output: dict[str, Any]) -> None:
        """The user's choice for a step that asked (which date, which link): the step is completed with that output and the run continues."""
        step = self._steps[step_id]
        step.status, step.output, step.note, step.failure = SStatus.DONE, output, "chosen by you", None
        self.outputs[step_id] = output
        self.wf.question, self.wf.choices = None, []

    # ---- main loop ---------------------------------------------------------------------------------------------------------------------

    def run(self) -> None:
        wf = self.wf
        self._began = self._clock()
        for s in wf.steps:            # re-entrant: a resumed run retries what was blocked by a temporary problem and re-reads what was mid-flight
            if s.status in (SStatus.RUNNING,) or (s.status in (SStatus.FAILED, SStatus.BLOCKED) and (s.failure in RETRYABLE_KINDS or s.status is SStatus.BLOCKED)):
                s.status, s.failure = SStatus.PENDING, None
            if s.status is SStatus.DONE and s.output:
                self.outputs[s.step_id] = s.output
        wf.failure, wf.failure_kind = "", None
        wf.touch(WStatus.RUNNING)
        self._update()
        self.store.audit(wf.workflow_id, "workflow_start", template=wf.template, risk=wf.risk_level.name, requested_by=wf.requested_by)
        try:
            self._loop()
            self._finish()
        except _Stop as stop:
            self._stopped(stop)
        except Exception as exc:  # noqa: BLE001 - a bug must never leave a workflow "running" or report success
            logger.error("Workflow %s crashed (%s)", wf.workflow_id, type(exc).__name__)
            wf.failure, wf.failure_kind = f"Something went wrong inside the workflow ({type(exc).__name__}).", FailureKind.EXTERNAL_SERVICE
            wf.finished_at = time.time()
            wf.touch(WStatus.FAILED)
            wf.result = self._result("failed")
        finally:
            self.store.save_checkpoint(wf)
            if wf.terminal:
                self.store.record_history(wf)
            self.store.audit(wf.workflow_id, "workflow_end", status=wf.status.value, failure_kind=wf.failure_kind.value if wf.failure_kind else "")
            metrics.observe("workflow.total_ms", (self._clock() - self._began) * 1000)
            self._update()

    def _update(self) -> None:
        self.wf.touch()
        if self._on_update is not None:
            try:
                self._on_update(self.wf)
            except Exception:  # noqa: BLE001
                pass

    def _check_alive(self) -> None:
        wf = self.wf
        if self.cancel_event.is_set():
            raise _Stop(WStatus.CANCELLED, wf.cancel_reason or "stopped by the user", FailureKind.USER_CANCELLED)
        while self._paused.is_set():
            if wf.status is not WStatus.PAUSED:
                wf.touch(WStatus.PAUSED)
                self.store.save_checkpoint(wf)
                self._update()
            self._sleep(0.05)
            if self.cancel_event.is_set():
                raise _Stop(WStatus.CANCELLED, wf.cancel_reason or "stopped by the user", FailureKind.USER_CANCELLED)
        if wf.status is WStatus.PAUSED:
            wf.touch(WStatus.RUNNING)
        if self._clock() - self._began > self.cfg.max_duration_s:
            raise _Stop(WStatus.FAILED, "That workflow took longer than the time limit, so I stopped it.", FailureKind.RESOURCE_LIMIT)
        if wf.tool_calls >= self.cfg.max_tool_calls:
            raise _Stop(WStatus.FAILED, "That workflow used more actions than allowed, so I stopped it.", FailureKind.RESOURCE_LIMIT)

    def _loop(self) -> None:
        wf = self.wf
        for step in wf.steps:
            if step.status is not SStatus.PENDING:
                continue
            self._check_alive()
            failed_dep = next((self._steps[d] for d in step.dependencies if self._steps[d].status in (SStatus.FAILED, SStatus.BLOCKED)), None)
            if failed_dep is not None:
                step.status, step.note = SStatus.BLOCKED, f"blocked because '{failed_dep.description}' didn't work"
                self.store.audit(wf.workflow_id, "step_blocked", step=step.step_id, tool=step.tool)
                self._save()
                continue
            waiting = next((self._steps[d] for d in step.dependencies if self._steps[d].status is SStatus.SKIPPED and self._steps[d].step_id not in self.outputs
                            and any(isinstance(v, From) and v.step == d and not v.optional and v.fallback is None for v in step.arguments.values())), None)
            if waiting is not None:
                step.status, step.note = SStatus.SKIPPED, f"skipped because '{waiting.description}' wasn't available"
                self._save()
                continue
            args, problem, skip = resolve(step.arguments, self.outputs, self._steps)
            if problem:
                step.status, step.note = (SStatus.SKIPPED if skip else SStatus.FAILED), problem
                step.failure = None if skip else FailureKind.DATA_MISSING
                self._save()
                continue
            if step.side_effect and wf.requested_by != "user":
                step.status, step.note = SStatus.SKIPPED, "suggestion only: I don't change anything for a suggestion"
                wf.warnings.append(f"Suggestion: {step.description.lower()}. Ask me to do it and I will.")
                self._save()
                continue
            if step.risk.needs_confirmation and step.step_id not in self._confirmed:
                self._gate(step)
                if step.status is not SStatus.PENDING:
                    self._save()
                    continue
            self._execute(step, args)
            self._save()
            if wf.status is WStatus.WAITING_FOR_USER:
                return

    def _save(self) -> None:
        self.store.save_checkpoint(self.wf)
        self._update()

    # ---- confirmation ------------------------------------------------------------------------------------------------------------------------

    def _gate(self, step: WStep) -> None:
        """One clear question for every pending step that needs confirmation (grouped), then continue only on the user's own yes."""
        wf = self.wf
        group = [s for s in wf.steps if s.status is SStatus.PENDING and s.risk.needs_confirmation and s.step_id not in self._confirmed
                 and all(self._steps[d].status in (SStatus.DONE, SStatus.SKIPPED, SStatus.PENDING) for d in s.dependencies)]
        summary = self._preview(group or [step])
        wf.pending_step, wf.question = step.step_id, summary
        self.store.audit(wf.workflow_id, "confirmation_requested", step=step.step_id, risk=step.risk.name, tool=step.tool)
        decision = self._confirm(wf, group or [step], summary)
        wf.pending_step, wf.question = None, None
        wf.touch(WStatus.RUNNING)
        self.store.audit(wf.workflow_id, "confirmation_" + decision, step=step.step_id)
        if decision == "approved":
            self._confirmed.update(s.step_id for s in (group or [step]))
        elif decision == "cancelled" or self.cancel_event.is_set():
            raise _Stop(WStatus.CANCELLED, wf.cancel_reason or "stopped by the user", FailureKind.USER_CANCELLED)
        elif decision == "declined":
            for s in group or [step]:
                s.status, s.note = SStatus.SKIPPED, "you chose not to"
        else:
            raise _Stop(WStatus.FAILED, "I didn't get your confirmation in time, so I did not do it.", FailureKind.PERMISSION)

    def _preview(self, steps: list[WStep]) -> str:
        url = next((o.get("url") for o in self.outputs.values() if o.get("url")), "")
        host = re.sub(r"^https?://", "", str(url)).split("/")[0] if url else ""
        reqs = next((o.get("requirements") for o in self.outputs.values() if o.get("requirements")), [])
        parts = []
        if host:
            parts.append(f"I've opened the page on {host} and read it.")
        if reqs:
            parts.append("The documents say it needs: " + ", ".join(str(r["text"]) for r in reqs[:5]) + ".")
        mem = next((o["memories"][0]["content"] for o in self.outputs.values() if o.get("memories")), "")
        if mem:
            parts.append(f"From what you've told me before: {mem[:120]}.")           # context only: it never changes what is done
        acts = " and ".join(s.description[0].lower() + s.description[1:] for s in steps)
        worst = max(s.risk for s in steps)
        tail = " This can't be undone." if worst >= Risk.EXTERNAL_EFFECT else ""
        return f"{' '.join(parts)} I'm ready to {acts}.{tail} Do you want me to go ahead?".strip()

    # ---- one step ------------------------------------------------------------------------------------------------------------------------------

    def _execute(self, step: WStep, args: dict[str, Any]) -> None:
        wf = self.wf
        step.status = SStatus.RUNNING
        wf.pending_step = None
        self._update()
        key = f"browser:{wf.workflow_id}:{step.step_id}"
        guarded_browser = step.side_effect and step.tool not in SPECS
        if guarded_browser:
            prior = self.store.effect_lookup(key)
            if prior is not None:
                step.status, step.failure = SStatus.FAILED, FailureKind.VERIFICATION_FAILED
                step.note = "this may already have been done before a restart; I won't repeat it, please check"
                return
        board = {k: v for k, v in self.outputs.items()}
        board["_reached"] = sorted({x.source for x in wf.steps if x.status is SStatus.DONE and x.source not in ("local", "")})
        board["_missing"] = sorted({SYSTEM_LABEL.get(x.source, x.source) for x in wf.steps if x.status is SStatus.SKIPPED and x.source not in ("local", "") and x.optional}
                                   | {SYSTEM_LABEL.get(x, x) for x in wf.params.get("unavailable", [])})
        attempts = 0
        began = self._clock()
        while True:
            self._check_alive()
            attempts += 1
            wf.tool_calls += 1
            self.store.audit(wf.workflow_id, "step_start", step=step.step_id, tool=step.tool, source=step.source, risk=step.risk.name, attempt=attempts)
            if step.side_effect:
                with self.router.ctx.effects_lock:
                    if self.cancel_event.is_set():
                        step.status = SStatus.PENDING
                        raise _Stop(WStatus.CANCELLED, wf.cancel_reason or "stopped by the user", FailureKind.USER_CANCELLED)
                    if guarded_browser:
                        self.store.effect_begin(key, "browser", wf.workflow_id)
                    out = self.router.call(step.tool, args, workflow=wf, board=board, confirmed=step.step_id in self._confirmed)
                    if guarded_browser and out.ok:
                        self.store.effect_done(key, "browser", step.tool, wf.workflow_id)
            else:
                out = self.router.call(step.tool, args, workflow=wf, board=board, confirmed=False)
            step.attempts = attempts
            if not out.ok and out.failure is FailureKind.TEMPORARY and step.retry_policy.safe and attempts <= min(step.retry_policy.max_retries, self.cfg.max_retries):
                wf.retries += 1
                self._sleep(min(0.2 * attempts, 1.0))
                continue
            break
        metrics.observe("workflow.step_ms", (self._clock() - began) * 1000)
        self._record(step, out)

    def _record(self, step: WStep, out: ToolOut) -> None:
        wf = self.wf
        ok = out.ok and all(_rule_ok(r, out) for r in step.verification) and (out.verified or not step.side_effect)
        if out.ok and not ok:
            out = ToolOut(False, out.data, "I couldn't confirm that " + step.description.lower() + " worked.", FailureKind.VERIFICATION_FAILED)
        if ok:
            step.status, step.output, step.note = SStatus.DONE, out.data, out.message[:300]
            step.failure = None
            self.outputs[step.step_id] = out.data
            wf.warnings.extend(w for w in out.warnings if w not in wf.warnings)
            if out.data.get("fact") and step.tool in ("verify_deadline_fact", "calendar_compare_fact"):
                self._remember_fact(out.data["fact"])
            if step.tool == "gmail_extract_deadlines":
                for f in out.data.get("facts", []):
                    self._remember_fact(f)
            self.store.audit(wf.workflow_id, "step_done", step=step.step_id, tool=step.tool, source=step.source, verified=out.verified, reused=out.reused)
            return
        kind = out.failure or FailureKind.EXTERNAL_SERVICE
        step.failure, step.note = kind, out.message[:300]
        if step.optional and kind in OPTIONAL_TOLERATED:
            step.status = SStatus.SKIPPED
            wf.warnings.append(f"I couldn't use {SYSTEM_LABEL.get(step.source, step.source)}: {out.message.rstrip('.')}.")
            self.store.audit(wf.workflow_id, "step_optional_skipped", step=step.step_id, tool=step.tool, failure=kind.value)
            return
        if out.choices and kind is FailureKind.AMBIGUOUS:
            step.status = SStatus.FAILED
            wf.question, wf.choices = out.message, out.choices
            step.output = {k: v for k, v in out.data.items() if k in ("fact", "conflict", "choices")}
            wf.touch(WStatus.WAITING_FOR_USER)
            self.store.audit(wf.workflow_id, "step_needs_answer", step=step.step_id, tool=step.tool)
            return
        step.status = SStatus.FAILED
        step.output = {k: v for k, v in out.data.items() if k in ("fact",)}
        self.store.audit(wf.workflow_id, "step_failed", step=step.step_id, tool=step.tool, failure=kind.value)

    def _remember_fact(self, d: dict[str, Any]) -> None:
        from workflows.models import Fact

        try:
            f = Fact.from_dict(d)
        except (KeyError, ValueError):
            return
        if not any(x.source_id == f.source_id and x.value == f.value and x.title == f.title for x in self.wf.facts):
            self.wf.facts.append(f)

    # ---- ending ------------------------------------------------------------------------------------------------------------------------------

    def _stopped(self, stop: _Stop) -> None:
        wf = self.wf
        for s in wf.steps:
            if s.status in (SStatus.PENDING, SStatus.RUNNING):
                s.status, s.note = SStatus.SKIPPED, "stopped before it ran" if stop.status is WStatus.CANCELLED else "stopped"
        wf.failure, wf.failure_kind, wf.finished_at = stop.reason, stop.kind, time.time()
        wf.touch(stop.status)
        wf.result = self._result("cancelled" if stop.status is WStatus.CANCELLED else "failed")
        metrics.incr(f"workflow.{stop.status.value.lower()}")

    def _finish(self) -> None:
        wf = self.wf
        if wf.status is WStatus.WAITING_FOR_USER:
            return
        failed = [s for s in wf.steps if s.status is SStatus.FAILED]
        if not failed:
            wf.finished_at = time.time()
            wf.touch(WStatus.COMPLETED)
            wf.result = self._result("completed")
            metrics.incr("workflow.completed")
            return
        first = failed[0]
        wf.failure_kind = first.failure or FailureKind.EXTERNAL_SERVICE
        wf.failure = first.note or "That step didn't work."
        if wf.failure_kind in WAITING_KINDS and not any(s.status is SStatus.DONE and s.side_effect for s in wf.steps):
            wf.touch(WStatus.WAITING_FOR_DATA)         # nothing changed yet and the cause is external: it can be tried again
            wf.result = self._result("waiting")
            metrics.incr("workflow.waiting_for_data")
            return
        wf.finished_at = time.time()
        wf.touch(WStatus.FAILED)
        wf.result = self._result("failed")
        metrics.incr("workflow.failed")

    # ---- result --------------------------------------------------------------------------------------------------------------------------------

    def _result(self, state: str) -> WorkflowResult:
        wf = self.wf
        done = [s for s in wf.steps if s.status is SStatus.DONE]
        writes = [s for s in wf.steps if s.side_effect]
        completed = [s.note for s in done if s.side_effect and s.note]
        skipped = [f"{s.description}: {s.note}" if s.note else s.description for s in wf.steps if s.status is SStatus.SKIPPED]
        blocked = [f"{s.description} ({s.note})" for s in wf.steps if s.status is SStatus.BLOCKED]
        sources = sorted({SYSTEM_LABEL.get(s.source, s.source) for s in done if s.source and s.source != "local"})
        report = next((s for s in done if s.tool == "compose_workflow_report"), None)
        briefing = next((s for s in done if s.tool == "briefing_compose"), None)
        head_facts = " ".join(s.note for s in done if s.tool == "verify_deadline_fact" and s.note and s.note != "chosen by you")
        all_verified = all(s.status is SStatus.DONE for s in writes) if writes else True
        if state == "completed":
            if report is not None:
                summary = report.output.get("text") or report.note
                if completed and all_verified:
                    summary += " " + " ".join(completed)
            elif briefing is not None:
                summary = briefing.output.get("text") or briefing.note
            elif writes and all_verified:
                gated_ok = [s for s in writes if s.tool not in SPECS]
                body = " ".join(completed)
                summary = ("Done. " if not head_facts else f"{head_facts} ") + body if body else "Done."
                if gated_ok and not completed:
                    summary = "Done: " + ", ".join(s.description.lower() for s in gated_ok) + "."
            elif writes:
                summary = "I did part of that: " + (" ".join(completed) or "nothing was changed") + " " + " ".join(f"I skipped: {x}." for x in skipped[:2])
            else:
                summary = " ".join(s.note for s in done[-2:] if s.note) or "Done."
        elif state == "cancelled":
            summary = "Okay, I stopped." + (f" Already done: {' '.join(completed)}" if completed else " Nothing was changed.")
        elif state == "waiting":
            summary = f"{wf.failure.rstrip('.')}. Nothing was changed. Say 'try again' when it's back."
        else:
            first = next((s for s in wf.steps if s.status is SStatus.FAILED), None)
            reason = wf.failure or (first.note if first else "That didn't work.")
            summary = reason.rstrip(".") + "."
            if completed:
                summary += " Already done: " + " ".join(completed)
            if blocked:
                summary += " Not done because of that: " + "; ".join(b for b in blocked[:3]) + "."
        return WorkflowResult(state, summary.strip(), completed, skipped, blocked, sources, list(wf.warnings), round(self._clock() - self._began, 2),
                              [w for w in wf.warnings if w.startswith("Suggestion")])
