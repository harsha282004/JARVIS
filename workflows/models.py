"""Data model of the Personal Operator: workflows, steps, facts with provenance, failure kinds, results.

Plain data only. A workflow is a directed acyclic graph of steps; a step is a PROPOSAL (tool name + typed arguments + dependencies) that the validator checks and the
runner executes through the tool router. Risk and permission are computed by code. Values that flow between steps are structured objects (`Fact`, ids, ISO dates),
referenced with `From(step, path)`; free text from an email, page or document is never an argument.
"""

import time
import uuid
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from autonomy.models import RetryPolicy, Risk


class WStatus(StrEnum):
    DRAFT = "DRAFT"
    PLANNING = "PLANNING"
    READY = "READY"
    RUNNING = "RUNNING"
    WAITING_FOR_DATA = "WAITING_FOR_DATA"
    WAITING_FOR_USER = "WAITING_FOR_USER"
    WAITING_FOR_CONFIRMATION = "WAITING_FOR_CONFIRMATION"
    PAUSED = "PAUSED"
    FAILED = "FAILED"
    COMPLETED = "COMPLETED"
    CANCELLED = "CANCELLED"


TERMINAL = frozenset({WStatus.FAILED, WStatus.COMPLETED, WStatus.CANCELLED})


class SStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    SKIPPED = "skipped"     # not needed (already exists, or a suggestion-only run) or its inputs are unavailable and it is optional
    BLOCKED = "blocked"     # an upstream step it depends on failed
    FAILED = "failed"


class FailureKind(StrEnum):
    TEMPORARY = "TEMPORARY"
    AUTHENTICATION = "AUTHENTICATION"
    PERMISSION = "PERMISSION"
    DATA_MISSING = "DATA_MISSING"
    AMBIGUOUS = "AMBIGUOUS"
    VERIFICATION_FAILED = "VERIFICATION_FAILED"
    EXTERNAL_SERVICE = "EXTERNAL_SERVICE"
    SECURITY_BLOCK = "SECURITY_BLOCK"
    USER_CANCELLED = "USER_CANCELLED"
    RESOURCE_LIMIT = "RESOURCE_LIMIT"


class FactStatus(StrEnum):
    VERIFIED = "VERIFIED"                  # stated explicitly and unambiguously, from a trusted read, and (where available) consistent with other systems
    HIGH_CONFIDENCE = "HIGH_CONFIDENCE"
    LOW_CONFIDENCE = "LOW_CONFIDENCE"
    AMBIGUOUS = "AMBIGUOUS"                # hedged or conflicting
    UNVERIFIED = "UNVERIFIED"              # from content that looks like an injection attempt, or without evidence


ACTIONABLE_STATUSES = frozenset({FactStatus.VERIFIED, FactStatus.HIGH_CONFIDENCE})


@dataclass(frozen=True)
class From:
    """A typed reference to an earlier step's output: `From("s3", "fact.date")`. Only steps listed in `dependencies` may be referenced."""

    step: str
    path: str = ""
    optional: bool = False     # an unavailable optional source resolves to an empty list instead of skipping the step (the briefing degrades, it isn't cancelled)
    fallback: "From | None" = None   # used when this step produced nothing (it was optional and unavailable)


@dataclass
class Fact:
    """One extracted personal fact and where it came from. Uncertain extractions are never promoted to plain facts."""

    name: str                 # "deadline", "event", ...
    value: str                # ISO date/time
    title: str
    status: FactStatus
    source: str               # gmail | calendar | github | documents | memory | task
    source_id: str            # message id, event id, file path, ...
    timestamp: str | None = None
    original_text: str = ""   # the sentence it came from (sanitized, short); NEVER persisted in checkpoints
    confidence: str = "medium"
    notes: list[str] = field(default_factory=list)

    def provenance(self) -> dict[str, Any]:
        return {"source": self.source, "source_id": self.source_id, "timestamp": self.timestamp, "confidence": self.confidence, "status": self.status.value}

    def to_dict(self, with_text: bool = True) -> dict[str, Any]:
        d = {"name": self.name, "value": self.value, "title": self.title, "status": self.status.value, "source": self.source, "source_id": self.source_id,
             "timestamp": self.timestamp, "confidence": self.confidence, "notes": list(self.notes)}
        if with_text:
            d["original_text"] = self.original_text
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Fact":
        return cls(d["name"], d["value"], d.get("title", ""), FactStatus(d["status"]), d["source"], d["source_id"], d.get("timestamp"), d.get("original_text", ""),
                   d.get("confidence", "medium"), list(d.get("notes", [])))


@dataclass
class WStep:
    step_id: str
    description: str
    tool: str
    arguments: dict[str, Any] = field(default_factory=dict)
    dependencies: list[str] = field(default_factory=list)
    expected_result: str = ""
    verification: list[str] = field(default_factory=list)   # names of verification rules the runner applies to the output
    optional: bool = False                                  # failure becomes a warning; steps that need its output are skipped
    source: str = ""                                        # the system this step touches (gmail, calendar, tasks, ...)
    retry_policy: RetryPolicy = field(default_factory=RetryPolicy)
    side_effect: bool = False
    # computed by code:
    risk: Risk = Risk.READ_ONLY
    permission: str = ""
    status: SStatus = SStatus.PENDING
    output: dict[str, Any] = field(default_factory=dict)
    note: str = ""
    attempts: int = 0
    failure: FailureKind | None = None
    idempotency_key: str = ""


@dataclass
class WorkflowResult:
    status: str
    summary: str
    actions_completed: list[str] = field(default_factory=list)
    actions_skipped: list[str] = field(default_factory=list)
    actions_blocked: list[str] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    duration_s: float = 0.0
    suggestions: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


@dataclass
class Workflow:
    goal: str
    template: str
    steps: list[WStep] = field(default_factory=list)
    session_id: str = ""
    requested_by: str = "user"      # "user" (a spoken/typed request) | "proactive" (suggestion-only: no writes)
    workflow_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    status: WStatus = WStatus.DRAFT
    risk_level: Risk = Risk.READ_ONLY
    scope: set[str] = field(default_factory=set)            # the systems this workflow may touch, derived from the goal
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    question: str | None = None
    choices: list[dict[str, Any]] = field(default_factory=list)
    preview: str = ""
    ack: str = ""
    result: WorkflowResult | None = None
    failure_kind: FailureKind | None = None
    failure: str = ""
    cancel_reason: str = ""
    warnings: list[str] = field(default_factory=list)
    facts: list[Fact] = field(default_factory=list)
    params: dict[str, Any] = field(default_factory=dict)
    tool_calls: int = 0
    retries: int = 0
    pending_step: str | None = None
    recovered: bool = False

    def touch(self, status: WStatus | None = None) -> None:
        if status is not None:
            self.status = status
        self.updated_at = time.time()

    @property
    def terminal(self) -> bool:
        return self.status in TERMINAL

    def step(self, step_id: str) -> WStep | None:
        return next((s for s in self.steps if s.step_id == step_id), None)

    @property
    def done_count(self) -> int:
        return sum(1 for s in self.steps if s.status in (SStatus.DONE, SStatus.SKIPPED))

    @property
    def current(self) -> WStep | None:
        return next((s for s in self.steps if s.status is SStatus.RUNNING), None) or next((s for s in self.steps if s.status is SStatus.PENDING), None)

    def summary(self) -> dict[str, Any]:
        """Safe for the dashboard/API/logs: no outputs, no email or page text."""
        cur = self.current
        return {"workflow_id": self.workflow_id, "goal": self.goal[:200], "template": self.template, "status": self.status.value, "progress": [self.done_count, len(self.steps)],
                "current_step": cur.description if cur else "", "risk": self.risk_level.name, "sources": sorted(self.scope), "question": self.question, "warnings": self.warnings[:6],
                "failure": self.failure[:300], "failure_kind": self.failure_kind.value if self.failure_kind else None, "result": self.result.summary[:600] if self.result else "",
                "started_at": self.created_at, "updated_at": self.updated_at, "finished_at": self.finished_at, "requested_by": self.requested_by,
                "duration_s": round((self.finished_at or time.time()) - self.created_at, 1), "recovered": self.recovered,
                "steps": [{"id": s.step_id, "description": s.description, "status": s.status.value, "risk": s.risk.name, "source": s.source, "note": s.note[:120],
                           "failure": s.failure.value if s.failure else None} for s in self.steps]}
