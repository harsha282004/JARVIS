"""RagService: ingestion, document management and grounded answering.

Ingestion:  validate -> hash -> (skip if unchanged / duplicate) -> extract -> chunk -> embed -> store
Answering:  retrieve -> (nothing relevant? controlled "insufficient" path, no LLM call)
                     -> grounded prompt -> LLM -> answer + sources

All document changes go through this class from code (CLI, future UI); the
LLM has no path to ingest, re-index or delete documents. Original files are
only read, never modified, moved or deleted. Logs carry ids, filenames and
counts, never document text.
"""

import hashlib
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from agent.rag.chunker import Chunker
from agent.rag.documents import DocumentRepository
from agent.rag.embeddings import EmbeddingProvider
from agent.rag.grounding import (
    INSUFFICIENT_MARKER,
    INSUFFICIENT_RESPONSE,
    RAG_UNAVAILABLE_RESPONSE,
    build_grounded_context,
    build_grounded_system_prompt,
)
from agent.rag.loaders import detect_source_type, load_document
from agent.rag.models import (
    AnswerStatus,
    CorruptDocument,
    Document,
    DocumentChunk,
    DocumentEvent,
    DocumentEventKind,
    DocumentStatus,
    EmbeddingError,
    IngestOutcome,
    IngestResult,
    RAGAnswer,
    RAGError,
    RAGStorageError,
    UnsupportedDocument,
    utcnow,
)
from agent.rag.retriever import Retriever
from agent.rag.store import VectorItem, VectorStore
from backend.core.llm.base import LLMProvider
from backend.core.llm.messages import Message, Role
from backend.core.logging import get_logger

logger = get_logger(__name__)

_HASH_BLOCK = 1024 * 1024
_MAX_ERROR_CHARS = 300


@dataclass(frozen=True)
class RagLimits:
    max_document_bytes: int = 25 * 1024 * 1024
    max_chunks_per_document: int = 2000


class RagService:
    def __init__(
        self,
        documents: DocumentRepository,
        store: VectorStore,
        embedder: EmbeddingProvider,
        retriever: Retriever,
        llm: LLMProvider,
        chunker: Chunker | None = None,
        limits: RagLimits | None = None,
        clock: Callable[[], datetime] = utcnow,
    ):
        self._docs = documents
        self._store = store
        self._embedder = embedder
        self._retriever = retriever
        self._llm = llm
        self._chunker = chunker or Chunker()
        self._limits = limits or RagLimits()
        self._clock = clock
        self._listeners: list[Callable[[DocumentEvent], None]] = []

    def add_listener(self, listener: Callable[[DocumentEvent], None]) -> None:
        """Be told when a document is indexed, re-indexed or deleted (keeps derived data consistent)."""
        self._listeners.append(listener)

    def _notify(self, kind: DocumentEventKind, document: Document) -> None:
        for listener in self._listeners:
            try:
                listener(DocumentEvent(kind=kind, document=document))
            except Exception as exc:  # noqa: BLE001 - a derived system must never break the RAG index
                logger.warning("Document change listener failed (%s)", type(exc).__name__)

    # ---- ingestion -------------------------------------------------------

    def ingest_file(self, path: str | Path, force: bool = False) -> IngestResult:
        """Index one file. Returns an IngestResult; only storage outages raise
        (RAGStorageError). `force` re-indexes even if the content is unchanged."""
        try:
            resolved = Path(path).expanduser().resolve(strict=True)
        except OSError:
            return self._rejected("File not found")
        if not resolved.is_file():
            return self._rejected("Not a regular file")
        try:
            source_type = detect_source_type(resolved)
        except UnsupportedDocument as exc:
            return self._rejected(str(exc))
        size = resolved.stat().st_size
        if size == 0:
            return self._rejected("File is empty")
        if size > self._limits.max_document_bytes:
            return self._rejected(f"File is larger than the {self._limits.max_document_bytes // (1024 * 1024)} MB limit")
        try:
            content_hash = self._hash(resolved)
        except OSError as exc:
            return self._rejected(f"Could not read the file ({type(exc).__name__})")

        location = str(resolved)
        logger.info("Document ingestion started (file=%s)", resolved.name)
        existing = self._docs.get_by_location(location)
        if existing and existing.status is DocumentStatus.INDEXED and existing.content_hash == content_hash and not force:
            logger.info("Document unchanged; not re-indexed (id=%s)", existing.document_id)
            return IngestResult(outcome=IngestOutcome.UNCHANGED, document=existing)
        if existing is None or existing.status is not DocumentStatus.INDEXED:
            duplicate = self._docs.find_indexed_by_hash(content_hash)
            if duplicate is not None:
                logger.info("Identical content already indexed (id=%s, file=%s)", duplicate.document_id, resolved.name)
                return IngestResult(outcome=IngestOutcome.DUPLICATE, document=duplicate, duplicate_of=duplicate.document_id)

        # A still-valid index is only replaced once the new one is fully built.
        is_reindex = existing is not None and existing.status is DocumentStatus.INDEXED
        now = self._clock()
        doc = existing or Document(
            filename=resolved.name, source_type=source_type, source_location=location,
            content_hash=content_hash, created_at=now,
        )
        if not is_reindex:
            doc = doc.model_copy(update={
                "filename": resolved.name, "source_type": source_type, "content_hash": content_hash,
                "status": DocumentStatus.PROCESSING, "last_error": None, "updated_at": now,
            })
            if existing is None:
                self._docs.add(doc)
            else:
                self._docs.save(doc)

        try:
            extracted = load_document(resolved)
            specs = self._chunker.chunk_pages(extracted.pages)
            if not specs:
                raise CorruptDocument("No extractable text (empty or scanned document; OCR is not supported)")
            if len(specs) > self._limits.max_chunks_per_document:
                raise CorruptDocument(
                    f"Document would need {len(specs)} chunks; the limit is {self._limits.max_chunks_per_document}"
                )
            vectors = self._embedder.embed_documents([s.text for s in specs])
            if len(vectors) != len(specs):
                raise EmbeddingError("Embedding provider returned the wrong number of vectors")
            model = self._embedder.model_name
            items = [
                VectorItem(
                    DocumentChunk(
                        document_id=doc.document_id, chunk_index=s.index, text=s.text, page=s.page,
                        metadata={"char_start": s.char_start}, embedding_model=model, created_at=now,
                    ),
                    vectors[i],
                )
                for i, s in enumerate(specs)
            ]
            self._store.replace_document(doc.document_id, items)
        except RAGStorageError:
            raise
        except RAGError as exc:
            return self._failed(doc, is_reindex, str(exc))

        indexed = doc.model_copy(update={
            "title": extracted.title, "page_count": extracted.page_count, "content_hash": content_hash,
            "status": DocumentStatus.INDEXED, "last_error": None, "chunk_count": len(items),
            "metadata": {"size_bytes": size}, "updated_at": now, "indexed_at": now,
        })
        self._docs.save(indexed)
        outcome = IngestOutcome.REINDEXED if is_reindex else IngestOutcome.INDEXED
        logger.info("Document ingestion completed (id=%s, file=%s, chunks=%d, outcome=%s)",
                    indexed.document_id, resolved.name, len(items), outcome.value)
        self._notify(DocumentEventKind.REINDEXED if is_reindex else DocumentEventKind.INDEXED, indexed)
        return IngestResult(outcome=outcome, document=indexed)

    def reindex_document(self, document_id: str) -> IngestResult:
        doc = self._docs.get(document_id)
        if doc is None or doc.status is DocumentStatus.DELETED:
            return self._rejected("Unknown document")
        return self.ingest_file(doc.source_location, force=True)

    def delete_document(self, document_id: str) -> bool:
        """Remove a document's chunks and vectors and mark it DELETED. The original file is untouched."""
        doc = self._docs.get(document_id)
        if doc is None or doc.status is DocumentStatus.DELETED:
            return False
        self._store.delete_document(document_id)
        self._docs.save(doc.model_copy(update={
            "status": DocumentStatus.DELETED, "chunk_count": 0, "updated_at": self._clock(),
        }))
        logger.info("Document deleted from the index (id=%s, file=%s)", document_id, doc.filename)
        self._notify(DocumentEventKind.DELETED, doc)
        return True

    def get_chunks(self, document_id: str) -> list[DocumentChunk]:
        return self._store.get_chunks(document_id)

    def search(self, query: str):
        """Relevant chunks (with scores and sources) without generating an answer."""
        return self._retriever.retrieve(query)

    def list_documents(self, statuses: Sequence[DocumentStatus] | None = None) -> list[Document]:
        return self._docs.list(statuses)

    def get_document(self, document_id: str) -> Document | None:
        return self._docs.get(document_id)

    # ---- answering ---------------------------------------------------------

    def answer(
        self,
        query: str,
        history: Sequence[Message] = (),
        user_text: str | None = None,
        memory_context: str = "",
    ) -> RAGAnswer:
        """Answer from the user's documents.

        `query` is the standalone search query; `history` is the conversation so
        far (owned by ConversationEngine); `user_text` is what the user actually said.
        Raises LLMProviderError if the LLM fails: no grounded answer is claimed.
        """
        min_score = self._retriever.min_score
        try:
            results = self._retriever.retrieve(query)
        except RAGError as exc:
            logger.warning("RAG retrieval failed (%s)", type(exc).__name__)
            return RAGAnswer(answer=RAG_UNAVAILABLE_RESPONSE, status=AnswerStatus.ERROR, min_score=min_score)
        if not results:
            return RAGAnswer(answer=INSUFFICIENT_RESPONSE, status=AnswerStatus.INSUFFICIENT_CONTEXT, min_score=min_score)

        context = build_grounded_context(query, results)
        messages = [
            Message(Role.SYSTEM, build_grounded_system_prompt(context, memory_context)),
            *history,
            Message(Role.USER, user_text or query),
        ]
        reply = self._llm.chat(messages).strip()
        top = max(r.score for r in context.results)
        if reply.upper().startswith(INSUFFICIENT_MARKER):
            return RAGAnswer(
                answer=INSUFFICIENT_RESPONSE, status=AnswerStatus.INSUFFICIENT_CONTEXT,
                top_score=top, retrieved_count=len(context.results), min_score=min_score,
            )
        return RAGAnswer(
            answer=reply, status=AnswerStatus.GROUNDED, sources=context.sources,
            top_score=top, retrieved_count=len(context.results), min_score=min_score,
        )

    # ---- internals ---------------------------------------------------------

    @staticmethod
    def _hash(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(_HASH_BLOCK), b""):
                digest.update(block)
        return digest.hexdigest()

    @staticmethod
    def _rejected(reason: str) -> IngestResult:
        logger.warning("Document ingestion rejected: %s", reason)
        return IngestResult(outcome=IngestOutcome.REJECTED, error=reason)

    def _failed(self, doc: Document, kept_previous: bool, reason: str) -> IngestResult:
        reason = reason[:_MAX_ERROR_CHARS]
        logger.warning("Document ingestion failed (id=%s, file=%s): %s", doc.document_id, doc.filename, reason)
        if kept_previous:
            # The previous index is still valid: keep it searchable, but record the failure.
            failed = doc.model_copy(update={"last_error": reason, "updated_at": self._clock()})
        else:
            failed = doc.model_copy(update={"status": DocumentStatus.FAILED, "last_error": reason, "updated_at": self._clock()})
        self._docs.save(failed)
        return IngestResult(outcome=IngestOutcome.FAILED, document=failed, error=reason, kept_previous_index=kept_previous)
