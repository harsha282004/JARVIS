"""Personal knowledge graph domain models (Pydantic). Storage-agnostic.

Entities and relationships are typed rows, never free-form JSON blobs. Every
relationship's evidence lives in `Provenance` records (memory id, document id
/ page / chunk, ...); the relationship's confidence and trust are derived from
its *active* provenance, and it stays active only while at least one remains.
"""

from datetime import datetime, timezone
from enum import IntEnum, StrEnum
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, Field, field_validator, model_validator

from agent.memory.models import Confidence  # LOW / MEDIUM / HIGH, shared with Phase 6

MAX_NAME_CHARS = 120
MAX_DESCRIPTION_CHARS = 500


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def new_id() -> str:
    return uuid4().hex


class EntityType(StrEnum):
    PERSON = "person"
    PROJECT = "project"
    TECHNOLOGY = "technology"
    ORGANIZATION = "organization"
    DOCUMENT = "document"
    SKILL = "skill"
    GOAL = "goal"
    LOCATION = "location"
    TOPIC = "topic"


class RelationshipType(StrEnum):
    WORKS_ON = "works_on"
    USES = "uses"
    KNOWS = "knows"
    PREFERS = "prefers"
    STUDIES = "studies"
    WORKS_AT = "works_at"
    PART_OF = "part_of"
    RELATED_TO = "related_to"
    MENTIONS = "mentions"
    DOCUMENTED_IN = "documented_in"
    HAS_SKILL = "has_skill"
    HAS_GOAL = "has_goal"
    LOCATED_IN = "located_in"
    DEPENDS_ON = "depends_on"


class TrustLevel(IntEnum):
    """Where a fact stands. Higher outranks lower; an inferred fact never overrides a higher one."""

    INFERRED = 1  # derived by a model, not stated by anyone
    VERIFIED_SOURCE = 2  # stated in one of the user's own documents (evidence quoted from it)
    EXPLICIT_USER = 3  # the user said it (directly, or as an explicit memory)


class SourceKind(StrEnum):
    EXPLICIT_USER_STATEMENT = "explicit_user_statement"
    PERSONAL_MEMORY = "personal_memory"
    PERSONAL_DOCUMENT = "personal_document"
    CONVERSATION = "conversation"
    IMPORTED_SOURCE = "imported_source"


class GraphStatus(StrEnum):
    ACTIVE = "active"
    INACTIVE = "inactive"


class Provenance(BaseModel):
    """Evidence for one relationship. Never fabricated: required fields depend on the kind."""

    provenance_id: str = Field(default_factory=new_id)
    source_kind: SourceKind
    source_id: str | None = Field(default=None, max_length=64)  # memory id or document id
    source_name: str | None = Field(default=None, max_length=260)  # e.g. the filename
    page: int | None = Field(default=None, ge=1)
    chunk_id: str | None = Field(default=None, max_length=64)
    confidence: Confidence = Confidence.HIGH
    trust: TrustLevel = TrustLevel.EXPLICIT_USER
    active: bool = True
    created_at: datetime = Field(default_factory=utcnow)

    @model_validator(mode="after")
    def _check(self) -> "Provenance":
        kind = self.source_kind
        if kind is SourceKind.PERSONAL_MEMORY and not self.source_id:
            raise ValueError("personal_memory provenance needs the memory id")
        if kind is SourceKind.PERSONAL_DOCUMENT and not (self.source_id and self.source_name):
            raise ValueError("personal_document provenance needs the document id and name")
        if kind is SourceKind.IMPORTED_SOURCE and not self.source_name:
            raise ValueError("imported_source provenance needs a source name")
        if self.trust is TrustLevel.EXPLICIT_USER and kind not in (
            SourceKind.EXPLICIT_USER_STATEMENT, SourceKind.PERSONAL_MEMORY
        ):
            raise ValueError("only a user statement or an explicit memory can carry explicit-user trust")
        if self.trust is TrustLevel.INFERRED and self.confidence is not Confidence.LOW:
            raise ValueError("inferred facts can only have LOW confidence")
        return self


class Entity(BaseModel):
    entity_id: str = Field(default_factory=new_id)
    entity_type: EntityType
    canonical_name: str
    name_key: str  # deterministic normalization used for identity (see normalize.py)
    description: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    confidence: Confidence = Confidence.HIGH
    trust: TrustLevel = TrustLevel.EXPLICIT_USER
    status: GraphStatus = GraphStatus.ACTIVE
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)

    @field_validator("canonical_name")
    @classmethod
    def _clean_name(cls, value: str) -> str:
        value = " ".join(value.split())
        if not value or len(value) > MAX_NAME_CHARS:
            raise ValueError(f"name must be 1-{MAX_NAME_CHARS} characters")
        return value

    @field_validator("description")
    @classmethod
    def _clean_description(cls, value: str | None) -> str | None:
        return " ".join(value.split())[:MAX_DESCRIPTION_CHARS] or None if value else None


class Relationship(BaseModel):
    relationship_id: str = Field(default_factory=new_id)
    source_entity_id: str
    relationship_type: RelationshipType
    target_entity_id: str
    confidence: Confidence = Confidence.HIGH  # best of its active provenance
    trust: TrustLevel = TrustLevel.EXPLICIT_USER  # highest of its active provenance
    status: GraphStatus = GraphStatus.ACTIVE
    valid_from: datetime = Field(default_factory=utcnow)
    valid_until: datetime | None = None  # set when the relationship stops being current
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


class RelatedEntity(BaseModel):
    """A neighbour found through a relationship. direction is relative to the queried entity."""

    entity: Entity
    relationship: Relationship
    direction: str  # "out": queried --rel--> entity, "in": entity --rel--> queried


class PathStep(BaseModel):
    relationship: Relationship
    from_entity_id: str
    to_entity_id: str


class GraphPath(BaseModel):
    entity_ids: list[str]
    steps: list[PathStep]

    @property
    def length(self) -> int:
        return len(self.steps)


class GraphFact(BaseModel):
    """One relationship rendered for the LLM: names and types, no ids or internals."""

    source_name: str
    source_type: EntityType
    relationship_type: RelationshipType
    target_name: str
    target_type: EntityType
    trust: TrustLevel
    confidence: Confidence
    sources: list[str] = Field(default_factory=list)  # e.g. "personal_memory", "resume.pdf"


# ---- validated facts (the only thing that may be written) ---------------------

class EntityRef(BaseModel):
    name: str
    entity_type: EntityType
    description: str | None = None


class RelationshipFact(BaseModel):
    source: EntityRef
    relationship_type: RelationshipType
    target: EntityRef
    provenance: Provenance


class ApplyResult(BaseModel):
    entities_created: int = 0
    relationships_created: int = 0
    relationships_updated: int = 0
    relationships_conflicted: int = 0
    skipped: int = 0


# ---- errors ---------------------------------------------------------------------

class GraphError(Exception):
    """Base for graph errors. Messages never contain document or conversation text."""


class GraphValidationError(GraphError):
    pass


class GraphStorageError(GraphError):
    """The graph database is unavailable or failed."""


class GraphExtractionError(GraphError):
    pass
