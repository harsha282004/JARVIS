"""Retriever: query -> ranked, sufficiently relevant chunks (semantic, exact cosine).

Plain vector retrieval. Hybrid (lexical + vector) retrieval and a reranker are
deliberately not part of Phase 7 (see docs/personal-rag.md).
"""

from agent.rag.embeddings import EmbeddingProvider
from agent.rag.models import RetrievalResult
from agent.rag.store import VectorStore
from backend.core.logging import get_logger

logger = get_logger(__name__)


class Retriever:
    def __init__(self, embedder: EmbeddingProvider, store: VectorStore, top_k: int, min_score: float):
        if top_k < 1:
            raise ValueError("top_k must be at least 1")
        self._embedder = embedder
        self._store = store
        self._top_k = top_k
        self._min_score = min_score

    @property
    def min_score(self) -> float:
        return self._min_score

    def retrieve(self, query: str) -> list[RetrievalResult]:
        """Results at or above the relevance threshold, best first, at most top_k.
        Raises EmbeddingError / RAGStorageError; never fabricates results."""
        query = query.strip()
        if not query:
            return []
        vector = self._embedder.embed_text(query)
        results = self._store.search(vector, self._embedder.model_name, self._top_k, self._min_score)
        logger.info("RAG retrieval (results=%d, top_k=%d)", len(results), self._top_k)
        if not results and self._store.count() > self._store.count(self._embedder.model_name):
            logger.warning(
                "Some indexed chunks use a different embedding model than '%s' and are ignored; re-index those documents",
                self._embedder.model_name,
            )
        return results
