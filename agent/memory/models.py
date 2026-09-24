"""Personal memory domain models (Pydantic). Storage-agnostic.

Two independent axes describe where a memory came from:
  source: the channel (conversation, explicit statement, correction, import)
  basis:  EXPLICIT (the user said it) or INFERRED (the system guessed it)
Inferred memories can only ever be LOW confidence, and a source that is a
direct user statement or correction must have an EXPLICIT basis, so a guess
can never masquerade as something the user said.
"""

import re
from datetime import datetime, timezone
from enum import IntEnum, StrEnum
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

MAX_CONTENT_CHARS = 300
_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class MemoryType(StrEnum):
    FACT = "fact"  # "User studies Computer Science."
    PREFERENCE = "preference"  # "User prefers Java for backend development."
    GOAL = "goal"  # "User wants to become a software developer."
    PROFILE = "profile"  # "User is a final-year student."
    CONTEXT = "context"  # "User is currently working on the JARVIS project."


class MemorySource(StrEnum):
    CONVERSATION = "conversation"
    EXPLICIT_USER_STATEMENT = "explicit_user_statement"
    USER_CORRECTION = "user_correction"
    IMPORTED_SOURCE = "imported_source"  # reserved: no import feature exists yet


class MemoryBasis(StrEnum):
    EXPLICIT = "explicit"
    INFERRED = "inferred"


class Confidence(IntEnum):
    """LOW: a guess or unverified. MEDIUM: stated but hedged/partial.
    HIGH: stated directly and unambiguously by the user."""

    LOW = 1
    MEDIUM = 2
    HIGH = 3


class MemoryStatus(StrEnum):
    ACTIVE = "active"
    SUPERSEDED = "superseded"  # replaced by a newer/corrected memory (kept for provenance)
    DELETED = "deleted"  # soft-deleted by an explicit request


class MemoryCandidate(BaseModel):
    """A proposed memory, before policy and storage."""

    model_config = ConfigDict(use_enum_values=False)

    type: MemoryType
    content: str
    source: MemorySource
    basis: MemoryBasis
    confidence: Confidence
    slot: str | None = Field(default=None, max_length=120)
    metadata: dict[str, Any] = Field(default_factory=dict)
    # Old value the user retracted ("..., not Java"). Used to supersede; never stored.
    retracts: str | None = Field(default=None, exclude=True, max_length=200)

    @field_validator("content")
    @classmethod
    def _clean_content(cls, value: str) -> str:
        value = " ".join(_CONTROL_CHARS.sub(" ", value).split())
        if not value:
            raise ValueError("content must not be empty")
        if len(value) > MAX_CONTENT_CHARS:
            raise ValueError(f"content must be at most {MAX_CONTENT_CHARS} characters")
        return value

    @model_validator(mode="after")
    def _check_provenance(self) -> "MemoryCandidate":
        direct = {MemorySource.EXPLICIT_USER_STATEMENT, MemorySource.USER_CORRECTION}
        if self.source in direct and self.basis is not MemoryBasis.EXPLICIT:
            raise ValueError("a direct user statement or correction must have an explicit basis")
        if self.basis is MemoryBasis.INFERRED and self.confidence is not Confidence.LOW:
            raise ValueError("inferred memories can only have LOW confidence")
        return self


class Memory(MemoryCandidate):
    """A stored memory."""

    memory_id: str = Field(default_factory=lambda: uuid4().hex)  # random; no user data
    status: MemoryStatus = MemoryStatus.ACTIVE
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)
    last_accessed_at: datetime | None = None
    superseded_by: str | None = None

    @field_validator("created_at", "updated_at", "last_accessed_at")
    @classmethod
    def _require_aware(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.tzinfo is None:
            raise ValueError("timestamps must be timezone-aware")
        return value


class StoreOutcome(StrEnum):
    STORED = "stored"
    DUPLICATE = "duplicate"  # an identical active memory already exists
    SUPERSEDED_OLD = "superseded_old"  # stored; conflicting older memories were deactivated
    KEPT_EXISTING = "kept_existing"  # an explicit memory outranks this inferred one; nothing stored
    PENDING_CONFIRMATION = "pending_confirmation"
    REJECTED = "rejected"
    FAILED = "failed"


class StoreResult(BaseModel):
    outcome: StoreOutcome
    memory: Memory | None = None
    superseded_ids: list[str] = Field(default_factory=list)
    reason: str = ""


class MemoryEventKind(StrEnum):
    STORED = "stored"
    UPDATED = "updated"
    DELETED = "deleted"
    SUPERSEDED = "superseded"
    PURGED = "purged"


class MemoryEvent(BaseModel):
    """Emitted after a committed change so derived systems (the knowledge graph) can stay consistent."""

    kind: MemoryEventKind
    memory: Memory


class PendingMemory(BaseModel):
    """A candidate waiting for the user's confirmation. Held in memory only."""

    pending_id: str = Field(default_factory=lambda: uuid4().hex)
    candidate: MemoryCandidate
    reason: str
    created_at: datetime = Field(default_factory=utcnow)


class MemoryError_(Exception):
    """Base for memory-subsystem errors (named to avoid the builtin MemoryError)."""


class MemoryStorageError(MemoryError_):
    """The memory database is unavailable or failed. Never carries memory content."""


class MemoryNotFound(MemoryError_):
    pass


class MemoryRejected(MemoryError_):
    """The content was refused by the safety rules (e.g. it looks like a secret)."""
