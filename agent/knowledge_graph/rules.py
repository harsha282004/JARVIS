"""The graph's controlled schema: which relationships may connect which entity types.

The LLM never extends this schema. Unknown relationship or entity types are
rejected; only combinations listed here are accepted.
"""

from agent.knowledge_graph.models import EntityType as E
from agent.knowledge_graph.models import RelationshipType as R

_ANY = frozenset(E)
_NON_DOCUMENT = frozenset(E) - {E.DOCUMENT}

# relationship -> (allowed source types, allowed target types)
ALLOWED: dict[R, tuple[frozenset[E], frozenset[E]]] = {
    R.WORKS_ON: (frozenset({E.PERSON}), frozenset({E.PROJECT})),
    R.USES: (frozenset({E.PERSON, E.PROJECT, E.ORGANIZATION}), frozenset({E.TECHNOLOGY})),
    R.KNOWS: (frozenset({E.PERSON}), frozenset({E.TECHNOLOGY, E.SKILL, E.TOPIC, E.PERSON})),
    R.PREFERS: (frozenset({E.PERSON}), frozenset({E.TECHNOLOGY, E.SKILL, E.TOPIC})),
    R.STUDIES: (frozenset({E.PERSON}), frozenset({E.TOPIC, E.SKILL})),
    R.WORKS_AT: (frozenset({E.PERSON}), frozenset({E.ORGANIZATION})),
    R.PART_OF: (_NON_DOCUMENT, _NON_DOCUMENT),
    R.RELATED_TO: (_ANY, _ANY),
    R.MENTIONS: (frozenset({E.DOCUMENT}), _NON_DOCUMENT),
    R.DOCUMENTED_IN: (frozenset({E.PROJECT, E.TECHNOLOGY, E.TOPIC, E.ORGANIZATION, E.SKILL}), frozenset({E.DOCUMENT})),
    R.HAS_SKILL: (frozenset({E.PERSON}), frozenset({E.SKILL})),
    R.HAS_GOAL: (frozenset({E.PERSON}), frozenset({E.GOAL})),
    R.LOCATED_IN: (frozenset({E.PERSON, E.ORGANIZATION}), frozenset({E.LOCATION})),
    R.DEPENDS_ON: (frozenset({E.PROJECT, E.TECHNOLOGY}), frozenset({E.PROJECT, E.TECHNOLOGY})),
}

# (source type, relationship) pairs where an entity can hold only ONE current target.
# A newer fact of equal or higher trust replaces the old one; a lower-trust fact never does.
SINGLE_VALUED: frozenset[tuple[E, R]] = frozenset({(E.PERSON, R.LOCATED_IN)})


def is_allowed(source: E, relationship: R, target: E) -> bool:
    sources, targets = ALLOWED[relationship]
    return source in sources and target in targets


# Deterministic typing of names that carry no type of their own (memory -> graph).
KNOWN_TECHNOLOGIES = frozenset(
    """python java javascript typescript kotlin swift go golang rust c c++ c# ruby php scala r sql bash
    fastapi django flask spring react angular vue node nodejs express next nextjs tailwind html css
    postgresql postgres mysql sqlite mongodb redis docker kubernetes linux windows git github ollama
    pytorch tensorflow numpy pandas sqlalchemy alembic pgvector langchain langgraph""".split()
)

# Words in a question that select an entity type ("what projects ...").
TYPE_WORDS: dict[str, E] = {
    "project": E.PROJECT, "projects": E.PROJECT,
    "technology": E.TECHNOLOGY, "technologies": E.TECHNOLOGY, "tech": E.TECHNOLOGY, "stack": E.TECHNOLOGY,
    "framework": E.TECHNOLOGY, "frameworks": E.TECHNOLOGY, "library": E.TECHNOLOGY, "libraries": E.TECHNOLOGY,
    "language": E.TECHNOLOGY, "languages": E.TECHNOLOGY,
    "document": E.DOCUMENT, "documents": E.DOCUMENT, "file": E.DOCUMENT, "files": E.DOCUMENT,
    "skill": E.SKILL, "skills": E.SKILL, "goal": E.GOAL, "goals": E.GOAL,
    "organization": E.ORGANIZATION, "organizations": E.ORGANIZATION, "company": E.ORGANIZATION,
    "location": E.LOCATION, "topic": E.TOPIC, "topics": E.TOPIC, "person": E.PERSON, "people": E.PERSON,
}
FIRST_PERSON = frozenset({"i", "my", "me", "mine", "myself", "im"})
