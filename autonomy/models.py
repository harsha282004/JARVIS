"""Data model of the autonomous task system: tasks, steps, sub-goals, checks, observations, verdicts, risk.

Everything here is plain data. A step is a *proposal*: its tool must exist in the router's registry, its arguments must pass that tool's
schema, and its risk and permission are computed by code (never taken from the plan). Text that came from a web page, a README or a search
result is only ever stored on the blackboard as data; it can fill an argument only through a typed, validated reference (`Ref`).
"""

import time
import uuid
from dataclasses import dataclass, field
from enum import IntEnum, StrEnum
from typing import Any


class TaskStatus(StrEnum):
    PLANNING = "PLANNING"
    WAITING_FOR_PERMISSION = "WAITING_FOR_PERMISSION"
    RUNNING = "RUNNING"
    WAITING_FOR_USER = "WAITING_FOR_USER"
    VERIFYING = "VERIFYING"
    PAUSED = "PAUSED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    BLOCKED = "BLOCKED"


TERMINAL = frozenset({TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED, TaskStatus.BLOCKED})


class Risk(IntEnum):
    """Ordered. A task carries the highest risk of any of its steps, whatever order they run in."""

    READ_ONLY = 0
    LOW_RISK = 1
    EXTERNAL_EFFECT = 2   # downloads, submits, posts: something outside JARVIS changes
    SENSITIVE = 3         # purchases, uploads, account or security changes, granting access
    DESTRUCTIVE = 4       # deleting or removing

    @property
    def needs_confirmation(self) -> bool:
        return self >= Risk.EXTERNAL_EFFECT


class StepStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    WAITING = "waiting"
    DONE = "done"
    SKIPPED = "skipped"   # its expected state already held
    FAILED = "failed"


@dataclass(frozen=True)
class Ref:
    """A typed reference to an earlier step's output on the blackboard (never free text from a page)."""

    key: str
    kind: str = "text"   # text | repo | url | int


@dataclass(frozen=True)
class Check:
    """An explicit success condition. `kind` selects the verifier rule; `params` are its parameters."""

    kind: str
    params: tuple[tuple[str, Any], ...] = ()

    def get(self, name: str, default: Any = None) -> Any:
        return dict(self.params).get(name, default)

    def describe(self) -> str:
        return f"{self.kind}({', '.join(f'{k}={v}' for k, v in self.params)})" if self.params else self.kind


def check(kind: str, **params: Any) -> Check:
    return Check(kind, tuple(sorted(params.items())))


@dataclass
class RetryPolicy:
    max_retries: int = 0
    safe: bool = False    # a step may be repeated only if repeating it cannot do harm (loading, reading, finding)


@dataclass
class Step:
    id: str
    description: str
    tool: str
    arguments: dict[str, Any] = field(default_factory=dict)
    expected_state: str = ""
    verification: list[Check] = field(default_factory=list)
    retry_policy: RetryPolicy = field(default_factory=RetryPolicy)
    satisfied_when: Check | None = None      # if this already holds (from the current browser state), the step is skipped
    output_key: str | None = None            # where its data goes on the blackboard
    subgoal: str | None = None
    fallback: str | None = None              # name of a replan rule to try when the step cannot be completed
    # computed by code (Planner/validator), never by the proposer:
    permission: str = ""
    risk: Risk = Risk.READ_ONLY
    status: StepStatus = StepStatus.PENDING
    attempts: int = 0
    note: str = ""
    confirmed: bool = False                  # set only by the runner after the user's own yes


@dataclass
class SubGoal:
    id: str
    description: str
    completion: Check
    done: bool = False


@dataclass
class Observation:
    """Only what the current task needs: the browser's address/title/tabs, and playback when a media step is involved."""

    url: str = ""
    host: str = ""
    netloc: str = ""     # host:port, so two local servers (or a site and its look-alike port) are not confused
    title: str = ""
    tab_count: int = 0
    active_tab: str | None = None
    browser_state: str = "closed"
    playing: bool | None = None
    volume: float | None = None
    ad: bool | None = None
    dialog: bool = False
    login_required: bool = False
    captcha: bool = False
    at: float = 0.0

    def signature(self) -> str:
        return f"{self.url}|{self.title}|{self.tab_count}|{self.playing}|{self.volume}|{self.dialog}"

    def diff(self, other: "Observation") -> list[str]:
        """What changed between this (before) and `other` (after)."""
        out = []
        for name in ("url", "title", "tab_count", "active_tab", "playing", "volume", "dialog", "login_required", "captcha", "browser_state"):
            if getattr(self, name) != getattr(other, name):
                out.append(name)
        return out


@dataclass
class Verdict:
    ok: bool
    reason: str = ""
    changed: list[str] = field(default_factory=list)


@dataclass
class HistoryEntry:
    index: int
    description: str
    tool: str
    ok: bool
    verified: bool
    note: str
    at: float
    duration_ms: float = 0.0


@dataclass
class Task:
    goal: str
    session_id: str = ""
    task_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    status: TaskStatus = TaskStatus.PLANNING
    steps: list[Step] = field(default_factory=list)
    subgoals: list[SubGoal] = field(default_factory=list)
    current_step: int = 0
    started_at: float = field(default_factory=time.time)
    last_updated: float = field(default_factory=time.time)
    finished_at: float | None = None
    risk_level: Risk = Risk.READ_ONLY
    blackboard: dict[str, Any] = field(default_factory=dict)
    untrusted_keys: set[str] = field(default_factory=set)
    history: list[HistoryEntry] = field(default_factory=list)
    question: str | None = None               # what JARVIS is waiting to be told (WAITING_FOR_USER)
    pending_choice: list[dict[str, Any]] = field(default_factory=list)
    pending_step: str | None = None
    result: str = ""
    failure: str = ""
    cancel_reason: str = ""
    preview: str = ""
    tool_calls: int = 0
    replans: int = 0
    retries: int = 0
    consecutive_failures: int = 0
    executed_steps: int = 0
    ack: str = ""

    def touch(self, status: TaskStatus | None = None) -> None:
        if status is not None:
            self.status = status
        self.last_updated = time.time()

    @property
    def done_steps(self) -> int:
        return sum(1 for s in self.steps if s.status in (StepStatus.DONE, StepStatus.SKIPPED))

    @property
    def current(self) -> Step | None:
        for s in self.steps:
            if s.status in (StepStatus.PENDING, StepStatus.RUNNING, StepStatus.WAITING):
                return s
        return None

    @property
    def terminal(self) -> bool:
        return self.status in TERMINAL

    def record(self, step: Step, ok: bool, verified: bool, note: str, duration_ms: float = 0.0) -> None:
        self.history.append(HistoryEntry(len(self.history) + 1, step.description, step.tool, ok, verified, note[:160], time.time(), duration_ms))
        del self.history[:-60]

    def summary(self) -> dict[str, Any]:
        """Safe to show anywhere (dashboard, tray, logs): no blackboard, no page text."""
        cur = self.current
        return {"task_id": self.task_id, "goal": self.goal[:200], "status": self.status.value, "progress": [self.done_steps, len(self.steps)],
                "current_action": cur.description if cur else "", "verified": bool(self.history and self.history[-1].verified) if self.history else None,
                "risk": self.risk_level.name, "question": self.question, "result": self.result[:400], "failure": self.failure[:300],
                "started_at": self.started_at, "finished_at": self.finished_at, "last_action": self.history[-1].description if self.history else "",
                "steps": [{"description": s.description, "status": s.status.value, "risk": s.risk.name} for s in self.steps]}
