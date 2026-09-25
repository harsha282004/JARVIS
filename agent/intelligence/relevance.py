"""Memory relevance: which stored memories are worth sending to the language model for THIS request.

Existing retrieval finds candidates by keyword. This ranks them by several transparent signals so an irrelevant memory is left out
rather than padded into the prompt:

    semantic   how many of the request's meaningful words the memory contains (cosine on word sets)
    recency    newer memories count more (half-life 90 days)
    importance stated with HIGH confidence, and goals/context memories, count more
    source     what the user explicitly said outranks what was inferred
    context    the memory mentions the active project/entity or the entity relationships (a task, event or project related to it)

A memory must clear a minimum semantic or context score to be included at all, and the score of every included memory is inspectable.
"""

import math
from dataclasses import dataclass
from datetime import datetime

from agent.intelligence.models import ContextGraph, MemoryItem, utcnow
from agent.intelligence.textnorm import tokens
from agent.memory.models import Confidence

HALF_LIFE_DAYS = 90.0
MIN_RELEVANCE = 0.12
_KIND_WEIGHT = {"goal": 1.0, "context": 1.0, "profile": 0.8, "preference": 0.7, "fact": 0.7}


@dataclass(frozen=True)
class RankedMemory:
    memory: MemoryItem
    score: float
    reasons: tuple[str, ...]


def semantic(query: frozenset[str], content: frozenset[str]) -> float:
    if not query or not content:
        return 0.0
    return len(query & content) / math.sqrt(len(query) * len(content))


def rank_memories(memories: list[MemoryItem], query: str, *, active_terms: frozenset[str] = frozenset(), graph: ContextGraph | None = None,
                  now: datetime | None = None, limit: int = 5) -> list[RankedMemory]:
    now = now or utcnow()
    q = tokens(query)
    context_terms = set(active_terms)
    if graph is not None:
        for e in graph.entities.values():
            if any(t in q for t in tokens(e.name)):
                for _, other in graph.related(e.entity_id):
                    context_terms |= set(tokens(other.name))
                context_terms |= set(tokens(e.name))
    out: list[RankedMemory] = []
    for m in memories:
        c = tokens(m.content)
        sem = semantic(q, c)
        ctx = semantic(frozenset(context_terms), c) if context_terms else 0.0
        if sem < MIN_RELEVANCE and ctx < MIN_RELEVANCE * 2:
            continue  # nothing links this memory to the request or the active context
        age_days = max(0.0, (now - m.created_at).total_seconds() / 86400)
        recency = 0.5 ** (age_days / HALF_LIFE_DAYS)
        importance = (int(m.confidence) / int(Confidence.HIGH)) * _KIND_WEIGHT.get(m.kind, 0.7)
        source = 1.0 if m.explicit else 0.6
        score = 0.45 * sem + 0.20 * ctx + 0.15 * recency + 0.10 * importance + 0.10 * source
        reasons = tuple(r for r, ok in (("matches the request", sem >= MIN_RELEVANCE), ("relates to the active context", ctx >= MIN_RELEVANCE),
                                        ("recent", recency > 0.5), ("stated by you", m.explicit), ("high confidence", m.confidence is Confidence.HIGH)) if ok)
        out.append(RankedMemory(m, round(score, 4), reasons))
    out.sort(key=lambda r: (-r.score, r.memory.memory_id))
    return out[:limit]
