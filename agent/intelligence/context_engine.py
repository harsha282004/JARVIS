"""PersonalContextEngine: turns a `Snapshot` into a `ContextGraph` of connected, evidence-backed entities.

    tasks, calendar, saved events, emails, documents, memories  ->  entities
    one thing described by several sources                       ->  resolved into ONE entity (or a recorded conflict)
    shared project names, containment of names, dependencies    ->  typed relationships with a reason and a confidence

Rules that keep it honest:
  * Relationships are only created from evidence that is stated in the sources (a project name that appears in a title, a task title
    that contains a whole event name, an explicit dependency). A single shared generic word ("project") is never enough.
  * Two sources are merged only when their names match strongly AND they agree on the day. A strong name match with a different day
    is NOT merged: it is reported as a conflict, and JARVIS does not choose a side.
  * Ids are derived from source ids and normalized names, so rebuilding from the same sources gives the same graph: nothing is
    duplicated and processing is idempotent.
  * Nothing here writes to any source system.
"""

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from agent.events.models import EventType
from agent.intelligence.deadlines import Deadline, DeadlineKind, DeadlineStatus, classify_deadline_kind, status_for
from agent.intelligence.dependencies import DependencyStore
from agent.intelligence.extraction import Commitment, CommitmentKind, ExtractionOutcome, TextExtractor, detect_project
from agent.intelligence.models import (
    ContextGraph,
    Entity,
    EntityKind,
    EVENT_LIKE,
    Provenance,
    RelationKind,
    Relationship,
    Snapshot,
    SourceKind,
    stable_id,
    utcnow,
)
from agent.intelligence.textnorm import GENERIC, contains_phrase, distinctive, same_thing_score, tokens
from agent.memory.models import Confidence
from backend.core.logging import get_logger

logger = get_logger(__name__)

MERGE_THRESHOLD = 0.75
_SUPPORT_NOUNS = frozenset({"slide", "presentation", "agenda", "note", "material", "prep", "preparation", "checklist", "summary", "prepa"})
_SOURCE_PRIORITY = {SourceKind.CALENDAR: 0, SourceKind.EVENT_RECORD: 1, SourceKind.EMAIL: 2, SourceKind.DOCUMENT: 3, SourceKind.MEMORY: 4}
_ORG = re.compile(r"\b(?:with|at)\s+(?:the\s+)?([A-Z][\w&.\-]*(?:\s+[A-Z][\w&.\-]*){0,2})")
_ORG_STOP = {"monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday", "the", "our", "your", "my", "tomorrow", "today"}


@dataclass(frozen=True)
class DateConflict:
    """Two sources name the same thing but disagree on when. JARVIS reports both and picks neither."""

    title: str
    kind: str  # "date" or "time"
    left: Provenance
    left_when: datetime
    left_all_day: bool
    right: Provenance
    right_when: datetime
    right_all_day: bool


@dataclass(frozen=True)
class TaskProposal:
    """A task an email/document asks for that the user has no similar task for. Never created without the user's yes."""

    proposal_id: str
    title: str
    due_at: datetime | None
    all_day: bool
    project: str | None
    source: Provenance
    confidence: Confidence
    flagged: bool  # the source looked like an injection attempt: never auto-created


@dataclass
class ContextResult:
    graph: ContextGraph
    deadlines: list[Deadline] = field(default_factory=list)
    conflicts: list[DateConflict] = field(default_factory=list)
    proposals: list[TaskProposal] = field(default_factory=list)
    unresolved: list[tuple[str, str, Provenance]] = field(default_factory=list)  # (phrase, question, source)
    flagged_sources: list[Provenance] = field(default_factory=list)
    projects: list[str] = field(default_factory=list)
    linked_existing: dict[str, str] = field(default_factory=dict)  # proposal id -> existing task entity id it duplicates
    cache_hits: int = 0


@dataclass
class _Mention:
    title: str
    when: datetime | None
    all_day: bool
    kind: EntityKind
    prov: Provenance
    entity_id: str  # id if it becomes its own entity
    status: str | None = None
    attributes: dict = field(default_factory=dict)


def classify_event_kind(title: str, event_type: str | None = None) -> EntityKind:
    t = title.lower()
    if event_type == "interview" or "interview" in t:
        return EntityKind.INTERVIEW
    if event_type == "exam" or re.search(r"\b(exam|midterm|viva|quiz)\b", t):
        return EntityKind.EXAM
    if "hackathon" in t:
        return EntityKind.HACKATHON
    if event_type == "assignment":
        return EntityKind.ASSIGNMENT
    if "project" in t and "review" in t or re.search(r"\b(design|code) review\b", t):
        return EntityKind.PROJECT_REVIEW
    if event_type == "meeting" or re.search(r"\b(meeting|call|sync|stand-?up|catch-?up|session)\b", t):
        return EntityKind.MEETING
    return EntityKind.EVENT


def _local_day(moment: datetime, zone) -> object:
    return moment.astimezone(zone).date()


class PersonalContextEngine:
    def __init__(self, zone, clock=utcnow, dependencies: DependencyStore | None = None):
        self._zone = zone
        self._clock = clock
        self._deps = dependencies
        self._cache: dict[tuple[str, str, int], ExtractionOutcome] = {}
        self.cache_hits = 0
        self.cache_misses = 0

    # ---- public --------------------------------------------------------------------------------------------------------

    def build(self, snap: Snapshot) -> ContextResult:
        graph = ContextGraph()
        result = ContextResult(graph)
        now = snap.now
        graph.add_entity(Entity("user", EntityKind.USER, "you"))

        projects = self._find_projects(snap)
        extractor = TextExtractor(snap.zone, self._clock, tuple(projects))
        task_entities = self._add_tasks(graph, snap)
        mentions = self._event_mentions(snap)
        commitments = self._extract_all(snap, extractor, result)
        self._add_deadline_records(graph, snap, result)
        clusters = self._resolve_events(graph, mentions, commitments, snap, result)
        self._apply_commitments(graph, snap, commitments, clusters, task_entities, result)
        self._apply_external(graph, snap, result)
        self._add_projects(graph, snap, projects, result)
        self._link_tasks_to_events(graph, task_entities)
        self._add_dependencies(graph, snap, task_entities)
        self._task_deadlines(graph, snap, task_entities, result)
        result.projects = projects
        result.cache_hits = self.cache_hits
        result.deadlines.sort(key=lambda d: (d.due_at, d.deadline_id))
        return result

    # ---- projects --------------------------------------------------------------------------------------------------------

    def _find_projects(self, snap: Snapshot) -> list[str]:
        names: dict[str, str] = {}

        def note(text: str) -> None:
            name = detect_project(text)
            if name and name.lower() not in names:
                names[name.lower()] = name

        for t in snap.tasks:
            note(f"{t.title}. {t.notes}")
        for c in snap.calendar:
            note(c.title)
        for r in snap.event_records:
            note(r.title)
        for d in snap.documents:
            note(d.title)
        for x in snap.external:
            for proj in x.meta("projects", []) or []:  # explicit user associations (repository <-> project)
                names.setdefault(str(proj).lower(), str(proj))
        for m in snap.memories:
            if "project" in m.content.lower():
                note(m.content)
        for e in snap.emails:
            note(e.subject)
        return sorted(names.values(), key=str.lower)

    def _add_projects(self, graph: ContextGraph, snap: Snapshot, projects: list[str], result: ContextResult) -> None:
        now = snap.now
        for name in projects:
            pid = f"proj:{name.lower()}"
            prov_list: list[Provenance] = []
            for m in snap.memories:
                if contains_phrase(m.content, name):
                    prov_list.append(Provenance(SourceKind.MEMORY, m.memory_id, "a stored memory", m.created_at, now, m.confidence, pid, m.content[:120]))
            graph.add_entity(Entity(pid, EntityKind.PROJECT, name, provenance=prov_list))
            project = graph.get(pid)
            assert project is not None
            for entity in list(graph.entities.values()):
                if entity.kind in (EntityKind.PROJECT, EntityKind.USER):
                    continue
                text = entity.name
                notes = str(entity.attributes.get("notes", ""))
                doc_text = str(entity.attributes.get("text", ""))
                if entity.kind is EntityKind.REPOSITORY and name.lower() in [str(p).lower() for p in entity.attributes.get("projects", [])]:
                    graph.add_relationship(Relationship(entity.entity_id, RelationKind.RELATES_TO, pid, Confidence.HIGH,
                                                        f"you told me this repository belongs to the '{name}' project", tuple(entity.provenance[:1])))
                    continue  # an explicit statement by the user is the strongest evidence there is
                if contains_phrase(text, name):
                    kind = RelationKind.BELONGS_TO if entity.kind in (EntityKind.TASK, EntityKind.ASSIGNMENT) else RelationKind.RELATES_TO
                    if entity.kind is EntityKind.DEADLINE:
                        continue
                    reason = f"the {entity.kind.value.replace('_', ' ')} title contains the project name '{name}'"
                    graph.add_relationship(Relationship(entity.entity_id, kind, pid, Confidence.HIGH, reason, tuple(entity.provenance[:2])))
                elif entity.kind in (EntityKind.TASK,) and contains_phrase(notes, name):
                    graph.add_relationship(Relationship(entity.entity_id, RelationKind.BELONGS_TO, pid, Confidence.MEDIUM,
                                                        f"the task notes mention the project name '{name}'", tuple(entity.provenance[:2])))
                elif entity.kind is EntityKind.DOCUMENT and len(re.findall(re.escape(name), doc_text, re.I)) >= 2:
                    graph.add_relationship(Relationship(entity.entity_id, RelationKind.RELATES_TO, pid, Confidence.MEDIUM,
                                                        f"the document mentions '{name}' several times", tuple(entity.provenance[:1])))
                elif entity.kind is EntityKind.REPOSITORY and name.lower() in [str(p).lower() for p in entity.attributes.get("projects", [])]:
                    graph.add_relationship(Relationship(entity.entity_id, RelationKind.RELATES_TO, pid, Confidence.HIGH,
                                                        f"you told me this repository belongs to the '{name}' project", tuple(entity.provenance[:1])))
                elif entity.attributes.get("project_hint", "").lower() == name.lower():
                    graph.add_relationship(Relationship(entity.entity_id, RelationKind.RELATES_TO, pid, Confidence.MEDIUM,
                                                        f"its source text refers to the '{name}' project", tuple(entity.provenance[:1])))

    # ---- tasks -------------------------------------------------------------------------------------------------------------

    def _add_tasks(self, graph: ContextGraph, snap: Snapshot) -> dict[str, Entity]:
        out: dict[str, Entity] = {}
        for t in snap.tasks:
            prov = Provenance(SourceKind.TASK, t.task_id, f"task '{t.title}'", t.created_at, snap.now, Confidence.HIGH, f"task:{t.task_id}")
            e = Entity(f"task:{t.task_id}", EntityKind.TASK, t.title, when=t.due_at, status=t.status,
                       attributes={"priority": t.priority, "estimate_minutes": t.estimate_minutes, "notes": t.notes, "task_id": t.task_id}, provenance=[prov])
            out[t.task_id] = graph.add_entity(e)
        return out

    # ---- event mentions (calendar, saved events) -----------------------------------------------------------------------

    def _event_mentions(self, snap: Snapshot) -> list[_Mention]:
        m: list[_Mention] = []
        for c in snap.calendar:
            if not c.blocks_time and not c.all_day:
                pass  # free/transparent events are still events; they just do not block planning time
            prov = Provenance(SourceKind.CALENDAR, f"{c.calendar_id}/{c.event_id}", f"calendar event '{c.title}'", None, snap.now, Confidence.HIGH, f"cal:{c.calendar_id}/{c.event_id}")
            m.append(_Mention(c.title, c.start, c.all_day, classify_event_kind(c.title), prov, f"cal:{c.calendar_id}/{c.event_id}",
                              attributes={"end": c.end, "blocks_time": c.blocks_time}))
        for x in snap.external:
            if x.kind == "event" and x.when is not None and not x.meta("registration"):
                src = SourceKind.MESSAGE if x.source == "telegram" else SourceKind.EMAIL
                prov = Provenance(src, x.source_id, f"a message ({x.title[:40]})", x.when, snap.now, x.confidence, f"ext:{x.source}:{x.source_id}", str(x.meta("evidence", ""))[:200])
                m.append(_Mention(x.title, x.when, False, classify_event_kind(x.title), prov, f"ext:{x.source}:{x.source_id}",
                                  attributes={"candidate": True, "flagged": bool(x.meta("injection_suspected"))}))
        for r in snap.event_records:
            if r.start_at is None:  # deadlines are handled separately
                continue
            prov = Provenance(SourceKind.EVENT_RECORD, r.event_id, f"saved event '{r.title}'", None, snap.now, r.confidence, f"rec:{r.event_id}")
            m.append(_Mention(r.title, r.start_at, False, classify_event_kind(r.title, r.event_type), prov, f"rec:{r.event_id}", r.status))
        return m

    def _add_deadline_records(self, graph: ContextGraph, snap: Snapshot, result: ContextResult) -> None:
        for r in snap.event_records:
            if r.start_at is not None or r.due_at is None:
                continue
            prov = Provenance(SourceKind.EVENT_RECORD, r.event_id, f"saved deadline '{r.title}'", None, snap.now, r.confidence, f"rec:{r.event_id}")
            kind = classify_deadline_kind(r.title)
            ent = graph.add_entity(Entity(f"rec:{r.event_id}", EntityKind.DEADLINE, r.title, when=r.due_at, status=r.status,
                                          attributes={"deadline_kind": kind.value, "task_id": r.task_id}, provenance=[prov]))
            result.deadlines.append(Deadline(
                f"dl:{ent.entity_id}", kind, r.due_at.astimezone(snap.zone), snap.zone.key, False, r.title[:120], r.confidence, prov, ent.entity_id,
                status_for(r.due_at, snap.now, confidence=r.confidence), False, r.title,
            ))

    # ---- extraction --------------------------------------------------------------------------------------------------------

    def _extract_all(self, snap: Snapshot, extractor: TextExtractor, result: ContextResult) -> list[Commitment]:
        found: list[Commitment] = []

        def run(source: SourceKind, sid: str, label: str, ts, text: str, subject=None, user_stated=False) -> None:
            key = (source.value, sid, hash(text) ^ hash(subject or ""))
            outcome = self._cache.get(key)
            if outcome is None:
                self.cache_misses += 1
                outcome = extractor.extract(text, source_type=source, source_id=sid, label=label, source_timestamp=ts, subject=subject, user_stated=user_stated)
                self._cache[key] = outcome
                if len(self._cache) > 500:
                    self._cache.pop(next(iter(self._cache)))
            else:
                self.cache_hits += 1
            if outcome.flagged:
                result.flagged_sources.append(Provenance(source, sid, label, ts, snap.now, Confidence.LOW, None, "contains text that tries to give JARVIS instructions"))
            now = snap.now
            found.extend(c for c in outcome.commitments if (c.when is None or c.when >= now - timedelta(days=1)))
            for u in outcome.unresolved:
                result.unresolved.append((u.phrase, u.question, Provenance(source, sid, label, ts, now, Confidence.LOW, None, u.evidence)))

        for e in snap.emails:
            run(SourceKind.EMAIL, e.message_id, f"email '{e.subject[:60]}'", e.received_at, e.body, e.subject)
        for m in snap.memories:
            run(SourceKind.MEMORY, m.memory_id, "a stored memory", m.created_at, m.content, None, user_stated=m.explicit)
        for d in snap.documents:
            if d.text:
                run(SourceKind.DOCUMENT, d.document_id, f"document '{d.title[:60]}'", d.indexed_at, d.text)
        return found

    # ---- resolution ----------------------------------------------------------------------------------------------------------

    def _resolve_events(self, graph, mentions: list[_Mention], commitments: list[Commitment], snap: Snapshot, result: ContextResult) -> dict[str, str]:
        """Cluster event descriptions from every source. Returns commitment key -> entity id of the event it was resolved into."""
        zone = snap.zone
        for c in commitments:
            if c.kind is CommitmentKind.EVENT and c.when is not None:
                kind = classify_event_kind(c.title, c.event_type.value if c.event_type else None)
                mentions.append(_Mention(c.title, c.when, c.all_day, kind, c.source, f"cand:{stable_id(c.title, c.when.date().isoformat())}",
                                         attributes={"project_hint": c.project_hint or "", "flagged": c.flagged, "candidate": True, "evidence": c.evidence}))
        mentions.sort(key=lambda m: (_SOURCE_PRIORITY.get(m.prov.source_type, 9), m.when or snap.now, m.title))
        clusters: list[tuple[Entity, list[_Mention]]] = []
        resolved: dict[str, str] = {}
        for m in mentions:
            best: tuple[float, Entity, list[_Mention]] | None = None
            for entity, members in clusters:
                if any(x.prov.source_type is m.prov.source_type and x.prov.source_id != m.prov.source_id for x in members) and m.prov.source_type in (SourceKind.CALENDAR, SourceKind.EVENT_RECORD):
                    continue  # two different records of one source are two things (a recurring stand-up), never one
                score, _ = same_thing_score(entity.name, m.title, verbs=True)
                if score >= MERGE_THRESHOLD and (best is None or score > best[0]):
                    best = (score, entity, members)
            if best is None:
                entity = graph.add_entity(Entity(m.entity_id, m.kind, m.title, when=m.when, until=m.attributes.get("end"), all_day=m.all_day,
                                                 status=m.status, attributes={k: v for k, v in m.attributes.items() if k != "end"}, provenance=[m.prov]))
                if "end" in m.attributes:
                    entity.attributes["end"] = m.attributes["end"]
                clusters.append((entity, [m]))
                continue
            score, entity, members = best
            same_day = m.when is None or entity.when is None or _local_day(m.when, zone) == _local_day(entity.when, zone)
            if same_day:
                self._merge(entity, m, zone)
                members.append(m)
            else:
                # Same name, different day: not merged. Both stay as separate entities and the disagreement is recorded.
                other = graph.add_entity(Entity(m.entity_id, m.kind, m.title, when=m.when, all_day=m.all_day, status=m.status,
                                                attributes=dict(m.attributes), provenance=[m.prov]))
                clusters.append((other, [m]))
                if entity.provenance and m.prov.source_type is not entity.provenance[0].source_type or m.prov.source_id != entity.provenance[0].source_id:
                    result.conflicts.append(DateConflict(entity.name, "date", entity.provenance[0], entity.when, entity.all_day, m.prov, m.when, m.all_day))  # type: ignore[arg-type]
                graph.add_relationship(Relationship(other.entity_id, RelationKind.SAME_AS, entity.entity_id, Confidence.LOW,
                                                    "the names match but the days differ, so they are kept separate", (m.prov,)))
        for entity, members in clusters:
            if len(members) > 1:
                self._time_conflicts(entity, members, zone, result)
        for c in commitments:
            if c.kind is not CommitmentKind.EVENT or c.when is None:
                continue
            for entity, members in clusters:
                if any(x.prov.source_id == c.source.source_id and x.prov.source_type is c.source.source_type and x.title == c.title for x in members):
                    resolved[c.key] = entity.entity_id
                    break
        # A candidate that was resolved INTO a calendar/record entity keeps the fact that an email/document/memory mentioned it.
        for entity, members in clusters:
            candidates = [x for x in members if x.attributes.get("candidate")]
            if candidates and len(members) > len(candidates):
                entity.attributes["mentioned_by"] = [x.prov.source_type.value for x in candidates]
                entity.attributes["on_calendar"] = any(x.prov.source_type is SourceKind.CALENDAR for x in members)
            elif candidates:
                entity.attributes["on_calendar"] = False
            else:
                entity.attributes["on_calendar"] = entity.provenance[0].source_type is SourceKind.CALENDAR
        return resolved

    @staticmethod
    def _merge(entity: Entity, m: _Mention, zone) -> None:
        for p in [m.prov]:
            entity.add_provenance(p)
        if entity.when is not None and m.when is not None and m.prov.source_type is SourceKind.CALENDAR:
            entity.when, entity.all_day = m.when, m.all_day  # the calendar is the source of truth for time
        if EntityKind.EVENT is entity.kind and m.kind is not EntityKind.EVENT:
            entity.kind = m.kind  # a more specific description wins ("meeting" -> "project review")

    @staticmethod
    def _time_conflicts(entity: Entity, members: list[_Mention], zone, result: ContextResult) -> None:
        timed = [m for m in members if m.when is not None and not m.all_day]
        for i, a in enumerate(timed):
            for b in timed[i + 1:]:
                if a.prov.source_type is b.prov.source_type:
                    continue
                if abs((a.when - b.when).total_seconds()) >= 1800:  # type: ignore[operator]
                    result.conflicts.append(DateConflict(entity.name, "time", a.prov, a.when, False, b.prov, b.when, False))  # type: ignore[arg-type]
                    return

    # ---- commitments -> entities ----------------------------------------------------------------------------------------

    def _apply_commitments(self, graph: ContextGraph, snap: Snapshot, commitments: list[Commitment], clusters: dict[str, str],
                           task_entities: dict[str, Entity], result: ContextResult) -> None:
        now = snap.now
        events_by_id = {c.key: c for c in commitments if c.kind is CommitmentKind.EVENT}
        source_events: dict[tuple[str, str], list[str]] = {}
        for c in commitments:
            if c.kind is CommitmentKind.EVENT and c.key in clusters:
                source_events.setdefault((c.source.source_type.value, c.source.source_id), []).append(clusters[c.key])
        for c in commitments:
            src = c.source
            if src.source_type is SourceKind.EMAIL:
                item = next((e for e in snap.emails if e.message_id == src.source_id), None)
                email = graph.add_entity(Entity(f"email:{src.source_id}", EntityKind.EMAIL, (item.subject if item else src.label) or src.label, when=src.source_timestamp,
                                                attributes={"message_id": src.source_id}, provenance=[src]))
                self._sender(graph, snap, src, email)
            if c.kind is CommitmentKind.EVENT:
                event_id = clusters.get(c.key)
                if event_id and src.source_type is SourceKind.EMAIL:
                    graph.add_relationship(Relationship(f"email:{src.source_id}", RelationKind.REFERENCES, event_id, c.confidence,
                                                        "the email names this event and its time", (src,)))
                if event_id and c.event_type in (EventType.INTERVIEW,):
                    self._organization(graph, c, event_id)
                continue
            if c.kind is CommitmentKind.TASK:
                self._apply_task(graph, snap, c, source_events, task_entities, result)
            else:
                self._apply_deadline(graph, snap, c, task_entities, result)
        _ = events_by_id, now

    def _sender(self, graph: ContextGraph, snap: Snapshot, src: Provenance, email: Entity) -> None:
        item = next((e for e in snap.emails if e.message_id == src.source_id), None)
        if item is None or item.sender in ("", "a sender"):
            return
        pid = f"person:{item.sender.lower()}"
        graph.add_entity(Entity(pid, EntityKind.PERSON, item.sender, provenance=[Provenance(SourceKind.EMAIL, src.source_id, src.label, src.source_timestamp, snap.now, Confidence.HIGH, pid)]))
        graph.add_relationship(Relationship(email.entity_id, RelationKind.REFERENCES, pid, Confidence.HIGH, "the email was sent by this person", (src,)))

    def _organization(self, graph: ContextGraph, c: Commitment, event_id: str) -> None:
        m = _ORG.search(c.evidence)
        if not m:
            return
        name = m.group(1).strip(" .,")
        if name.lower() in _ORG_STOP or len(name) < 3:
            return
        oid = f"org:{name.lower()}"
        graph.add_entity(Entity(oid, EntityKind.ORGANIZATION, name, provenance=[c.source]))
        graph.add_relationship(Relationship(event_id, RelationKind.RELATES_TO, oid, Confidence.MEDIUM, f"the text says '{c.evidence[:80]}'", (c.source,)))

    def _apply_task(self, graph: ContextGraph, snap: Snapshot, c: Commitment, source_events: dict, task_entities: dict[str, Entity], result: ContextResult) -> None:
        match = self._similar_task(c.title, task_entities)
        deadline_prov = c.source
        if match is not None:
            entity = match
            entity.add_provenance(Provenance(c.source.source_type, c.source.source_id, c.source.label, c.source.source_timestamp, snap.now, c.confidence, entity.entity_id, c.evidence))
            proposal_id = f"prop:{stable_id(c.title, c.when.date().isoformat() if c.when else '')}"
            result.linked_existing[proposal_id] = entity.entity_id
            entity.attributes.setdefault("also_requested_by", []).append(c.source.label)
        else:
            pid = f"cand-task:{stable_id(c.title, c.when.date().isoformat() if c.when else '')}"
            entity = graph.add_entity(Entity(pid, EntityKind.TASK, c.title, when=c.when, status="candidate",
                                             attributes={"candidate": True, "project_hint": c.project_hint or "", "flagged": c.flagged, "evidence": c.evidence},
                                             provenance=[c.source]))
            result.proposals.append(TaskProposal(pid, c.title, c.when, c.all_day, c.project_hint, c.source, c.confidence, c.flagged))
        if c.when is None:
            return
        dl = self._deadline_entity(graph, snap, c, entity, result)
        if c.relative_to:
            event_ids = source_events.get((c.source.source_type.value, c.source.source_id), [])
            for eid in event_ids:
                ev = graph.get(eid)
                if ev is not None and tokens(c.relative_to) & tokens(ev.name):
                    graph.add_relationship(Relationship(eid, RelationKind.HAS_DEADLINE, dl.entity_id, c.confidence,
                                                        f"the text says to do this 'before the {c.relative_to}'", (deadline_prov,)))
        _ = dl

    def _apply_deadline(self, graph: ContextGraph, snap: Snapshot, c: Commitment, task_entities: dict[str, Entity], result: ContextResult) -> None:
        if c.when is None:
            return
        target = self._similar_task(c.title, task_entities)
        self._deadline_entity(graph, snap, c, target, result)

    def _deadline_entity(self, graph: ContextGraph, snap: Snapshot, c: Commitment, target: Entity | None, result: ContextResult) -> Entity:
        assert c.when is not None
        did = f"dl:{stable_id(target.entity_id if target else c.title, c.when.isoformat())}"
        kind = c.deadline_kind or DeadlineKind.SUBMISSION
        dl = graph.add_entity(Entity(did, EntityKind.DEADLINE, c.title, when=c.when, all_day=c.all_day, status="open",
                                     attributes={"deadline_kind": kind.value, "flagged": c.flagged}, provenance=[c.source]))
        if target is not None:
            graph.add_relationship(Relationship(did, RelationKind.APPLIES_TO, target.entity_id, c.confidence, "the text asks for this task by that date", (c.source,)))
        if not any(d.deadline_id == did for d in result.deadlines):
            result.deadlines.append(Deadline(did, kind, c.when.astimezone(snap.zone), snap.zone.key, c.all_day, c.evidence[:200], c.confidence, c.source,
                                             target.entity_id if target else None, status_for(c.when, snap.now, confidence=c.confidence), c.is_bound, c.title))
        return dl

    def _similar_task(self, title: str, task_entities: dict[str, Entity]) -> Entity | None:
        best: tuple[float, Entity] | None = None
        for entity in task_entities.values():
            if entity.status in ("completed", "cancelled"):
                continue
            score, shared = same_thing_score(title, entity.name)
            if score >= MERGE_THRESHOLD and (distinctive(shared) or len(shared) >= 2) and (best is None or score > best[0]):
                best = (score, entity)
        return best[1] if best else None

    # ---- relationships between existing entities --------------------------------------------------------------------

    def _link_tasks_to_events(self, graph: ContextGraph, task_entities: dict[str, Entity]) -> None:
        events = [e for e in graph.entities.values() if e.kind in EVENT_LIKE and e.entity_id.startswith(("cal:", "rec:"))]
        for task in task_entities.values():
            if task.status in ("completed", "cancelled"):
                continue
            t_tokens = tokens(task.name, drop_verbs=True) - _SUPPORT_NOUNS
            for ev in events:
                e_tokens = tokens(ev.name)
                shared = t_tokens & e_tokens
                if not t_tokens or not e_tokens or not shared:
                    continue
                contained = t_tokens <= e_tokens
                d_task, d_ev = distinctive(t_tokens), distinctive(e_tokens)
                conflicting = bool(d_task and d_ev and not (d_task & d_ev))
                if contained and not conflicting and (len(shared) >= 2 or distinctive(shared)):
                    graph.add_relationship(Relationship(
                        task.entity_id, RelationKind.RELATES_TO, ev.entity_id, Confidence.MEDIUM,
                        f"the task title '{task.name}' matches the event name '{ev.name}'", tuple(task.provenance[:1] + ev.provenance[:1])))

    def _add_dependencies(self, graph: ContextGraph, snap: Snapshot, task_entities: dict[str, Entity]) -> None:
        if self._deps is None:
            return
        for task_id, dep_id in self._deps.all_edges():
            a, b = task_entities.get(task_id), task_entities.get(dep_id)
            if a is not None and b is not None:
                graph.add_relationship(Relationship(a.entity_id, RelationKind.DEPENDS_ON, b.entity_id, Confidence.HIGH, "you told me this task depends on that one", tuple(a.provenance[:1])))

    def _task_deadlines(self, graph: ContextGraph, snap: Snapshot, task_entities: dict[str, Entity], result: ContextResult) -> None:
        for t in snap.tasks:
            if t.due_at is None or not t.is_open:
                continue
            entity = task_entities[t.task_id]
            did = f"dl:{stable_id(entity.entity_id, t.due_at.astimezone(snap.zone).isoformat())}"
            prov = Provenance(SourceKind.TASK, t.task_id, f"task '{t.title}'", t.created_at, snap.now, Confidence.HIGH, did)
            all_day = t.due_at.astimezone(snap.zone).hour == 23 and t.due_at.astimezone(snap.zone).minute == 59
            if graph.get(did) is None:
                graph.add_entity(Entity(did, EntityKind.DEADLINE, t.title, when=t.due_at, all_day=all_day, status="open",
                                        attributes={"deadline_kind": classify_deadline_kind(t.title).value}, provenance=[prov]))
                graph.add_relationship(Relationship(did, RelationKind.APPLIES_TO, entity.entity_id, Confidence.HIGH, "the task's own due date", (prov,)))
            else:
                graph.get(did).add_provenance(prov)  # type: ignore[union-attr]
            if not any(d.deadline_id == did for d in result.deadlines):
                result.deadlines.append(Deadline(did, classify_deadline_kind(t.title), t.due_at.astimezone(snap.zone), snap.zone.key, all_day, t.title[:120],
                                                 Confidence.HIGH, prov, entity.entity_id, status_for(t.due_at, snap.now), False, t.title))


    # ---- Integration Hub items (Phase 18) ------------------------------------------------------------------------------------

    def _apply_external(self, graph: ContextGraph, snap: Snapshot, result: ContextResult) -> None:
        """GitHub repositories, commits, issues and pull requests; deadlines found in messages; hackathon registrations. Each carries provenance."""
        now = snap.now
        repos: dict[str, Entity] = {}
        for x in snap.external:
            if x.source == "github" and x.kind == "repository":
                prov = Provenance(SourceKind.GITHUB, x.source_id, f"GitHub repository {x.title}", x.when, now, Confidence.HIGH, f"gh:repo:{x.source_id}")
                repos[x.source_id] = graph.add_entity(Entity(f"gh:repo:{x.source_id}", EntityKind.REPOSITORY, x.title, when=x.when,
                                                             attributes={"language": x.meta("language"), "open_issues": x.meta("open_issues"), "projects": list(x.meta("projects", []) or [])}, provenance=[prov]))
        for x in snap.external:
            if x.source == "github" and x.kind in ("commit", "issue", "pull_request"):
                repo_name = str(x.meta("repo", ""))
                repo = repos.get(repo_name)
                kind = {"commit": EntityKind.COMMIT, "issue": EntityKind.ISSUE, "pull_request": EntityKind.PULL_REQUEST}[x.kind]
                prov = Provenance(SourceKind.GITHUB, x.source_id, f"GitHub {x.kind.replace('_', ' ')} in {repo_name}", x.when, now, Confidence.HIGH, f"gh:{x.source_id}")
                e = graph.add_entity(Entity(f"gh:{x.source_id}", kind, x.title, when=x.when, status=str(x.meta("state", "")) or None,
                                            attributes={"repo": repo_name, "author": x.meta("author")}, provenance=[prov]))
                if repo is not None:
                    graph.add_relationship(Relationship(e.entity_id, RelationKind.BELONGS_TO, repo.entity_id, Confidence.HIGH, f"it is in the repository {repo_name}", (prov,)))
            elif x.kind == "deadline" and x.when is not None and x.source != "github":
                src = SourceKind.MESSAGE if x.source == "telegram" else SourceKind.EMAIL
                prov = Provenance(src, x.source_id, f"a message ({x.title[:40]})", x.when, now, x.confidence, f"ext:{x.source}:{x.source_id}", str(x.meta("evidence", ""))[:200])
                if x.meta("injection_suspected") or x.when < now - timedelta(days=1):
                    continue
                did = f"dl:{stable_id('ext', x.source_id, x.when.isoformat())}"
                kind = classify_deadline_kind(str(x.meta("evidence", "")) or x.title)
                graph.add_entity(Entity(did, EntityKind.DEADLINE, x.title, when=x.when, status="open", attributes={"deadline_kind": kind.value}, provenance=[prov]))
                if not any(d.deadline_id == did for d in result.deadlines):
                    result.deadlines.append(Deadline(did, kind, x.when.astimezone(snap.zone), snap.zone.key, x.when.hour == 23 and x.when.minute == 59, str(x.meta("evidence", ""))[:200],
                                                     x.confidence, prov, None, status_for(x.when, now, confidence=x.confidence), False, x.title))
            elif x.kind == "event" and x.meta("registration"):
                prov = Provenance(SourceKind.EMAIL, x.source_id.split("#")[0], f"email about {x.title[:40]}", x.when, now, x.confidence, f"reg:{x.source_id}", str(x.meta("evidence", ""))[:200])
                graph.add_entity(Entity(f"reg:{stable_id(x.title)}", classify_event_kind(x.title), x.title, status="registered" if x.meta("registration") == "completed" else "mentioned",
                                        attributes={"registration": x.meta("registration"), "location": x.meta("location")}, provenance=[prov]))
