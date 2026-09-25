"""TaskRunner: the controlled loop that carries one task from a validated plan to completion, a question, a safe stop or an honest failure.

    pick the next step -> (skip it if its expected state already holds) -> resolve typed references -> risk gate (pause for the user's yes)
      -> ToolRouter.call -> observe -> verify (explicit checks + independent observation) -> store outputs -> next
    on failure: was it done anyway? -> ask the user (ambiguity) -> stop (needs the user: sign-in, CAPTCHA) -> retry (only safe steps) -> replan/fallback -> fail honestly

It never executes a pre-generated list blindly: every step starts from a fresh observation and every step ends with one. It never repeats an unsafe step
(submit, send, upload, delete, purchase, publish, download) after an error without first checking whether it already worked, and then only reports what it
verified. Limits (duration, steps, tool calls, retries, replans, consecutive failures, repeated identical actions) end a task safely.
"""

import queue
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from autonomy import analysis
from autonomy.models import Observation, Risk, Step, StepStatus, Task, TaskStatus
from autonomy.observe import Observer, Verifier
from autonomy.planner import Planner
from autonomy.toolrouter import ToolOutcome, ToolRouter, resolve
from backend.core.logging import get_logger
from backend.core.metrics import metrics

logger = get_logger(__name__)

LOOP_MESSAGE = "I couldn't complete that because the page isn't responding as expected."
NEEDS_YOU = ("login_required", "captcha")


@dataclass
class AutonomyConfig:
    enabled: bool = True
    max_duration_s: float = 180.0
    max_steps: int = 25
    max_tool_calls: int = 40
    max_retries: int = 2            # per step; also bounded task-wide by 2 * this
    max_replans: int = 3
    loop_threshold: int = 3
    observation_timeout_s: float = 10.0
    confirmation_timeout_s: float = 120.0
    browser_task_timeout_s: float = 60.0
    max_consecutive_failures: int = 3
    progress_notifications: bool = True
    inline_wait_s: float = 25.0
    history_size: int = 30
    still_working_after_s: float = 20.0


class TaskCancelled(Exception):
    pass


class TaskRunner:
    def __init__(self, task: Task, planner: Planner, router: ToolRouter, observer: Observer, verifier: Verifier, config: AutonomyConfig, *,
                 confirm: Callable[[Task, Step, str], str], notify: Callable[[str], None] | None = None, on_update: Callable[[Task], None] | None = None,
                 clock: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep):
        self.task, self._planner, self._router, self._observer, self._verifier, self._cfg = task, planner, router, observer, verifier, config
        self._confirm, self._notify, self._on_update, self._clock, self._sleep = confirm, notify, on_update, clock, sleep
        self.cancel_event = threading.Event()
        self.pause_event = threading.Event()
        self._answers: queue.Queue = queue.Queue()
        self._loop_counts: dict[tuple, int] = {}
        self._started = 0.0
        self._announced: set[str] = set()

    # ---- control (any thread) ----------------------------------------------------------------------------------------------------

    def cancel(self, reason: str = "stopped by the user") -> None:
        self.task.cancel_reason = self.task.cancel_reason or reason
        self.cancel_event.set()
        self._answers.put(None)

    def pause(self) -> bool:
        if self.task.terminal or self.task.status is TaskStatus.PAUSED:
            return False
        self.pause_event.set()
        return True

    def resume(self) -> bool:
        if not self.pause_event.is_set():
            return False
        self.pause_event.clear()
        return True

    def provide_answer(self, choice: int | None) -> None:
        self._answers.put(choice)

    # ---- the loop -------------------------------------------------------------------------------------------------------------------

    def run(self) -> Task:
        t = self.task
        self._started = self._clock()
        t.touch(TaskStatus.RUNNING)
        try:
            while True:
                self._check_stop()
                self._respect_pause()
                if self._limits_reached():
                    return self._end(TaskStatus.FAILED, self._limit_text)
                step = t.current
                if step is None:
                    return self._complete()
                self._run_step(step)
                self._update()
        except TaskCancelled:
            return self._end(TaskStatus.CANCELLED, t.cancel_reason or "stopped")
        except _Stop as stop:
            return self._end(stop.status, stop.reason)
        except Exception as exc:  # noqa: BLE001 - the loop never leaks an exception into the conversation
            logger.exception("Autonomous task crashed")
            return self._end(TaskStatus.FAILED, f"something unexpected went wrong ({type(exc).__name__})")

    def _check_stop(self) -> None:
        if self.cancel_event.is_set():
            raise TaskCancelled()

    def _respect_pause(self) -> None:
        if not self.pause_event.is_set():
            return
        self.task.touch(TaskStatus.PAUSED)
        self._update()
        while self.pause_event.is_set():
            self._check_stop()
            self._sleep(0.05)
        self.task.touch(TaskStatus.RUNNING)

    _limit_text = ""
    _step_ms = 0.0

    def _limits_reached(self) -> bool:
        t, c = self.task, self._cfg
        if self._clock() - self._started > c.max_duration_s:
            self._limit_text = f"it took longer than {int(c.max_duration_s)} seconds, so I stopped"
        elif t.executed_steps >= c.max_steps:
            self._limit_text = f"it needed more than {c.max_steps} steps, so I stopped"
        elif t.tool_calls >= c.max_tool_calls:
            self._limit_text = "it needed too many actions, so I stopped"
        elif t.consecutive_failures >= c.max_consecutive_failures:
            self._limit_text = "several steps in a row failed, so I stopped"
        else:
            return False
        metrics.incr("autonomy.limit_stops")
        return True

    # ---- one step ---------------------------------------------------------------------------------------------------------------------

    def _needs_media(self, step: Step) -> bool:
        return any(c.kind in ("playing", "volume") for c in step.verification) or step.tool.endswith("_youtube")

    def _run_step(self, step: Step) -> None:
        t = self.task
        step.status = StepStatus.RUNNING
        before = self._observer.observe(media=self._needs_media(step))
        if step.satisfied_when is not None and self._verifier.holds(step.satisfied_when, before, t.blackboard):
            step.status, step.note = StepStatus.SKIPPED, "already satisfied"
            t.record(step, True, True, "already satisfied: " + step.expected_state)
            return
        args, err = resolve(step.arguments, t.blackboard, self._router.allow_private)
        if err:
            return self._fail_step(step, ToolOutcome(False, error=err), "missing information")
        risk, perm = self._router.risk_of(step.tool, args, step.description)  # with the real values: a page can only RAISE the risk, never lower it
        if risk > step.risk:
            step.risk, step.permission = risk, perm
            t.risk_level = max(t.risk_level, risk)
        if step.risk.needs_confirmation and not step.confirmed:
            self._gate(step, args, before)
            self._check_stop()
        step_started = self._clock()
        outcome = self._router.call(step.tool, args, session_id=t.session_id, blackboard=t.blackboard, confirmed=step.confirmed)
        if outcome.needs_confirmation and not step.confirmed:  # the browser's own gate found something the words did not reveal
            step.risk = max(step.risk, Risk.EXTERNAL_EFFECT)
            self._gate(step, args, before, outcome.message)
            outcome = self._router.call(step.tool, args, session_id=t.session_id, blackboard=t.blackboard, confirmed=True)
        t.tool_calls += 1
        t.executed_steps += 1
        step.attempts += 1
        self._check_stop()
        t.touch(TaskStatus.VERIFYING)
        after = self._observer.observe_dialogs(self._observe_after(step), outcome)
        if outcome.success and outcome.ambiguous and outcome.data.get("candidates"):  # found several equally good matches: the user chooses
            self._ask(step, outcome)
            return
        verdict = self._verifier.verify(step, outcome, before, after, t.blackboard)
        duration = (self._clock() - step_started) * 1000
        t.touch(TaskStatus.RUNNING)
        if verdict.ok:
            self._store(step, outcome, after)
            step.status = StepStatus.DONE
            t.consecutive_failures = 0
            t.record(step, True, outcome.verified, "; ".join(verdict.changed) or step.expected_state, duration)
            self._progress(step)
            return
        t.record(step, False, False, verdict.reason, duration)
        self._step_ms = duration
        if not _transient(verdict.reason):  # a network hiccup is bounded by the retry limit and reported by its cause; an unresponsive page is a loop
            self._loop_check(step, args, after)
        self._recover(step, outcome, verdict.reason, before, after)

    def _observe_after(self, step: Step) -> Observation:
        """The observation after an action. Media state settles a moment after the tool returns (an ad or a buffering video), so for playback and volume checks the
        page is re-read (bounded by `observation_timeout_s`) until it matches or the time is up: verification looks at the real state, not at the tool's claim."""
        media = self._needs_media(step)
        obs = self._observer.observe(media=media)
        watched = [c for c in step.verification if c.kind in ("playing", "volume")]
        if not watched:
            return obs
        end = self._clock() + min(self._cfg.observation_timeout_s, 8.0)
        while not all(self._verifier.holds(c, obs, self.task.blackboard) for c in watched) and self._clock() < end:
            self._check_stop()
            self._sleep(0.3)
            obs = self._observer.observe(media=True)
        return obs

    # ---- permission gate ----------------------------------------------------------------------------------------------------------------

    def _gate(self, step: Step, args: dict[str, Any], obs: Observation, prompt: str = "") -> None:
        t = self.task
        where = f" on {obs.host}" if obs.host else ""
        effect = ("This can't easily be undone." if step.risk >= Risk.SENSITIVE else "This will change something outside JARVIS.")
        summary = prompt or f"Next I need to {step.description[:1].lower() + step.description[1:]}{where}. {effect} Shall I go ahead?"
        if t.blackboard.get("target_name") and step.tool == "click_element":
            summary = f"I found '{str(t.blackboard['target_name'])[:60]}'. " + summary
        t.touch(TaskStatus.WAITING_FOR_PERMISSION)
        t.question, t.pending_step = summary, step.id
        self._update()
        decision = self._confirm(t, step, summary)
        t.question, t.pending_step = None, None
        self._check_stop()
        if decision == "approved":
            step.confirmed = True  # only the user's own yes (via the ConfirmationEngine) gets here
            t.touch(TaskStatus.RUNNING)
            return
        if decision == "declined":
            raise _Stop(TaskStatus.CANCELLED, "you declined that step")
        raise _Stop(TaskStatus.BLOCKED, "I didn't get your confirmation in time, so I did not do it")

    # ---- recovery ----------------------------------------------------------------------------------------------------------------------------

    def _recover(self, step: Step, outcome: ToolOutcome, reason: str, before: Observation, after: Observation) -> None:
        t, c = self.task, self._cfg
        data = outcome.data or {}
        if any(data.get(k) for k in NEEDS_YOU) or after.captcha or (after.login_required and step.tool not in ("open_url",)):
            what = "a human check" if (data.get("captcha") or after.captcha) else "a sign-in"
            raise _Stop(TaskStatus.BLOCKED, f"the page needs {what}, which only you can complete; I didn't try to get past it")
        if outcome.ambiguous and data.get("candidates"):
            return self._ask(step, outcome)
        # was it done anyway? A failure report can hide a success (an error after the click landed, a crash after the load).
        if self._state_already_holds(step, after):
            step.status, step.note = StepStatus.DONE, "verified from the page state after an error"
            t.record(step, True, True, "the expected state holds after the error")
            t.consecutive_failures = 0
            self._store(step, ToolOutcome(True, True, data=data), after)
            return
        safe = step.retry_policy.safe and self._step_ms <= self._cfg.browser_task_timeout_s * 1000  # a step that already used its whole time budget is not repeated
        if safe and step.attempts <= min(step.retry_policy.max_retries, c.max_retries) and t.retries < 2 * c.max_retries and not _deterministic_failure(reason):
            t.retries += 1
            metrics.incr("autonomy.retries")
            step.status = StepStatus.PENDING
            self._sleep(0.2 * step.attempts)
            t.consecutive_failures += 1
            return
        if step.tool == "click_element" and not step.fallback and reason and "not found" in reason.lower():
            step.fallback = "retarget"
        if step.fallback and t.replans < c.max_replans:
            began = time.perf_counter()
            new = self._planner.expand_fallback(t, step, outcome, after)
            metrics.observe("autonomy.replan_ms", (time.perf_counter() - began) * 1000)
            if new:
                i = t.steps.index(step)
                step.status, step.note = StepStatus.SKIPPED, f"replaced: {reason[:80]}"
                t.steps[i + 1:i + 1] = new
                t.replans += 1
                t.consecutive_failures = 0
                metrics.incr("autonomy.replans")
                t.record(step, False, False, "replanned: " + (new[0].description if new else ""))
                return
        self._fail_step(step, outcome, reason)

    def _state_already_holds(self, step: Step, after: Observation) -> bool:
        """True if every state-based check of the step holds right now (the action worked; the tool call still reported a problem)."""
        state_kinds = {"url_host", "url_repo", "playing", "volume", "url_matches_board"}
        checks = [c for c in step.verification if c.kind in state_kinds]
        if not checks or step.tool in ("compose_report",):
            return False
        return all(self._verifier.holds(c, after, self.task.blackboard) for c in checks)

    def _ask(self, step: Step, outcome: ToolOutcome) -> None:
        """Ambiguity: put the candidates to the user, wait, apply the choice to THIS step, carry on (the same task, not a new one)."""
        t = self.task
        cands = outcome.data["candidates"][:5]
        names = [str(x.get("name") or x.get("title") or "")[:70] for x in cands]
        t.pending_choice = [{**{k: v for k, v in c.items() if k in ("url", "repo", "role", "official", "title")}, "n": i + 1, "orig_n": c.get("n", i + 1), "name": n}
                            for i, (n, c) in enumerate(zip(names, cands))]  # `n` is what the user hears; `orig_n` is the tool's own numbering
        t.question = f"I found {len(cands)} matches: " + "; ".join(f"{i + 1}, {n}" for i, n in enumerate(names)) + ". Which one do you mean?"
        t.pending_step = step.id
        step.status = StepStatus.WAITING
        t.touch(TaskStatus.WAITING_FOR_USER)
        self._update()
        self._notify_user(t.question)
        end = self._clock() + self._cfg.confirmation_timeout_s
        choice = None
        while True:
            self._check_stop()
            try:
                choice = self._answers.get(timeout=0.05)
                break
            except queue.Empty:
                self._sleep(0.0)
            if self._clock() > end:
                raise _Stop(TaskStatus.BLOCKED, "I didn't hear which one you meant, so I stopped")
        self._check_stop()
        if choice is None or not 1 <= choice <= len(t.pending_choice):
            raise _Stop(TaskStatus.CANCELLED, "no choice was made")
        picked = t.pending_choice[choice - 1]
        t.question, t.pending_choice, t.pending_step = None, [], None
        t.touch(TaskStatus.RUNNING)
        self._apply_choice(step, picked, choice)

    def _apply_choice(self, step: Step, picked: dict[str, Any], n: int) -> None:
        t, board = self.task, self.task.blackboard
        if step.tool == "play_youtube":
            step.arguments = {"choice": int(picked.get("orig_n", n))}
            step.status = StepStatus.PENDING
        elif step.tool in ("github_find_repo", "github_latest_repo") and picked.get("repo"):
            board["repo"], board["repo_url"] = picked["repo"], f"https://github.com/{picked['repo']}"
            step.status, step.note = StepStatus.DONE, "chosen by the user"
            t.record(step, True, True, f"you chose {picked['repo']}")
        elif step.tool == "find_element":
            board["target_name"], board["target_role"] = picked["name"], picked.get("role", "button")
            step.status, step.note = StepStatus.DONE, "chosen by the user"
            t.record(step, True, True, f"you chose {picked['name'][:40]}")
        elif step.tool == "pick_official_result" and picked.get("url"):
            board["official_url"], board["official_title"] = picked["url"], picked.get("title", "")
            step.status, step.note = StepStatus.DONE, "chosen by the user"
            t.record(step, True, True, "you chose the result")
        elif step.tool == "click_element":
            step.arguments = {**step.arguments, "index": n - 1}
            step.status = StepStatus.PENDING
        else:
            step.status = StepStatus.PENDING

    def _fail_step(self, step: Step, outcome: ToolOutcome, reason: str) -> None:
        step.status = StepStatus.FAILED
        self.task.consecutive_failures += 1
        raise _Stop(TaskStatus.FAILED, self._failure_text(step, reason or outcome.error))

    def _failure_text(self, step: Step, reason: str) -> str:
        t = self.task
        reason = (reason or "it didn't work").rstrip(".")
        if step.subgoal == "download" and step.tool == "click_element":
            found = str(t.blackboard.get("target_name", "the file"))[:60]
            return f"I found {found}, but the download failed: {reason}"
        done = [s for s in t.steps if s.status is StepStatus.DONE and s.tool != "compose_report"]
        so_far = f" So far I managed to {done[-1].description[:1].lower() + done[-1].description[1:]}." if done else ""
        return f"I couldn't {step.description[:1].lower() + step.description[1:]}: {reason}.{so_far}"

    # ---- loop detection ------------------------------------------------------------------------------------------------------------------

    def _loop_check(self, step: Step, args: dict[str, Any], after: Observation) -> None:
        key = (step.tool, repr(sorted((k, repr(v)[:80]) for k, v in args.items())), after.signature())
        self._loop_counts[key] = self._loop_counts.get(key, 0) + 1
        if self._loop_counts[key] >= self._cfg.loop_threshold:
            metrics.incr("autonomy.loop_stops")
            step.status = StepStatus.FAILED
            raise _Stop(TaskStatus.FAILED, LOOP_MESSAGE)

    # ---- outputs and completion -----------------------------------------------------------------------------------------------------------

    def _store(self, step: Step, outcome: ToolOutcome, after: Observation) -> None:
        t, b, d = self.task, self.task.blackboard, outcome.data or {}
        untrusted = t.untrusted_keys
        tool = step.tool
        if tool in ("github_find_repo", "github_latest_repo") and d.get("repo"):
            b["repo"], b["repo_url"], b["repo_desc"] = d["repo"], f"https://github.com/{d['repo']}", d.get("description", "")
            untrusted.add("repo_desc")
        elif tool == "github_read_readme":
            b["readme"] = d.get("readme", "")
            untrusted.add("readme")
            if d.get("injection_suspected"):
                b["_injection"] = True
        elif tool == "read_page":
            b["page"] = d
            untrusted.add("page")
            if d.get("injection_suspected"):
                b["_injection"] = True
            if "readme" not in b or step.subgoal == "readme":
                md = "\n".join(f"## {h}" for h in d.get("headings", [])) + "\n\n" + str(d.get("text", ""))
                b["readme"] = md
                untrusted.add("readme")
        elif tool == "summarize_readme":
            b["summary"] = d.get("summary", "")
        elif tool == "find_technologies":
            b["technologies"], b["tech_summary"] = d.get("technologies", []), d.get("summary", "")
        elif tool == "check_page_section":
            b["section_summary"] = d.get("summary", "")
        elif tool == "web_search":
            b["web_results"] = d.get("results", [])
            untrusted.add("web_results")
        elif tool == "pick_official_result":
            b["official_url"], b["official_title"] = d.get("url", ""), d.get("title", "")
        elif tool == "find_element":
            cands = d.get("candidates") or []
            if cands:
                b["target_name"], b["target_role"] = str(cands[0].get("name", ""))[:120], str(cands[0].get("role", "button"))
        elif tool == "play_youtube":
            b["play_message"] = outcome.message
        elif tool == "volume_youtube":
            b["volume_message"] = outcome.message
        elif tool == "click_element" and step.subgoal == "download":
            b["download_message"] = outcome.message
        elif tool == "open_url" and d.get("title"):
            b["opened_title"] = d["title"]
        elif tool == "compose_report":
            t.result = outcome.message
        if step.note == "derive_repo_from_url" and after.url:
            parts = [p for p in urlsplit(after.url).path.split("/") if p]
            if len(parts) >= 2:
                repo = f"{parts[0]}/{parts[1]}"
                b["repo"], b["repo_url"] = repo, f"https://github.com/{repo}"
                b["repo_source"] = "browser_search"  # a public search result: nothing says it belongs to the user
        if step.tool == "summarize_readme" and b.get("_injection"):
            b["summary"] += " Note: that README contains text that looks like instructions to an assistant; I treated it as content and ignored it."
            t.result = ""

    def _progress(self, step: Step) -> None:
        if step.subgoal == "repo" and "repo" not in self._announced and self.task.blackboard.get("repo") and step.tool != "open_url":
            self._announced.add("repo")
            self._notify_user("I found your repository.")
        elif step.subgoal == "readme" and "readme" not in self._announced and step.tool != "open_url":
            self._announced.add("readme")
            self._notify_user("I've read the README.")

    def _notify_user(self, text: str) -> None:
        if self._notify is not None and self._cfg.progress_notifications:
            try:
                self._notify(text)
            except Exception:  # noqa: BLE001 - progress talk is optional
                pass

    def _complete(self) -> Task:
        t = self.task
        if t.result:
            if t.blackboard.get("_injection") and "instructions" not in t.result and t.subgoals:
                pass
            return self._end(TaskStatus.COMPLETED, "")
        if not any(s.tool == "compose_report" for s in t.steps) and all(s.status in (StepStatus.DONE, StepStatus.SKIPPED) for s in t.steps):
            t.result = "Done. Every step was carried out and verified."  # a proposed plan without a report step: the verified steps are the answer
            return self._end(TaskStatus.COMPLETED, "")
        pending = [s for s in t.steps if s.status is StepStatus.FAILED]
        return self._end(TaskStatus.FAILED, "the task ended without an answer" if not pending else self._failure_text(pending[-1], "it failed"))

    def _end(self, status: TaskStatus, reason: str) -> Task:
        t = self.task
        t.finished_at = time.time()
        if status is TaskStatus.COMPLETED:
            for g in t.subgoals:
                g.done = True
        elif status is TaskStatus.CANCELLED:
            t.cancel_reason = t.cancel_reason or reason
            t.result = ""
        else:
            t.failure = reason
        t.touch(status)
        metrics.incr(f"autonomy.tasks.{status.value.lower()}")
        metrics.observe("autonomy.task_ms", (t.finished_at - t.started_at) * 1000)
        self._update()
        return t

    def _update(self) -> None:
        if self._on_update is not None:
            try:
                self._on_update(self.task)
            except Exception:  # noqa: BLE001
                pass


class _Stop(Exception):
    def __init__(self, status: TaskStatus, reason: str):
        super().__init__(reason)
        self.status, self.reason = status, reason


def _transient(reason: str) -> bool:
    r = (reason or "").lower()
    return any(k in r for k in ("too long", "couldn't connect", "doesn't resolve", "couldn't be loaded", "reported an error", "went away"))


def _deterministic_failure(reason: str) -> bool:
    """Failures that repeating cannot fix (refused, not allowed, invalid, missing)."""
    r = (reason or "").lower()
    return any(k in r for k in ("isn't allowed", "won't", "not allowed", "invalid", "refuse", "only open", "can't", "isn't valid", "no results", "not found", "couldn't find", "missing"))
