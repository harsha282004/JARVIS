"""Conversation references: "When is it due?", "What about that meeting?", "Who sent the email?" without repeating names.

`ConversationContext` remembers the entities that recent turns were about (found by matching entity names from the user's own data
against what was said and what JARVIS answered). `resolve()` turns a reference phrase into one entity, or says it is ambiguous, or
says there is nothing to refer to. It never guesses: if two different things were just discussed and the phrase does not say which,
the answer is a question ("Do you mean X or Y?").

Context expires after a period of inactivity, like the conversation session itself.
"""

import re
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from agent.intelligence.models import ContextGraph, Entity, EntityKind, EVENT_LIKE, utcnow
from agent.intelligence.textnorm import contains_phrase, distinctive, tokens

CONTEXT_MINUTES = 30
MAX_TURNS = 6

_KIND_WORDS: list[tuple[re.Pattern[str], frozenset[EntityKind]]] = [
    (re.compile(r"\b(?:meeting|call|session)\b", re.I), frozenset({EntityKind.MEETING, EntityKind.PROJECT_REVIEW, EntityKind.EVENT})),
    (re.compile(r"\b(?:review)\b", re.I), frozenset({EntityKind.PROJECT_REVIEW, EntityKind.MEETING, EntityKind.EVENT})),
    (re.compile(r"\b(?:interview)\b", re.I), frozenset({EntityKind.INTERVIEW})),
    (re.compile(r"\b(?:exam|test|quiz)\b", re.I), frozenset({EntityKind.EXAM})),
    (re.compile(r"\b(?:email|mail|message)\b", re.I), frozenset({EntityKind.EMAIL, EntityKind.MESSAGE})),
    (re.compile(r"\b(?:hackathon)\b", re.I), frozenset({EntityKind.HACKATHON})),
    (re.compile(r"\b(?:project)\b", re.I), frozenset({EntityKind.PROJECT})),
    (re.compile(r"\b(?:task|todo|to-do|assignment)\b", re.I), frozenset({EntityKind.TASK, EntityKind.ASSIGNMENT})),
    (re.compile(r"\b(?:deadline)\b", re.I), frozenset({EntityKind.DEADLINE, EntityKind.TASK})),
    (re.compile(r"\b(?:document|doc|file|report)\b", re.I), frozenset({EntityKind.DOCUMENT})),
]
_PRONOUN = re.compile(r"\b(?:it|that|this|those|them|its)\b", re.I)
_PREVIOUS = re.compile(r"\b(?:previous|last|earlier|other)\b", re.I)


@dataclass
class _Turn:
    at: datetime
    entity_ids: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class Resolution:
    entity: Entity | None = None
    candidates: tuple[Entity, ...] = ()
    question: str | None = None  # what to ask when ambiguous or there is nothing to refer to

    @property
    def resolved(self) -> bool:
        return self.entity is not None


class ConversationContext:
    def __init__(self, clock=utcnow):
        self._clock = clock
        self._turns: deque[_Turn] = deque(maxlen=MAX_TURNS)

    def reset(self) -> None:
        self._turns.clear()

    def _fresh(self) -> list[_Turn]:
        cutoff = self._clock() - timedelta(minutes=CONTEXT_MINUTES)
        return [t for t in self._turns if t.at >= cutoff]

    def note(self, entity_ids: list[str]) -> None:
        """Record that the current turn was about these entities (most relevant first)."""
        ids = [i for n, i in enumerate(entity_ids) if i not in entity_ids[:n]]
        if ids:
            self._turns.append(_Turn(self._clock(), ids))

    def observe(self, text: str, graph: ContextGraph) -> list[str]:
        """Find entities whose names appear in `text` (a user message or JARVIS's reply) and remember them."""
        found = mentioned_entities(text, graph)
        self.note([e.entity_id for e in found])
        return [e.entity_id for e in found]

    def recent_entities(self, graph: ContextGraph, kinds: frozenset[EntityKind] | None = None) -> list[Entity]:
        """Distinct entities from newest turn to oldest, still present in `graph`."""
        out: list[Entity] = []
        for turn in reversed(self._fresh()):
            for eid in turn.entity_ids:
                e = graph.get(eid)
                if e is not None and e not in out and (kinds is None or e.kind in kinds):
                    out.append(e)
        return out

    def last_topic(self) -> str | None:
        fresh = self._fresh()
        return fresh[-1].entity_ids[0] if fresh else None

    def resolve(self, phrase: str, graph: ContextGraph) -> Resolution:
        """`phrase` is words like "it", "that meeting", "this project", "the email", "that hackathon", "the previous task"."""
        kinds: frozenset[EntityKind] | None = None
        for pattern, k in _KIND_WORDS:
            if pattern.search(phrase):
                kinds = k
                break
        pool = self.recent_entities(graph, kinds)
        if not pool and kinds is not None:  # "the meeting" with no meeting discussed: look at the graph itself, but only if unique
            upcoming = sorted((e for e in graph.of_kind(*kinds) if e.when is not None), key=lambda e: e.when)  # type: ignore[arg-type,return-value]
            if len(upcoming) == 1:
                return Resolution(upcoming[0])
            if len(upcoming) > 1:
                return Resolution(candidates=tuple(upcoming[:3]), question=_which(upcoming[:3]))
        if not pool:
            return Resolution(question="I'm not sure what you're referring to. Which one do you mean?")
        if _PREVIOUS.search(phrase) and len(pool) > 1:
            return Resolution(pool[1])
        fresh = self._fresh()
        latest_ids = fresh[-1].entity_ids if fresh else []
        latest = [e for e in pool if e.entity_id in latest_ids]
        if kinds is None and len(latest) > 1 and _PRONOUN.search(phrase):
            distinct_names = {e.name.lower() for e in latest}
            if len(distinct_names) > 1 and not _same_family(latest, graph):
                return Resolution(candidates=tuple(latest[:3]), question=_which(latest[:3]))
        return Resolution(pool[0])


def _which(entities: list[Entity]) -> str:
    names = [f"'{e.name}'" for e in entities]
    return "Do you mean " + ", ".join(names[:-1]) + (" or " if len(names) > 1 else "") + names[-1] + "?"


def _same_family(entities: list[Entity], graph: ContextGraph) -> bool:
    """True when the entities are one thing seen from several sides (an event and the email that mentioned it), so "it" is not ambiguous."""
    first = entities[0]
    for other in entities[1:]:
        linked = any(rel.subject_id == other.entity_id or rel.object_id == other.entity_id for rel, _ in graph.related(first.entity_id))
        if not linked:
            return False
    return True


def mentioned_entities(text: str, graph: ContextGraph, limit: int = 4) -> list[Entity]:
    """Entities whose (distinctive) name appears in `text`, longest names first. Emails and people are excluded: they are found via the event."""
    out: list[tuple[int, Entity]] = []
    text_tokens = tokens(text)
    for e in graph.entities.values():
        if e.kind in (EntityKind.USER, EntityKind.EMAIL, EntityKind.PERSON, EntityKind.DEADLINE) or len(e.name) < 4:
            continue
        if contains_phrase(text, e.name) or (e.kind is EntityKind.PROJECT and contains_phrase(text, e.name)):
            out.append((len(e.name), e))
            continue
        name_tokens = tokens(e.name)
        d = distinctive(name_tokens)
        if len(name_tokens) >= 2 and name_tokens <= text_tokens and (d or len(name_tokens) >= 3) and e.kind in (EVENT_LIKE | {EntityKind.TASK}):
            out.append((len(e.name) - 1, e))
    out.sort(key=lambda x: (-x[0], x[1].entity_id))
    result: list[Entity] = []
    for _, e in out:
        if e not in result:
            result.append(e)
    return result[:limit]
