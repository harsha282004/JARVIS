"""IntelligenceService: the one facade over the personal intelligence layer.

    sources (tasks, reminders, events, calendar, Gmail, memory, documents)          [read only]
        -> SnapshotCollector -> Snapshot
        -> PersonalContextEngine -> ContextGraph + deadlines + conflicts + task proposals
        -> FindingsEngine -> findings (facts, evidence, optional suggestions and offers)
        -> answers (focus, plan, briefing, project status, workflows, explanations) and notifications

The bundle (snapshot + graph + findings) is cached for a few seconds so several questions in one conversation cost one read of the
sources. Nothing derived is written back to a source without the user's confirmation (the ConfirmationEngine), and every action that
does run is verified and audited. The layer itself makes no language-model call.
"""

import threading
import time
from dataclasses import dataclass
from datetime import date, datetime, time as dtime, timedelta

from agent.intelligence.briefing import BriefingComposer
from agent.intelligence.confirmation import ActionReport, ConfirmationEngine
from agent.intelligence.context_engine import ContextResult, PersonalContextEngine, TaskProposal
from agent.intelligence.dependencies import DepStatus, DependencyError, DependencyStore
from agent.intelligence.explain import ExplanationLog
from agent.intelligence.findings import Finding, FindingKind, FindingsEngine, Offer, Urgency
from agent.intelligence.models import Answer, EntityKind, MemoryItem, Provenance, Snapshot, SourceKind, SourceState, Statement, action, fact, utcnow
from agent.intelligence.phrasing import day_word, join_and, quoted, when_phrase
from agent.intelligence.plan_executor import PlanExecutor
from agent.intelligence.planner import DayPlanner, PlanProposal
from agent.intelligence.refs import ConversationContext
from agent.intelligence.relevance import rank_memories
from agent.intelligence.snapshot import SnapshotCollector
from agent.intelligence.textnorm import same_thing_score, tokens
from agent.intelligence.timeline import ActivityTimeline
from agent.intelligence.workflows import Workflows
from agent.memory.models import Confidence
from backend.core.action_audit import ActionAuditLog, ActionResult, Confirmation
from backend.core.events import EventBus, SystemEvent
from backend.core.logging import get_logger
from backend.core.metrics import metrics
from backend.core.preferences import PreferenceStore, parse_hhmm
from backend.core.security.approval import ApprovalClass
from backend.core.state_store import JsonFile

logger = get_logger(__name__)

BUNDLE_MAX_AGE_SECONDS = 20.0


@dataclass
class Bundle:
    snapshot: Snapshot
    result: ContextResult
    findings: list[Finding]
    fingerprint: str
    built_at: float


class IntelligenceService:
    def __init__(
        self,
        *,
        zone,
        collector: SnapshotCollector,
        engine: PersonalContextEngine,
        planner: DayPlanner,
        confirmations: ConfirmationEngine,
        prefs: PreferenceStore,
        deps: DependencyStore,
        timeline: ActivityTimeline,
        audit: ActionAuditLog | None = None,
        bus: EventBus | None = None,
        executor: PlanExecutor | None = None,
        tasks=None,
        rag_search=None,
        state_file=None,
        clock=utcnow,
        auto_create_default: bool = False,
        notifications=None,
    ):
        self.zone = zone
        self._collector, self._engine, self.planner = collector, engine, planner
        self.confirmations, self.prefs, self.deps, self.timeline = confirmations, prefs, deps, timeline
        self._audit, self._bus, self.executor, self._tasks = audit, bus, executor, tasks
        self.notifications = notifications
        self._clock = clock
        self.findings_engine = FindingsEngine(zone, prefs, deps)
        self.explanations = ExplanationLog()
        self.context = ConversationContext(clock)
        self.composer = BriefingComposer(zone, planner)
        self.workflows = Workflows(zone, rag_search)
        self._processed = JsonFile(state_file, {"created": []}) if state_file else None
        self._created_mem: set[str] = set()
        self._auto_default = auto_create_default
        self._lock = threading.RLock()
        self._bundle: Bundle | None = None
        self._previous: Snapshot | None = None
        self.last_plan: PlanProposal | None = None
        self.last_offers: list[Offer] = []
        self.last_answer: Answer | None = None
        self.last_briefing: Answer | None = None

    def now(self) -> datetime:
        """The current time in the user's timezone (from the injected clock, so tests and the real system agree)."""
        return self._clock().astimezone(self.zone)

    # ---- the bundle -------------------------------------------------------------------------------------------------------

    def bundle(self, *, max_age: float = BUNDLE_MAX_AGE_SECONDS, force: bool = False) -> Bundle:
        with self._lock:
            b = self._bundle
            if b is not None and not force and time.monotonic() - b.built_at < max_age:
                return b
            return self._refresh()

    def _refresh(self) -> Bundle:
        with metrics.timer("intelligence.refresh_ms"):
            with metrics.timer("intelligence.snapshot_ms"):
                snap = self._collector.collect()
            fingerprint = snap.fingerprint()
            with metrics.timer("intelligence.context_ms"):
                result = self._engine.build(snap)
            with metrics.timer("intelligence.findings_ms"):
                findings = self.findings_engine.evaluate(snap, result, lambda title, minutes: self.planner.next_free_slot(snap, result, minutes))
        metrics.incr("intelligence.refreshes")
        bundle = Bundle(snap, result, findings, fingerprint, time.monotonic())
        self._publish_changes(self._previous, snap, result)
        self._previous = snap
        self._bundle = bundle
        return bundle

    def cached(self) -> Bundle | None:
        return self._bundle

    def _publish_changes(self, before: Snapshot | None, after: Snapshot, result: ContextResult) -> None:
        """Turn differences between two snapshots into bus events, so other parts can react instead of polling. The first snapshot
        publishes nothing (it is the baseline, not news)."""
        if before is None or self._bus is None:
            return
        old_mail = {e.message_id for e in before.emails}
        for e in after.emails:
            if e.message_id not in old_mail:
                self._bus.publish(SystemEvent.EMAIL_RECEIVED, message_id=e.message_id)
        old_tasks = {t.task_id: t for t in before.tasks}
        for t in after.tasks:
            prev = old_tasks.get(t.task_id)
            if prev is None:
                self._bus.publish(SystemEvent.TASK_CREATED, task_id=t.task_id)
            elif prev.status != "completed" and t.status == "completed":
                self._bus.publish(SystemEvent.TASK_COMPLETED, task_id=t.task_id)
        old_cal = {(c.event_id, c.start, c.end, c.title) for c in before.calendar}
        if {(c.event_id, c.start, c.end, c.title) for c in after.calendar} != old_cal:
            self._bus.publish(SystemEvent.CALENDAR_UPDATED)
        old_deadlines = {d.deadline_id for d in (self._bundle.result.deadlines if self._bundle else [])}
        for d in result.deadlines:
            if d.deadline_id not in old_deadlines and d.source.source_type is not SourceKind.TASK:
                self._bus.publish(SystemEvent.DEADLINE_DETECTED, deadline_id=d.deadline_id, source=d.source.source_type.value)

    # ---- answers (each records its evidence for "why?" and "where did you get that?") ---------------------------------------

    def _finish(self, answer: Answer, *, record: bool = True) -> Answer:
        self.last_answer = answer
        if record and answer.statements:
            self.explanations.record("answer", answer.subject or answer.text[:60], answer.statements, lead="I told you that")
        if answer.entity_ids:
            self.context.note(list(answer.entity_ids))
        return answer

    def focus(self) -> Answer:
        with metrics.timer("intelligence.focus_ms"):
            b = self.bundle()
            return self._finish(self.composer.focus(b.snapshot, b.result, b.findings))

    def important(self, day: date) -> Answer:
        b = self.bundle()
        return self._finish(self.composer.important(b.snapshot, b.result, b.findings, day))

    def morning(self) -> Answer:
        b = self.bundle(force=True)
        pending = []
        if self.notifications is not None:
            pending = [f"{n.title}." for n in self.notifications.for_briefing()[:3]]
        answer = self.composer.morning(b.snapshot, b.result, b.findings, pending)
        self.last_briefing = answer
        return self._finish(answer)

    def evening(self) -> Answer:
        b = self.bundle(force=True)
        return self._finish(self.composer.evening(b.snapshot, b.result, b.findings))

    def conflicts(self) -> Answer:
        b = self.bundle()
        kinds = (FindingKind.CALENDAR_OVERLAP, FindingKind.MEMORY_CONFLICT, FindingKind.SOURCE_CONFLICT, FindingKind.MULTIPLE_DEADLINES,
                 FindingKind.REMINDER_COLLISION, FindingKind.TASK_LATER_THAN_REQUESTED)
        found = [f for f in b.findings if f.kind in kinds]
        if not found:
            unavailable = b.snapshot.unavailable()
            extra = f" I couldn't check {join_and(unavailable)}, so this only covers what I could read." if unavailable else ""
            return self._finish(Answer("I don't see any conflicts in what I can read." + extra, subject="conflicts"), record=False)
        stmts = tuple(s for f in found for s in f.facts)
        text = " ".join(f.spoken(with_suggestion=False) for f in found[:5])
        return self._finish(Answer(text, stmts, tuple(e for f in found for e in f.entity_ids), subject="conflicts"))

    def prepare(self, query: str, day: date | None) -> Answer:
        b = self.bundle()
        with metrics.timer("intelligence.workflow_ms"):
            return self._finish(self.workflows.prepare_for(b.snapshot, b.result, b.findings, query, day))

    def project(self, name: str) -> Answer:
        b = self.bundle()
        return self._finish(self.workflows.project_status(b.snapshot, b.result, name, b.findings))

    def active_project(self) -> str | None:
        """The project most recently talked about, else the only project there is."""
        b = self.bundle()
        for e in self.context.recent_entities(b.result.graph, frozenset({EntityKind.PROJECT})):
            return e.name
        projects = b.result.projects
        return projects[0] if len(projects) == 1 else None

    def activity(self, day: date, topic: str | None) -> Answer:
        b = self.bundle()
        self.timeline.ingest(b.snapshot)
        related: frozenset[tuple[str, str]] = frozenset()
        if topic:
            project = self.workflows.find_project(b.result, topic)
            if project is not None:
                ids = {project.entity_id}
                for _, other in b.result.graph.related(project.entity_id):
                    ids.add(other.entity_id)
                pairs = set()
                for eid in ids:
                    e = b.result.graph.get(eid)
                    if e is not None:
                        for p in e.provenance:
                            pairs.add((_TIMELINE_SOURCE.get(p.source_type, p.source_type.value), _bare_id(p)))
                related = frozenset(pairs)
        text = self.timeline.describe_day(day, self.zone, b.snapshot.now, topic, related)
        return self._finish(Answer(text, subject=f"activity {day_word(day, b.snapshot.now, self.zone)}"), record=False)

    # ---- planning -------------------------------------------------------------------------------------------------------------

    def plan(self, day: date) -> Answer:
        with metrics.timer("intelligence.plan_ms"):
            b = self.bundle(force=True)
            plan = self.planner.plan(b.snapshot, b.result, day, self.deps)
        self.last_plan = plan
        self.last_offers = []
        stmts: list[Statement] = []
        for block in plan.blocks:
            stmts.extend(block.reason)
            self.explanations.record("plan_block", f"{block.title} scheduled", block.reason, lead=f"I scheduled {quoted(block.title)} at {block.start.astimezone(self.zone).strftime('%H:%M')}")
        answer = Answer(self.planner.describe(plan, b.snapshot.now), tuple(stmts), tuple(f"task:{blk.task_id}" for blk in plan.blocks if blk.task_id), subject="the proposed plan")
        return self._finish(answer, record=False)

    def add_plan_to_calendar(self, session_id: str) -> str:
        """Ask for confirmation to add the last proposed plan to the calendar (nothing is created yet)."""
        if self.last_plan is None or not self.last_plan.blocks:
            return "I don't have a plan to add yet. Ask me to plan your day first."
        if self.executor is None:
            return "Google Calendar isn't set up, so I can't add anything to it. The plan is only a proposal."
        return self.executor.propose_plan(self.last_plan, session_id)

    def offer_action(self, offer: Offer, session_id: str) -> str:
        if offer.kind == "calendar_event":
            if self.executor is None:
                return "Google Calendar isn't set up, so I can't add it."
            return self.executor.propose_offer(offer, session_id)
        if offer.kind == "create_task":
            return self._propose_task(offer, session_id)
        return "I can't do that."

    def _propose_task(self, offer: Offer, session_id: str) -> str:
        if self._tasks is None:
            return "Tasks aren't enabled, so I can't create one."
        params = {"title": offer.title, "due": offer.start.isoformat() if offer.start else None}
        return self.confirmations.request(
            action_class=ApprovalClass.CREATE_FROM_EXTERNAL, tool="tasks.create_task", summary=offer.prompt, params=params,
            run=lambda: self._create_task(offer, Confirmation.USER), session_id=session_id, source=f"proposal:{offer.proposal_id}",
        )

    def _create_task(self, offer: Offer, confirmation: Confirmation) -> ActionReport:
        """Create the task, then read it back. Only a verified read-back is reported as done."""
        key = offer.proposal_id or offer.title
        if key in self._created() or self._similar_open_task(offer.title):
            return ActionReport(True, "That task already exists, so I didn't create a duplicate.", verified=True)
        try:
            task = self._tasks.create_task(offer.title, due_at=offer.start, source="email", metadata={"origin": "intelligence", "proposal": key})
            check = self._tasks.get_task(task.task_id)
        except Exception as exc:  # noqa: BLE001
            self._audit_record("tasks.create_task", "create task", ActionResult.FAILED, confirmation, offer.title, type(exc).__name__)
            return ActionReport(False, "I couldn't create the task, so nothing was added.")
        ok = check.title == task.title
        self._mark_created(key)
        self._audit_record("tasks.create_task", "create task", ActionResult.SUCCESS if ok else ActionResult.UNVERIFIED, confirmation, offer.title, "")
        self.timeline.note_action(self._clock(), f"Task created by JARVIS: {offer.title[:80]}", task.task_id)
        self._invalidate()
        if not ok:
            return ActionReport(True, "I created the task but couldn't confirm it, so please check your task list.", verified=False)
        due = f", due {when_phrase(offer.start, self._clock(), self.zone, offer.all_day)}" if offer.start else ""
        return ActionReport(True, f"Done. I created the task {quoted(offer.title)}{due} and confirmed it's in your task list.", verified=True)

    def _similar_open_task(self, title: str) -> bool:
        b = self._bundle
        if b is None:
            return False
        return any(t.is_open and same_thing_score(title, t.title)[0] >= 0.75 for t in b.snapshot.tasks)

    def _created(self) -> set[str]:
        if self._processed is None:
            return self._created_mem
        return set(self._processed.read().get("created", []))

    def _mark_created(self, key: str) -> None:
        if self._processed is None:
            self._created_mem.add(key)
            return
        self._processed.update(lambda d: {"created": sorted({*d.get("created", []), key})[-500:]})

    def _audit_record(self, tool: str, act: str, result: ActionResult, confirmation: Confirmation, detail: str, error: str) -> None:
        if self._audit is not None:
            self._audit.record(tool=tool, action=act, result=result, confirmation=confirmation, source="intelligence", detail=f"{detail[:100]} {error}".strip())

    def _invalidate(self) -> None:
        with self._lock:
            self._bundle = None

    def after_change(self) -> None:
        """A confirmed action changed a source: forget the cached bundle and tell the rest of JARVIS (event-driven, no polling)."""
        self._invalidate()
        if self._bus is not None:
            self._bus.publish(SystemEvent.AGENT_RESPONSE, executed=True, source="intelligence")

    def auto_create_enabled(self) -> bool:
        return self.prefs.get().auto_create_tasks or self._auto_default

    def auto_create_tasks(self, b: Bundle) -> list[Statement]:
        """When the user allowed it: create tasks for clear, unflagged, high-confidence email requests, once each. Otherwise nothing."""
        if not self.auto_create_enabled() or self._tasks is None:
            return []
        made: list[Statement] = []
        for p in b.result.proposals:
            if p.flagged or p.confidence < Confidence.MEDIUM or p.due_at is None or p.source.source_type is not SourceKind.EMAIL:
                continue
            offer = Offer("create_task", p.title, p.due_at, None, p.all_day, "", p.proposal_id)
            report = self._create_task(offer, Confirmation.POLICY)
            if report.ok and report.verified and "already exists" not in report.message:
                made.append(action(f"I created the task {quoted(p.title)} from {p.source.phrase}.", p.source))
        return made

    # ---- dependencies ---------------------------------------------------------------------------------------------------------

    def resolve_task(self, phrase: str):
        """(task, None) for one clear match, (None, [candidates]) when several match, (None, []) for none. Never guesses between two."""
        b = self.bundle()
        scored = []
        for t in b.snapshot.tasks:
            if not t.is_open:
                continue
            score, shared = same_thing_score(phrase, t.title, verbs=True)
            if score >= 0.6 and shared:
                scored.append((score, t))
        scored.sort(key=lambda x: -x[0])
        if not scored:
            return None, []
        if len(scored) > 1 and scored[0][0] - scored[1][0] < 0.15:
            return None, [t for _, t in scored[:3]]
        return scored[0][1], None

    def add_dependency(self, task_phrase: str, prerequisite_phrase: str) -> str | None:
        """None when the phrases do not name two of the user's tasks (so the request is not ours to handle)."""
        a, alts_a = self.resolve_task(task_phrase)
        b, alts_b = self.resolve_task(prerequisite_phrase)
        if alts_a:
            return "Which task do you mean: " + join_and([quoted(t.title) for t in alts_a]) + "?"
        if alts_b:
            return "Which task does it depend on: " + join_and([quoted(t.title) for t in alts_b]) + "?"
        if a is None or b is None:
            return None
        try:
            added = self.deps.add(a.task_id, b.task_id)
        except DependencyError as exc:
            return str(exc)
        self._invalidate()
        if not added:
            return f"I already have {quoted(a.title)} waiting for {quoted(b.title)}."
        self._audit_record("dependencies.add", "record dependency", ActionResult.SUCCESS, Confirmation.POLICY, f"{a.title} -> {b.title}", "")
        return f"Okay. I'll treat {quoted(a.title)} as blocked until {quoted(b.title)} is finished."

    def blocked_answer(self, phrase: str | None = None) -> Answer:
        b = self.bundle()
        by_id = {t.task_id: t for t in b.snapshot.tasks}
        if phrase:
            task, alts = self.resolve_task(phrase)
            if alts:
                return Answer("Which task do you mean: " + join_and([quoted(t.title) for t in alts]) + "?")
            if task is None:
                return Answer(f"I don't see an open task matching that.")
            blockers = self.deps.blockers(task, by_id)
            if not blockers:
                return Answer(f"Nothing is blocking {quoted(task.title)}; it's {self.deps.status_for(task, by_id).value.replace('_', ' ')}.")
            return Answer(f"{quoted(task.title)} is waiting for {join_and([quoted(t.title) for t in blockers])}.")
        blocked = [t for t in b.snapshot.tasks if t.is_open and self.deps.status_for(t, by_id) is DepStatus.BLOCKED]
        if not blocked:
            return Answer("None of your open tasks are blocked by a dependency.")
        return Answer("Blocked: " + "; ".join(f"{quoted(t.title)} (waiting for {join_and([quoted(x.title) for x in self.deps.blockers(t, by_id)])})" for t in blocked[:6]) + ".")

    # ---- references, explanations, memory ---------------------------------------------------------------------------------

    def observe_turn(self, user_text: str, reply_text: str) -> None:
        """After any turn (also the ones the language model answered), remember which of the user's things it was about."""
        b = self._bundle
        if b is None:
            return
        try:
            self.context.observe(f"{user_text} {reply_text}", b.result.graph)
        except Exception as exc:  # noqa: BLE001 - context tracking is best effort
            logger.warning("Context tracking failed (%s)", type(exc).__name__)

    def rerank_memories(self, query: str, memories: list) -> list:
        """Keep the retrieved memories that are actually relevant to this request, best first (see relevance.py)."""
        if not memories:
            return memories
        items = [MemoryItem(m.memory_id, m.content, m.created_at, m.confidence, str(getattr(m.basis, "value", m.basis)) == "explicit", str(getattr(m.type, "value", m.type)))
                 for m in memories]
        b = self._bundle
        ranked = rank_memories(items, query, graph=b.result.graph if b else None, now=self._clock(), limit=len(items))
        order = {r.memory.memory_id: i for i, r in enumerate(ranked)}
        kept = [m for m in memories if m.memory_id in order]
        kept.sort(key=lambda m: order[m.memory_id])
        return kept

    def end_session(self, session_id: str) -> None:
        self.confirmations.cancel(session_id)
        self.context.reset()

    # ---- background pass -------------------------------------------------------------------------------------------------------

    def background_pass(self) -> Bundle:
        """One evaluation for the runner: fresh bundle, timeline ingest, dependency pruning, optional auto-created tasks."""
        b = self.bundle(force=True)
        try:
            self.timeline.ingest(b.snapshot)
            if b.snapshot.ok("tasks"):
                self.deps.prune({t.task_id for t in b.snapshot.tasks})
        except Exception as exc:  # noqa: BLE001
            logger.warning("Background housekeeping failed (%s)", type(exc).__name__)
        return b

    # ---- dashboard --------------------------------------------------------------------------------------------------------------

    def dashboard(self) -> dict:
        """Everything the intelligence view shows, from the latest bundle (never hidden reasoning, only evidence)."""
        b = self.bundle(max_age=60.0)
        s, r = b.snapshot, b.result
        z = self.zone

        def when(d: datetime | None) -> str | None:
            return d.astimezone(z).isoformat() if d else None

        return {
            "generated_at": when(s.now),
            "sources": {k: v.value for k, v in s.states.items()},
            "active_context": [e.name for e in self.context.recent_entities(r.graph)[:5]],
            "projects": r.projects,
            "deadlines": [{"text": d.original_text[:80], "kind": d.kind.value, "due": when(d.due_at), "status": d.status.value, "source": d.source.source_type.value,
                           "confidence": d.confidence.name.lower()} for d in r.deadlines if d.status.value in ("open", "overdue")][:15],
            "events": [{"name": e.name, "kind": e.kind.value, "when": when(e.when), "on_calendar": bool(e.attributes.get("on_calendar")),
                        "sources": [p.source_type.value for p in e.provenance]}
                       for e in sorted((e for e in r.graph.entities.values() if e.kind.value in ("meeting", "event", "project_review", "interview", "exam", "hackathon")
                                        and e.when and e.when >= s.now - timedelta(hours=2)), key=lambda e: e.when)[:15]],
            "pending_tasks": [{"title": t.title, "status": t.status, "due": when(t.due_at), "priority": t.priority,
                               "dependency": self.deps.status_for(t, {x.task_id: x for x in s.tasks}).value} for t in s.tasks if t.is_open][:20],
            "conflicts": [{"title": f.title, "text": f.spoken(with_suggestion=False), "kind": f.kind.value} for f in b.findings if f.category == "conflict"],
            "recommendations": [{"title": f.title, "urgency": f.urgency.name.lower(), "text": f.spoken(), "kind": f.kind.value,
                                 "evidence": [p.describe() for p in f.evidence()]} for f in b.findings if f.urgency >= Urgency.NOTICE][:12],
            "recent_events": [{"type": e.type.value, "at": e.at.isoformat()} for e in (self._bus.recent()[-15:] if self._bus else [])],
            "metrics": metrics.snapshot(),
        }


_TIMELINE_SOURCE = {SourceKind.EMAIL: "email", SourceKind.TASK: "task", SourceKind.CALENDAR: "calendar"}


def _bare_id(p: Provenance) -> str:
    return p.source_id.split("/")[-1] if p.source_type is SourceKind.CALENDAR else p.source_id
