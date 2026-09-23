"""Personal RAG domain models. Storage- and library-agnostic.

Documents and chunks live in their own tables and are unrelated to Phase 6
structured memory. Raw embeddings never appear in any model returned to
callers or to the LLM.
"""

from datetime import datetime, timezone
from enum import StrEnum
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, Field


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def new_id() -> str:
    return uuid4().hex


class DocumentStatus(StrEnum):
    PENDING = "pending"
    PROCESSING = "processing"
    INDEXED = "indexed"
    FAILED = "failed"
    DELETED = "deleted"


class SourceType(StrEnum):
    TXT = "txt"
    MARKDOWN = "markdown"
    PDF = "pdf"


class Document(BaseModel):
    document_id: str = Field(default_factory=new_id)
    filename: str
    title: str | None = None
    source_type: SourceType
    source_location: str  # absolute path of the original file (never copied or modified)
    content_hash: str  # SHA-256 of the file bytes
    status: DocumentStatus = DocumentStatus.PENDING
    last_error: str | None = None  # short reason, never document content
    page_count: int | None = None
    chunk_count: int = 0
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)
    indexed_at: datetime | None = None


class DocumentChunk(BaseModel):
    chunk_id: str = Field(default_factory=new_id)
    document_id: str
    chunk_index: int = Field(ge=0)
    text: str = Field(min_length=1)
    page: int | None = None  # 1-based; None when the format has no pages
    metadata: dict[str, Any] = Field(default_factory=dict)
    embedding_model: str = ""  # which model produced this chunk's vector
    created_at: datetime = Field(default_factory=utcnow)


class SourceRef(BaseModel):
    document_id: str
    filename: str
    chunk_id: str
    page: int | None = None

    @property
    def citation(self) -> str:
        page = f", page {self.page}" if self.page is not None else ""
        return f"[Source: {self.filename}{page}]"


class RetrievalResult(BaseModel):
    """A retrieved chunk with its document metadata and relevance score."""

    chunk_id: str
    document_id: str
    chunk_index: int
    text: str
    score: float  # cosine similarity, higher is more relevant
    filename: str
    title: str | None = None
    source_type: SourceType
    page: int | None = None

    @property
    def source(self) -> SourceRef:
        return SourceRef(document_id=self.document_id, filename=self.filename, chunk_id=self.chunk_id, page=self.page)


class GroundedContext(BaseModel):
    query: str
    results: list[RetrievalResult]
    sources: list[SourceRef]


class AnswerStatus(StrEnum):
    GROUNDED = "grounded"
    INSUFFICIENT_CONTEXT = "insufficient_context"
    ERROR = "error"  # the RAG subsystem could not run; nothing was retrieved


class RAGAnswer(BaseModel):
    answer: str
    status: AnswerStatus
    sources: list[SourceRef] = Field(default_factory=list)
    top_score: float | None = None
    retrieved_count: int = 0
    min_score: float | None = None

    @property
    def grounded(self) -> bool:
        return self.status is AnswerStatus.GROUNDED

    @property
    def citations(self) -> list[str]:
        return list(dict.fromkeys(s.citation for s in self.sources))


class IngestOutcome(StrEnum):
    INDEXED = "indexed"
    REINDEXED = "reindexed"
    UNCHANGED = "unchanged"  # same file, same content: nothing done
    DUPLICATE = "duplicate"  # identical content is already indexed under another file
    REJECTED = "rejected"  # refused before processing (missing, unsupported, too large)
    FAILED = "failed"


class IngestResult(BaseModel):
    outcome: IngestOutcome
    document: Document | None = None
    error: str | None = None
    duplicate_of: str | None = None
    kept_previous_index: bool = False  # a failed re-index left the old, still-valid index in place


# ---- errors -------------------------------------------------------------

class RAGError(Exception):
    """Base for RAG-subsystem errors. Messages never contain document content."""


class UnsupportedDocument(RAGError):
    pass


class CorruptDocument(RAGError):
    pass


class EmbeddingError(RAGError):
    pass


class RAGStorageError(RAGError):
    """The document/vector database is unavailable or failed."""
