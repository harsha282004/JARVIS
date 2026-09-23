"""Vector store abstraction and its PostgreSQL/SQLAlchemy implementation.

`SqlVectorStore` keeps chunk text, metadata and the embedding (float32 bytes)
in the `rag_chunks` table and does an *exact* cosine search with NumPy in
batches. That needs no PostgreSQL extension, runs identically on the
SQLite used by unit tests, and is fast enough for a personal collection
(tens of thousands of chunks). pgvector could replace it behind the same
`VectorStore` interface if a collection outgrows this; see docs/personal-rag.md.

Only chunks of INDEXED documents are searchable, and vectors from different
embedding models are never compared.
"""

import heapq
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass

import numpy as np
from sqlalchemy import delete, func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from agent.rag.models import DocumentChunk, DocumentStatus, RAGStorageError, RetrievalResult, SourceType
from backend.models.rag import RagChunk, RagDocument

SessionFactory = Callable[[], Session]
_SCAN_BATCH = 1000


@dataclass(frozen=True)
class VectorItem:
    chunk: DocumentChunk
    vector: np.ndarray  # float32, L2-normalized


class VectorStore(ABC):
    @abstractmethod
    def add(self, items: Sequence[VectorItem]) -> None:
        raise NotImplementedError

    @abstractmethod
    def replace_document(self, document_id: str, items: Sequence[VectorItem]) -> None:
        """Atomically swap a document's chunks: either all new chunks replace all
        old ones, or nothing changes."""
        raise NotImplementedError

    @abstractmethod
    def delete_document(self, document_id: str) -> int:
        """Remove all of a document's chunks and vectors. Returns how many."""
        raise NotImplementedError

    @abstractmethod
    def search(self, query_vector: np.ndarray, model_name: str, top_k: int, min_score: float) -> list[RetrievalResult]:
        raise NotImplementedError

    @abstractmethod
    def count(self, model_name: str | None = None) -> int:
        raise NotImplementedError

    @abstractmethod
    def clear(self) -> None:
        raise NotImplementedError


def _row(item: VectorItem) -> RagChunk:
    vector = np.ascontiguousarray(item.vector, dtype=np.float32)
    chunk = item.chunk
    return RagChunk(
        id=chunk.chunk_id, document_id=chunk.document_id, chunk_index=chunk.chunk_index, text=chunk.text,
        page=chunk.page, extra=dict(chunk.metadata), embedding=vector.tobytes(),
        embedding_model=chunk.embedding_model, embedding_dim=int(vector.shape[0]), created_at=chunk.created_at,
    )


class SqlVectorStore(VectorStore):
    def __init__(self, session_factory: SessionFactory):
        self._session_factory = session_factory

    def _run(self, work: Callable[[Session], object]):
        try:
            with self._session_factory() as session:
                result = work(session)
                session.commit()
                return result
        except SQLAlchemyError as exc:
            # Type only: driver messages can echo SQL parameters (document text).
            raise RAGStorageError(f"Vector store database error ({type(exc).__name__})") from None

    def add(self, items: Sequence[VectorItem]) -> None:
        self._run(lambda s: s.add_all([_row(i) for i in items]))

    def replace_document(self, document_id: str, items: Sequence[VectorItem]) -> None:
        def work(session: Session) -> None:
            session.execute(delete(RagChunk).where(RagChunk.document_id == document_id))
            session.add_all([_row(i) for i in items])

        self._run(work)

    def delete_document(self, document_id: str) -> int:
        return int(self._run(lambda s: s.execute(delete(RagChunk).where(RagChunk.document_id == document_id)).rowcount or 0))

    def count(self, model_name: str | None = None) -> int:
        stmt = select(func.count()).select_from(RagChunk)
        if model_name is not None:
            stmt = stmt.where(RagChunk.embedding_model == model_name)
        return int(self._run(lambda s: s.scalar(stmt) or 0))

    def clear(self) -> None:
        self._run(lambda s: s.execute(delete(RagChunk)))

    def search(self, query_vector: np.ndarray, model_name: str, top_k: int, min_score: float) -> list[RetrievalResult]:
        query = np.ascontiguousarray(query_vector, dtype=np.float32)

        def work(session: Session) -> list[RetrievalResult]:
            scan = (
                select(RagChunk.id, RagChunk.embedding)
                .join(RagDocument, RagDocument.id == RagChunk.document_id)
                .where(
                    RagDocument.status == DocumentStatus.INDEXED.value,
                    RagChunk.embedding_model == model_name,
                    RagChunk.embedding_dim == int(query.shape[0]),
                )
                .execution_options(yield_per=_SCAN_BATCH)
            )
            best: list[tuple[float, str]] = []  # min-heap of (score, chunk_id)
            for batch in _batched(session.execute(scan), _SCAN_BATCH):
                ids = [row[0] for row in batch]
                matrix = np.vstack([np.frombuffer(row[1], dtype=np.float32) for row in batch])
                for chunk_id, score in zip(ids, matrix @ query):
                    if score >= min_score:
                        heapq.heappush(best, (float(score), chunk_id))
                        if len(best) > top_k:
                            heapq.heappop(best)
            if not best:
                return []
            scores = {chunk_id: score for score, chunk_id in best}
            rows = session.execute(
                select(RagChunk, RagDocument).join(RagDocument, RagDocument.id == RagChunk.document_id)
                .where(RagChunk.id.in_(list(scores)))
            ).all()
            results = [
                RetrievalResult(
                    chunk_id=c.id, document_id=c.document_id, chunk_index=c.chunk_index, text=c.text,
                    score=scores[c.id], filename=d.filename, title=d.title,
                    source_type=SourceType(d.source_type), page=c.page,
                )
                for c, d in rows
            ]
            results.sort(key=lambda r: (-r.score, r.document_id, r.chunk_index))
            return results

        return self._run(work)


def _batched(rows: Iterable, size: int):
    batch: list = []
    for row in rows:
        batch.append(row)
        if len(batch) >= size:
            yield batch
            batch = []
    if batch:
        yield batch
