"""KnowledgeGraphInterface: the operations the rest of JARVIS may use on the graph.

Typed models in and out, no SQL, no free-form model text. Implemented by
GraphService (rules) over GraphRepository (persistence).
"""

from abc import ABC, abstractmethod
from collections.abc import Sequence
from typing import Any

from agent.knowledge_graph.models import (
    Confidence,
    Entity,
    EntityType,
    GraphPath,
    Provenance,
    RelatedEntity,
    Relationship,
    RelationshipType,
    TrustLevel,
)


class KnowledgeGraphInterface(ABC):
    @abstractmethod
    def create_entity(
        self, entity_type: EntityType, name: str, description: str | None = None,
        metadata: dict[str, Any] | None = None, confidence: Confidence = Confidence.HIGH,
        trust: TrustLevel = TrustLevel.EXPLICIT_USER,
    ) -> Entity:
        """Return the active entity for this (type, canonical name), creating it only if none exists."""

    @abstractmethod
    def get_entity(self, entity_id: str) -> Entity | None: ...

    @abstractmethod
    def resolve_entity(self, name: str, entity_type: EntityType | None = None) -> Entity | None:
        """Find an entity by name. Ambiguous or unknown: None (never guesses)."""

    @abstractmethod
    def create_relationship(
        self, source_entity_id: str, relationship_type: RelationshipType, target_entity_id: str,
        provenance: Provenance,
    ) -> Relationship:
        """Validate and add a fact; an existing relationship gains provenance instead of a duplicate."""

    @abstractmethod
    def get_relationship(self, relationship_id: str) -> Relationship | None: ...

    @abstractmethod
    def find_related_entities(
        self, entity_id: str, relationship_type: RelationshipType | None = None,
        entity_type: EntityType | None = None, direction: str = "both", limit: int | None = None,
    ) -> list[RelatedEntity]: ...

    @abstractmethod
    def find_paths(self, source_id: str, target_id: str, max_depth: int | None = None) -> list[GraphPath]: ...

    @abstractmethod
    def search_entities(
        self, text: str | None = None, entity_types: Sequence[EntityType] | None = None, limit: int = 20,
    ) -> list[Entity]: ...

    @abstractmethod
    def deactivate_entity(self, entity_id: str) -> bool: ...

    @abstractmethod
    def deactivate_relationship(self, relationship_id: str) -> bool: ...
