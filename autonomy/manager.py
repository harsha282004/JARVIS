"""AutonomyManager (one task at a time, its history, user controls) and AutonomyRouter (the conversational front door).

The manager owns the active task's thread, the confirmation hand-off (the shared Phase 17 ConfirmationEngine: strict yes/no, single use, bound to the exact
step), clarifying questions ("Which one do you mean?" resumes THIS task), pause/resume/cancel, task limits, the short-lived history (redacted summaries only:
no page text, no blackboard, no credentials) and shutdown (an active task is cancelled and marked, never resumed by itself).

The router recognises: answers to a waiting task, control phrases ("stop the task", "pause", "resume", "what are you doing?"), and new goals. Single
browser commands are left to the Phase 20 router; multi-step and research goals become tasks.
"""

import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agent.intelligence.confirmation import ActionReport
from autonomy.models import Risk, Task, TaskStatus, TERMINAL
from autonomy.observe import Observer, Verifier
from autonomy.planner import PlanContext, Planner
from autonomy.runner import AutonomyConfig, TaskRunner
from autonomy.toolrouter import ToolRouter
from backend.core.logging import get_logger
from backend.core.metrics import metrics
from backend.core.redaction import redact
from backend.core.security.approval import ApprovalClass
from backend.core.state_store import JsonFile

logger = get_logger(__name__)

_ORD = {"first": 1, "1st": 1, "second": 2, "2nd": 2, "third": 3, "3rd": 3, "fourth": 4, "4th": 4, "fifth": 5, "5th": 5}  # "one" is not here: "the acoustic one" is not "number one"
_CANCEL = re.compile(r"^(?:stop|cancel|abort|halt|never ?mind|forget it)(?: (?:the |that |this )?(?:task|it|that|everything|what you(?:'re| are) doing))?$|^(?:stop|cancel|abort) (?:the |that |this )?(?:task|job|operation)$", re.I)
_PAUSE = re.compile(r"^(?:pause|hold)(?: (?:the |that |this )?(?:task|job|work))$|^pause the task$", re.I)
_RESUME = re.compile(r"^(?:resume|continue|carry on|go on|keep going)(?: (?:the |that )?(?:task|job|work))?$", re.I)
_STATUS = re.compile(r"^(?:what(?:'s| are| is) (?:you|jarvis) doing|what are you doing|(?:what(?:'s| is) )?(?:the )?(?:task )?status|how(?:'s| is) (?:it|the task) going|are you (?:still )?working|what(?:'s| is) the current task)$", re.I)
_WHY = re.compile(r"^why did (?:that|it|the task) (?:fail|stop)|^what went wrong$|^why (?:did you stop|couldn't you)", re.I)


@dataclass
class Reply:
    text: str
    task: Task | None = None


class AutonomyManager:
    def __init__(self, planner: Planner, router: ToolRouter, observer: Observer, config: AutonomyConfig, *, confirmations=None, announce: Callable[[str, str], None] | None = None,
                 context_provider: Callable[[], PlanContext] | None = None, browser_stop: Callable[[], None] | None = None, history_path: Path | None = None,
                 clock: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep, verifier: Verifier | None = None, threaded: bool = True):
        self.planner, self.router, self.observer, self.cfg = planner, router, observer, config
        self._confirmations = confirmations
        self._announce = announce
        self._context = context_provider
        self._browser_stop = browser_stop
        self._clock, self._sleep, self._threaded = clock, sleep, threaded
        self._verifier = verifier or Verifier()
        self._file = JsonFile(history_path, []) if history_path is not None else None
        self._lock = threading.RLock()
        self._start_lock = threading.RLock()
        self.active: Task | None = None
        self.runner: TaskRunner | None = None
        self._thread: threading.Thread | None = None
        self._recent: list[Task] = []
        self._clarify: dict[str, Any] | None = None
        self.last_repo: str | None = None
        self._approved: dict[str, bool] = {}
        self._history = self._load_history()

    # ---- observation ----------------------------------------------------------------------------------------------------------------

    def _load_history(self) -> list[dict[str, Any]]:
        if self._file is None:
            return []
        data = self._file.read()
        return [d for d in data if isinstance(d, dict)][-self.cfg.history_size:]

    def current(self) -> Task | None:
        with self._lock:
            return self.active if self.active is not None and not self.active.terminal else None

    def last(self) -> Task | None:
        with self._lock:
            return self.active

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            cur = self.active
            return {"enabled": self.cfg.enabled, "current": cur.summary() if cur is not None else None, "history": list(reversed(self._history[-self.cfg.history_size:])),
                    "limits": {"max_duration_s": self.cfg.max_duration_s, "max_steps": self.cfg.max_steps, "max_tool_calls": self.cfg.max_tool_calls, "loop_threshold": self.cfg.loop_threshold},
                    "clarifying": bool(self._clarify)}

    def awaiting_user(self) -> bool:
        t = self.current()
        return bool((t is not None and t.status is TaskStatus.WAITING_FOR_USER) or self._clarify)

    # ---- starting a task -------------------------------------------------------------------------------------------------------------

    def start(self, goal: str, session_id: str, *, known: dict[str, str] | None = None) -> Reply | None:
        """A Reply if `goal` is ours (a plan, a question, a refusal); None if it is not an autonomous goal (let the Phase 20 router handle it)."""
        if not self.cfg.enabled:
            return None
        with self._start_lock:  # one task at a time, even if two callers (voice and API) race to start one
            return self._start(goal, session_id, known)

    def _start(self, goal: str, session_id: str, known: dict[str, str] | None) -> Reply | None:
        cur = self.current()
        if cur is not None:
            return Reply(f"I'm still working on: {cur.goal[:80]}. Say stop to cancel it, or wait until I'm done.", cur)
        ctx = self._plan_context()
        if known:
            ctx.known.update(known)
        outcome = self.planner.plan(goal, ctx, session_id)
        if outcome.kind == "none":
            return None
        if outcome.kind == "refuse":
            self._remember_failed(goal, outcome.reason)
            return Reply(outcome.reason)
        if outcome.kind == "clarify":
            self._clarify = {"goal": goal, "missing": outcome.missing, "session": session_id, "known": dict(known or {})}
            return Reply(outcome.question)
        task = outcome.task
        assert task is not None
        self._clarify = None
        if ctx.last_repo:  # a repository found earlier in the conversation is context, not something to ask again
            task.blackboard.setdefault("repo", ctx.last_repo)
            task.blackboard.setdefault("repo_url", f"https://github.com/{ctx.last_repo}")
        return self._launch(task)

    def _plan_context(self) -> PlanContext:
        ctx = self._context() if self._context is not None else PlanContext()
        ctx.last_repo = ctx.last_repo or self.last_repo
        ctx.github_available = self.router.available("github_find_repo")[0]
        ctx.browser_available = self.router.available("open_url")[0]
        return ctx

    def _launch(self, task: Task) -> Reply:
        runner = TaskRunner(task, self.planner, self.router, self.observer, self._verifier, self.cfg, confirm=self._confirm, notify=lambda text: self._say(text, "normal"),
                            on_update=self._on_update, clock=self._clock, sleep=self._sleep)
        with self._lock:
            self.active, self.runner = task, runner
        metrics.incr("autonomy.tasks.started")
        if not self._threaded:
            runner.run()
            self._finish(task)
            return Reply(self._final_text(task), task)
        task.blackboard["_inline_open"] = True
        self._thread = threading.Thread(target=self._run_thread, args=(runner,), name="jarvis-autonomy", daemon=True)
        self._thread.start()
        try:
            return self._inline(task)
        finally:
            task.blackboard["_inline_open"] = False

    def _run_thread(self, runner: TaskRunner) -> None:
        runner.run()
        self._finish(runner.task)
        for _ in range(100):  # the caller may still be waiting inline for this very result: let it take it
            if not runner.task.blackboard.get("_inline_open"):
                break
            time.sleep(0.01)
        if not runner.task.blackboard.get("_delivered"):
            self._say(self._final_text(runner.task), "high")

    def _inline(self, task: Task) -> Reply:
        """Wait a while for a quick task; otherwise hand back the acknowledgement (or a question the task is waiting on) and deliver the result later."""
        preview = ("\n" + task.preview) if task.preview else ""
        deadline = self._clock() + self.cfg.inline_wait_s
        started = time.monotonic()
        while time.monotonic() - started < min(self.cfg.inline_wait_s, 3600):
            if task.terminal or task.status in (TaskStatus.WAITING_FOR_USER, TaskStatus.WAITING_FOR_PERMISSION):
                break
            time.sleep(0.02)
        _ = deadline
        if task.terminal:
            task.blackboard["_delivered"] = True
            return Reply(self._final_text(task), task)
        if task.status in (TaskStatus.WAITING_FOR_USER, TaskStatus.WAITING_FOR_PERMISSION):
            task.blackboard["_asked"] = True
            head = (task.ack + preview + "\n") if task.preview else ""
            return Reply((head + (task.question or "")).strip(), task)
        return Reply((task.ack + preview + " I'm working on it and will tell you when I'm done.").strip(), task)

    def _final_text(self, task: Task) -> str:
        if task.status is TaskStatus.COMPLETED:
            return task.result or "Done."
        if task.status is TaskStatus.CANCELLED:
            return "Okay, I stopped." if not task.cancel_reason or "user" in task.cancel_reason else f"I stopped: {task.cancel_reason}."
        return (task.failure or "That didn't work.").rstrip(".") + "."

    # ---- confirmation hand-off (shared ConfirmationEngine) -----------------------------------------------------------------------------

    def _confirm(self, task: Task, step, summary: str) -> str:
        cls = ApprovalClass.SENSITIVE_DESKTOP if step.risk >= Risk.SENSITIVE else ApprovalClass.EXTERNAL_MESSAGE
        key = f"{task.task_id}:{step.id}"
        self._approved[key] = False

        def approve() -> ActionReport:
            self._approved[key] = True
            return ActionReport(True, "Okay, continuing.", True)

        if self._confirmations is None:
            return "declined"  # no confirmation channel: consequential steps never run
        params = {"task": task.task_id, "step": step.id, "tool": step.tool}
        self._confirmations.request(action_class=cls, tool="autonomy.step", summary=summary, params=params, run=approve, session_id=task.session_id, source="autonomy")
        task.blackboard["_asked"] = True
        self._say(summary, "high")
        end = self._clock() + self.cfg.confirmation_timeout_s
        runner = self.runner
        while True:
            if runner is not None and runner.cancel_event.is_set():
                self._confirmations.cancel(task.session_id)
                return "cancelled"
            if self._approved.get(key):
                self._approved.pop(key, None)
                return "approved"
            if not self._confirmations.has_pending(task.session_id):
                return "approved" if self._approved.pop(key, False) else "declined"
            if self._clock() > end:
                self._confirmations.cancel(task.session_id)
                return "timeout"
            self._sleep(0.05)

    # ---- answers to a waiting task / controls ----------------------------------------------------------------------------------------------

    def respond(self, text: str, session_id: str) -> Reply | None:
        """The user's reply while a task is waiting for a choice, or while a clarifying question is open. None if it is not an answer."""
        t = text.strip().strip(".!?")
        if self._clarify is not None:
            return self._answer_clarification(t, session_id)
        cur = self.current()
        if cur is None or cur.status is not TaskStatus.WAITING_FOR_USER or self.runner is None:
            return None
        if _CANCEL.match(t):
            return Reply(self.cancel("stopped by the user"))
        n = self._pick(t, cur.pending_choice)
        if n is None:
            return Reply(cur.question or "Which one do you mean?")
        self.runner.provide_answer(n)
        chosen = cur.pending_choice[n - 1]["name"] if 0 < n <= len(cur.pending_choice) else ""
        return Reply(f"Okay, {chosen[:60]}.")

    @staticmethod
    def _pick(text: str, choices: list[dict[str, Any]]) -> int | None:
        low = text.lower()
        m = re.search(r"\b(?:number |option |result |the )?(\d)\b", low)
        if m and 1 <= int(m.group(1)) <= len(choices):
            return int(m.group(1))
        for word, n in _ORD.items():
            if re.search(rf"\b{word}\b", low) and n <= len(choices):
                return n
        if "official" in low:
            official = [c for c in choices if c.get("official")]
            if len(official) == 1:
                return int(official[0]["n"])
        words = [w for w in re.findall(r"[a-z0-9]+", low) if len(w) > 2 and w not in {"the", "one", "that", "this", "report", "please", "want", "need", "mean"}]
        if words:
            hits = [c for c in choices if all(w in c["name"].lower() for w in words)] or [c for c in choices if any(w in c["name"].lower() for w in words)]
            if len(hits) == 1:
                return int(hits[0]["n"])
        return None

    def _answer_clarification(self, text: str, session_id: str) -> Reply | None:
        c = self._clarify
        assert c is not None
        if _CANCEL.match(text):
            self._clarify = None
            return Reply("Okay, cancelled.")
        slot, goal = c["missing"], c["goal"]
        answer = text.strip()
        looks_new = bool(re.match(r"^(?:open|find|search|play|read|summari[sz]e|go|check|what|show|tell|stop|download|upload|pause|resume|set)", answer, re.I))
        if not answer or len(answer.split()) > 6 or looks_new:
            self._clarify = None
            return None  # this is a new request, not the answer
        if slot == "portfolio_url":
            return self._resume_goal(goal, session_id, {**c["known"], "portfolio_url": answer})
        prefix = {"youtube_query": "search youtube for ", "search_query": "search the web for ", "site": "open ", "repo_name": "find my {} repository, "}.get(slot, "")
        new_goal = (prefix.format(answer) if "{}" in prefix else prefix + answer) + ", " + goal
        return self._resume_goal(new_goal, session_id, c["known"])

    def _resume_goal(self, goal: str, session_id: str, known: dict[str, str]) -> Reply:
        self._clarify = None
        reply = self.start(goal, session_id, known=known)
        return reply or Reply("I'm not sure how to do that.")

    def cancel(self, reason: str = "stopped by the user") -> str:
        """Stop the active task (or drop a clarifying question). Nothing further is executed; completed safe actions are not undone."""
        self._clarify = None
        cur = self.current()
        if cur is None or self.runner is None:
            return "There's nothing running to stop."
        self.runner.cancel(reason)
        if self._browser_stop is not None:
            try:
                self._browser_stop()
            except Exception:  # noqa: BLE001
                pass
        if self._confirmations is not None:
            self._confirmations.cancel(cur.session_id)
        metrics.incr("autonomy.cancellations")
        if self._thread is not None and threading.current_thread() is not self._thread:
            self._thread.join(timeout=3.0)
        return "Okay, I stopped the task."

    def pause(self) -> str:
        cur = self.current()
        if cur is None or self.runner is None or not self.runner.pause():
            return "There's no running task to pause."
        return "Paused. Say resume to carry on."

    def resume(self) -> str:
        if self.runner is None or not self.runner.resume():
            return "There's no paused task."
        return "Resuming."

    def describe(self) -> str:
        t = self.last()
        if t is None:
            return "I'm not doing anything right now."
        s = t.summary()
        if not t.terminal:
            extra = f" I'm waiting for you: {t.question}" if t.question else ""
            return f"I'm working on: {t.goal[:100]}. {s['progress'][0]} of {s['progress'][1]} steps done" + (f", now: {s['current_action']}" if s["current_action"] else "") + f".{extra}"
        return f"My last task was: {t.goal[:100]}. " + self._final_text(t)

    def why_failed(self) -> str:
        t = self.last()
        if t is None or t.status not in (TaskStatus.FAILED, TaskStatus.BLOCKED):
            return "Nothing has failed."
        return self._final_text(t)

    # ---- bookkeeping ---------------------------------------------------------------------------------------------------------------------------

    def _say(self, text: str, priority: str) -> None:
        cur = self.active
        if cur is not None and cur.blackboard.get("_inline_open") and text and text == cur.question:
            return  # the caller waiting inline is about to speak this very question
        if self._announce is not None and text:
            try:
                self._announce(text, priority)
            except Exception:  # noqa: BLE001
                pass

    def _on_update(self, task: Task) -> None:
        pass

    def _finish(self, task: Task) -> None:
        with self._lock:
            if task.blackboard.get("repo"):
                self.last_repo = task.blackboard["repo"]
            entry = {"task_id": task.task_id, "goal": redact(task.goal)[:160], "status": task.status.value, "at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                     "duration_s": round((task.finished_at or time.time()) - task.started_at, 1), "steps": [task.done_steps, len(task.steps)], "risk": task.risk_level.name,
                     "outcome": redact(task.result or task.failure or task.cancel_reason)[:200]}
            self._history.append(entry)
            del self._history[:-self.cfg.history_size]
            if self._file is not None:
                try:
                    self._file.write(self._history)
                except OSError:
                    pass
            self._recent.append(task)
            del self._recent[:-5]

    def _remember_failed(self, goal: str, reason: str) -> None:
        with self._lock:
            self._history.append({"task_id": "", "goal": redact(goal)[:160], "status": "FAILED", "at": time.strftime("%Y-%m-%dT%H:%M:%S"), "duration_s": 0.0, "steps": [0, 0],
                                  "risk": "READ_ONLY", "outcome": redact(reason)[:200]})
            del self._history[:-self.cfg.history_size]

    def shutdown(self) -> None:
        """Application exit: stop future actions, mark the task, never resume it on the next start."""
        cur = self.current()
        if cur is not None and self.runner is not None:
            self.runner.cancel("JARVIS was shutting down")
            if self._browser_stop is not None:
                try:
                    self._browser_stop()
                except Exception:  # noqa: BLE001
                    pass
            if self._thread is not None:
                self._thread.join(timeout=5.0)
        with self._lock:
            if cur is not None and not cur.terminal:
                cur.cancel_reason = "JARVIS was shutting down"
                cur.finished_at = time.time()
                cur.touch(TaskStatus.CANCELLED)
                self._finish(cur)


class AutonomyRouter:
    """Recognises autonomous goals and task controls inside a conversation. Plugged into IntelligenceRouter ahead of the Hub and Browser routers."""

    def __init__(self, manager: AutonomyManager):
        self._m = manager

    def handle(self, t: str, original: str, session_id: str) -> str | None:
        m = self._m
        try:
            if m.awaiting_user():
                answered = m.respond(original, session_id)
                if answered is not None:
                    return answered.text
            cur = m.current()
            text = re.sub(r"^(?:hey |hi |ok |okay )?jarvis[, ]*", "", original.strip(), flags=re.I).strip(" .!?")
            if cur is not None:
                if _CANCEL.match(text):
                    return m.cancel()
                if _PAUSE.match(text):
                    return m.pause()
            if _RESUME.match(text) and m.runner is not None and m.runner.pause_event.is_set():
                return m.resume()
            if _STATUS.match(text) and m.last() is not None:
                return m.describe()
            if _WHY.match(text) and m.last() is not None:
                return m.why_failed()
            if not m.cfg.enabled:
                return None
            reply = m.start(original, session_id)
            return reply.text if reply is not None else None
        except Exception as exc:  # noqa: BLE001 - the conversation must survive
            logger.error("Autonomy request failed (%s)", type(exc).__name__)
            return "Sorry, I couldn't carry that task out."

    # used by the conversation/voice layer
    def cancel_task(self) -> bool:
        if self._m.current() is None and not self._m.awaiting_user():
            return False
        self._m.cancel()
        return True

    def task_active(self) -> bool:
        return self._m.current() is not None
