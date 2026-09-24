"""Deterministic entity-name canonicalization. No LLM, no fuzzy matching.

Identity is (entity_type, name_key). The key is the name lowercased, with
punctuation and diacritics folded, whitespace collapsed, and ONE generic
trailing descriptor removed for that type ("Python programming language" ->
"python", "JARVIS assistant" -> "jarvis", "Virtual Campus project" ->
"virtual campus"). Nothing else is merged: "Jarvis Mark 2", "JavaScript" and
"Java" stay distinct from "Jarvis" and "Java", and the same name under two
types ("Java" the technology vs the island) is two entities.
"""

import re
import unicodedata

from agent.knowledge_graph.models import EntityType

_SUFFIXES: dict[EntityType, tuple[str, ...]] = {
    EntityType.TECHNOLOGY: (
        "programming language", "programming", "language", "framework", "library", "database", "tool", "platform",
    ),
    EntityType.PROJECT: ("project", "application", "app", "assistant", "system"),
    EntityType.ORGANIZATION: ("organization", "organisation", "company"),
    EntityType.TOPIC: ("subject", "field"),
    EntityType.SKILL: ("skill", "skills"),
}
USER_NAME = "User"
USER_KEY = "user"
_PUNCT = re.compile(r"[^\w+#. ]+")


def fold_text(text: str) -> str:
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c)).casefold()
    text = _PUNCT.sub(" ", text)
    words = (w.strip(".") for w in text.replace("_", " ").split())
    return " ".join(w for w in words if w)  # "Node.js" keeps its dot; "postgresql." loses the sentence period


def name_key(name: str, entity_type: EntityType) -> str:
    """Identity key for `name` as an entity of `entity_type`."""
    key = fold_text(name)
    for suffix in _SUFFIXES.get(entity_type, ()):
        if key.endswith(" " + suffix):
            stripped = key[: -len(suffix)].strip()
            if stripped:  # never reduce a name to nothing
                key = stripped
            break
    return key
