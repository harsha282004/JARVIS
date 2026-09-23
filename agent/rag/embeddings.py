"""Embedding providers. RAG depends only on `EmbeddingProvider`.

The default implementation is local (SentenceTransformers): documents and
queries are embedded on this machine and nothing is sent to a cloud API. The
model files are downloaded once from Hugging Face on first use (the only
network access RAG needs) and cached locally.
"""

from abc import ABC, abstractmethod
from collections.abc import Sequence

import numpy as np

from agent.rag.models import EmbeddingError

DEFAULT_MODEL = "sentence-transformers/all-MiniLM-L6-v2"


class EmbeddingProvider(ABC):
    @property
    @abstractmethod
    def model_name(self) -> str:
        """Identifier stored with each vector; vectors from different models are never compared."""
        raise NotImplementedError

    @abstractmethod
    def embed_documents(self, texts: Sequence[str]) -> np.ndarray:
        """Return an (n, dim) float32 array of L2-normalized vectors. Raises EmbeddingError."""
        raise NotImplementedError

    def embed_text(self, text: str) -> np.ndarray:
        """Return one L2-normalized float32 vector of shape (dim,)."""
        return self.embed_documents([text])[0]


class SentenceTransformerProvider(EmbeddingProvider):
    def __init__(self, model: str = DEFAULT_MODEL, device: str = "cpu", batch_size: int = 32):
        self._model_id = model
        self._device = device
        self._batch_size = batch_size
        self._model = None

    @property
    def model_name(self) -> str:
        return self._model_id

    def _load(self):
        if self._model is None:
            try:
                from sentence_transformers import SentenceTransformer
            except ImportError as exc:  # pragma: no cover - dependency is in requirements.txt
                raise EmbeddingError("sentence-transformers is not installed. Run: pip install -r requirements.txt") from exc
            try:
                self._model = SentenceTransformer(self._model_id, device=self._device)
            except Exception as exc:  # noqa: BLE001 - covers missing/corrupt model download, offline first run
                raise EmbeddingError(
                    f"Could not load embedding model '{self._model_id}' ({type(exc).__name__}). "
                    "The first use downloads it and needs network access."
                ) from None
        return self._model

    def embed_documents(self, texts: Sequence[str]) -> np.ndarray:
        if not texts:
            return np.zeros((0, 0), dtype=np.float32)
        model = self._load()
        try:
            vectors = model.encode(
                list(texts), batch_size=self._batch_size, normalize_embeddings=True,
                convert_to_numpy=True, show_progress_bar=False,
            )
        except Exception as exc:  # noqa: BLE001 - backend boundary
            raise EmbeddingError(f"Embedding failed ({type(exc).__name__})") from None
        return np.asarray(vectors, dtype=np.float32)
