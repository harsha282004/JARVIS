"""Controlled multi-step, READ-ONLY workflows.

"Prepare me for tomorrow's project review":
    1. find the event            2. identify its project        3. find related documents
    4. find pending tasks        5. find related emails         6. summarize          7. offer a checklist

Each step reads from the context graph (built from the user's real sources) and records what it did, so the answer can say how it was
found. A step that finds nothing says so; nothing is invented to fill a section. No step changes any system; the checklist is a
suggestion the user may act on.
"""

from dataclasses import dataclass
from datetime import date, datetime, timedelta

from agent.intelligence.context_engine import ContextResult
from agent.intelligence.findings import Finding, FindingKind
from agent.intelligence.models import Answer, Entity, EntityKind, EVENT_LIKE, Provenance, RelationKind, Snapshot, Statement, fact, inference, suggestion
from agent.intelligence.phrasing import day_word, join_and, quoted, status_word, when_phrase
from agent.intelligence.textnorm import contains_phrase, distinctive, same_thing_score, tokens

PLANNING_KINDS = (RelationKind.BELONGS_TO, RelationKind.RELATES_TO, RelationKind.REFERENCES, RelationKind.HAS_DEADLINE, RelationKind.APPLIES_TO)


@dataclass
class StepLog:
    steps: list[str]

    def add(self, text: str) -> None:
        self.steps.append(text)


class Workflows:
    def __init__(self, zone, rag_search=None):
        self._zone = zone
        self._rag_search = rag_search  # optional: query -> list[(title, page or None)]; only results the RAG layer judged relevant

    # ---- helpers -----------------------------------------------------------------------------------------------------

    def find_events(self, result: ContextResult, query: str, day: date | None, now: datetime) -> list[Entity]:
        q_tokens = tokens(query)
        out: list[tuple[float, Entity]] = []
        for e in result.graph.entities.values():
            if e.kind not in EVENT_LIKE or e.when is None or e.when < now - timedelta(hours=2):
                continue
            if day is not None and e.when.astimezone(self._zone).date() != day:
                continue
            score = 1.0 if not q_tokens else same_thing_score(query, e.name, verbs=True)[0]
            if q_tokens and score < 0.5 and not (q_tokens <= tokens(e.name)):
                continue
            out.append((score, e))
        out.sort(key=lambda x: (-x[0], x[1].when or now))
        # calendar-backed entities first; the same event described by an email is already merged into them
        return [e for _, e in out]

    def _related(self, result: ContextResult, entity_id: str) -> list[tuple[str, Entity]]:
        found: dict[str, tuple[str, Entity]] = {}
        graph = result.graph
        for rel, other in graph.related(entity_id):
            found[other.entity_id] = (rel.reason, other)
            if other.kind is EntityKind.DEADLINE:  # event -> deadline -> task
                for r2, third in graph.related(other.entity_id):
                    if third.kind is EntityKind.TASK:
                        found.setdefault(third.entity_id, (r2.reason, third))
            if other.kind is EntityKind.PROJECT:  # event -> project -> everything on that project
                for r2, third in graph.related(other.entity_id):
                    if third.entity_id != entity_id and third.kind in (EntityKind.TASK, EntityKind.DOCUMENT, EntityKind.EMAIL, EntityKind.ASSIGNMENT):
                        found.setdefault(third.entity_id, (r2.reason, third))
        return list(found.values())

    # ---- prepare for an event ----------------------------------------------------------------------------------------

    def prepare_for(self, snap: Snapshot, result: ContextResult, findings: list[Finding], query: str, day: date | None) -> Answer:
        log = StepLog([])
        matches = self.find_events(result, query, day, snap.now)
        log.add("looked for the event")
        if not matches:
            where = f" {day_word(day, snap.now, self._zone)}" if day else ""
            return Answer(f"I couldn't find an event matching that{where} in your calendar or saved events, so I can't prepare a summary.", subject="event preparation")
        if len(matches) > 1 and len({m.name.lower() for m in matches[:2]}) > 1 and same_thing_score(matches[0].name, matches[1].name, verbs=True)[0] < 0.75:
            names = [f"'{m.name}' {when_phrase(m.when, snap.now, self._zone, m.all_day)}" for m in matches[:3]]  # type: ignore[arg-type]
            return Answer("Which one do you mean: " + join_and(names) + "?", subject="event preparation")
        event = matches[0]
        assert event.when is not None
        stmts: list[Statement] = [fact(f"{quoted(event.name)} is {when_phrase(event.when, snap.now, self._zone, event.all_day)}.", *event.provenance[:1])]
        related = self._related(result, event.entity_id)
        log.add("found related items through the project, deadlines and links between sources")

        projects = [o for _, o in related if o.kind is EntityKind.PROJECT]
        tasks = [(why, o) for why, o in related if o.kind in (EntityKind.TASK, EntityKind.ASSIGNMENT) and o.status not in ("completed", "cancelled", "candidate")]
        docs = [(why, o) for why, o in related if o.kind is EntityKind.DOCUMENT]
        emails = [(why, o) for why, o in related if o.kind is EntityKind.EMAIL]
        parts = [stmts[0].text]
        if projects:
            parts.append(f"It belongs to your {projects[0].name} project.")
            stmts.append(inference(f"It appears to belong to your {projects[0].name} project.", *event.provenance[:1], *projects[0].provenance[:1]))
        if tasks:
            listing = join_and([f"{quoted(o.name)} ({status_word(o.status or 'pending')}{', due ' + when_phrase(o.when, snap.now, self._zone) if o.when else ''})" for _, o in tasks[:5]])
            parts.append(f"Pending tasks: {listing}.")
            stmts += [fact(f"Your task {quoted(o.name)} is {status_word(o.status or 'pending')}.", *o.provenance[:1]) for _, o in tasks[:5]]
        else:
            parts.append("I don't see any pending task connected to it.")
        doc_titles = [o.name for _, o in docs]
        if self._rag_search is not None:
            try:
                for title, page in self._rag_search(event.name)[:3]:
                    if title not in doc_titles:
                        doc_titles.append(title)
            except Exception:  # noqa: BLE001 - documents are optional; a failure is not an answer
                log.add("the document search failed")
        if doc_titles:
            parts.append("Related documents: " + join_and([quoted(t) for t in doc_titles[:4]]) + ".")
            stmts += [fact(f"One of your documents is titled {quoted(t)} and looks related.", *(docs[0][1].provenance[:1] if docs else ())) for t in doc_titles[:1]]
        else:
            parts.append("I couldn't find a related document in your indexed files.")
        if emails:
            parts.append("Related email: " + join_and([f"{quoted(o.name)}" for _, o in emails[:3]]) + ".")
            stmts += [fact(f"An email is linked to it: {o.name}.", *o.provenance[:1]) for _, o in emails[:2]]
        else:
            parts.append("I don't see an email linked to it.")
        for f in findings:
            if event.entity_id in f.entity_ids and f.kind in (FindingKind.MEMORY_CONFLICT, FindingKind.SOURCE_CONFLICT, FindingKind.CALENDAR_OVERLAP):
                parts.append(f.spoken(with_suggestion=False))
        checklist = [o.name for _, o in tasks[:4]] + [f"Look over {t}" for t in doc_titles[:2]] + ["Confirm the time and place"]
        checklist_text = "A possible checklist: " + "; ".join(f"{i}. {c}" for i, c in enumerate(checklist, start=1)) + "."
        stmts.append(suggestion(checklist_text, *event.provenance[:1]))
        parts.append(checklist_text)
        parts.append("I haven't changed anything.")
        return Answer(" ".join(parts), tuple(stmts), (event.entity_id, *[o.entity_id for _, o in tasks]), detail="Steps: " + "; ".join(log.steps) + ".", subject=f"preparing for {event.name}")

    # ---- project status ----------------------------------------------------------------------------------------------

    def find_project(self, result: ContextResult, name: str) -> Entity | None:
        best: tuple[int, Entity] | None = None
        for e in result.graph.of_kind(EntityKind.PROJECT):
            if contains_phrase(name, e.name) or contains_phrase(e.name, name):
                score = len(e.name)
                if best is None or score > best[0]:
                    best = (score, e)
        return best[1] if best else None

    def project_status(self, snap: Snapshot, result: ContextResult, name: str, findings: list[Finding]) -> Answer:
        project = self.find_project(result, name)
        if project is None:
            return Answer(f"I don't have a project called {quoted(name)} in your tasks, calendar, documents or memory.", subject=name)
        graph = result.graph
        tasks, events, docs, emails, repos = [], [], [], [], []
        for rel, other in graph.related(project.entity_id):
            if other.kind in (EntityKind.TASK, EntityKind.ASSIGNMENT):
                (tasks if other.status not in ("completed", "cancelled") else []).append(other)
            elif other.kind in EVENT_LIKE and other.when is not None and other.when >= snap.now - timedelta(hours=2):
                events.append(other)
            elif other.kind is EntityKind.DOCUMENT:
                docs.append(other)
            elif other.kind is EntityKind.EMAIL:
                emails.append(other)
            elif other.kind is EntityKind.REPOSITORY:
                repos.append(other)
        tasks.sort(key=lambda t: (t.status == "candidate", t.when is None, t.when or snap.now))
        events.sort(key=lambda e: e.when)  # type: ignore[arg-type,return-value]
        stmts: list[Statement] = []
        parts = []
        real_tasks = [t for t in tasks if t.status != "candidate"]
        if real_tasks:
            parts.append("Pending tasks: " + join_and([f"{quoted(t.name)} ({status_word(t.status or 'pending')}{', due ' + when_phrase(t.when, snap.now, self._zone) if t.when else ''})" for t in real_tasks[:6]]) + ".")
            stmts += [fact(f"Your task {quoted(t.name)} is {status_word(t.status or 'pending')}.", *t.provenance[:1]) for t in real_tasks[:6]]
        else:
            parts.append(f"You have no open tasks for {project.name}.")
        if events:
            parts.append("Coming up: " + join_and([f"{quoted(e.name)} {when_phrase(e.when, snap.now, self._zone, e.all_day)}" for e in events[:4]]) + ".")  # type: ignore[arg-type]
            stmts += [fact(f"{quoted(e.name)} is {when_phrase(e.when, snap.now, self._zone, e.all_day)}.", *e.provenance[:1]) for e in events[:4]]  # type: ignore[arg-type]
        if docs:
            parts.append("Documents: " + join_and([quoted(d.name) for d in docs[:4]]) + ".")
        if emails:
            parts.append(f"{len(emails)} related email{'s' if len(emails) != 1 else ''}: " + join_and([quoted(e.name, 50) for e in emails[:3]]) + ".")
        for repo in repos:
            commits = sorted((o for _, o in graph.related(repo.entity_id) if o.kind is EntityKind.COMMIT and o.when), key=lambda c: c.when, reverse=True)  # type: ignore[arg-type,return-value]
            issues = [o for _, o in graph.related(repo.entity_id) if o.kind is EntityKind.ISSUE and (o.status or "open") == "open"]
            pulls = [o for _, o in graph.related(repo.entity_id) if o.kind is EntityKind.PULL_REQUEST and (o.status or "open") == "open"]
            line = f"GitHub {repo.name}: {len(issues)} open issue{'s' if len(issues) != 1 else ''}, {len(pulls)} open pull request{'s' if len(pulls) != 1 else ''}"
            if commits:
                line += f"; latest commit {quoted(commits[0].name, 60)} on {commits[0].when.strftime('%b')} {commits[0].when.day}"  # type: ignore[union-attr]
            parts.append(line + ".")
            stmts.append(fact(line + ".", *repo.provenance[:1], *(commits[0].provenance[:1] if commits else ())))
        proposals = [f for f in findings if f.kind is FindingKind.TASK_PROPOSAL and project.name.lower() in f.title.lower()]
        if proposals:
            parts.append("An email also asks for " + join_and([quoted(f.title.replace('Possible task: ', '')) for f in proposals[:2]]) + ", which isn't a task yet.")
        for m in project.provenance[:1]:
            stmts.append(fact(f"Your stored memory mentions the {project.name} project.", m))
        return Answer(f"Here's what I found for {project.name}. " + " ".join(parts), tuple(stmts), (project.entity_id,), subject=f"the {project.name} project")
