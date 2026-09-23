"""Deterministic fakes for RAG tests: bag-of-words embeddings, a PDF writer, a scripted LLM."""

import hashlib
import re
from collections.abc import Sequence

import numpy as np

from agent.rag.embeddings import EmbeddingProvider
from agent.rag.models import EmbeddingError
from backend.core.llm.base import LLMProvider

_STOP = {"the", "a", "an", "is", "are", "of", "for", "what", "does", "do", "did", "i", "my", "in", "on", "to", "and", "it", "use", "uses", "used"}
DIM = 128


class BagOfWordsEmbedder(EmbeddingProvider):
    """Words hash into a 128-d count vector: shared words mean similarity, no shared words means ~0."""

    def __init__(self, model_name="fake-bow", fail=False):
        self._model_name = model_name
        self.fail = fail
        self.calls = 0

    @property
    def model_name(self) -> str:
        return self._model_name

    def embed_documents(self, texts: Sequence[str]) -> np.ndarray:
        self.calls += 1
        if self.fail:
            raise EmbeddingError("fake embedding failure")
        out = np.zeros((len(texts), DIM), dtype=np.float32)
        for row, text in enumerate(texts):
            for word in re.findall(r"[a-z0-9]+", text.lower()):
                word = word.rstrip("s") if len(word) > 3 else word
                if word in _STOP:
                    continue
                out[row, int(hashlib.md5(word.encode()).hexdigest(), 16) % DIM] += 1.0
        norms = np.linalg.norm(out, axis=1, keepdims=True)
        return out / np.where(norms == 0, 1, norms)


class ScriptedLLM(LLMProvider):
    """Returns scripted replies (or raises) and records every request."""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.requests = []

    def chat(self, messages, json_mode=False):
        self.requests.append(list(messages))
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


def make_pdf(pages: Sequence[str]) -> bytes:
    """A minimal valid text PDF, one string per page."""
    def esc(s):
        return s.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")

    count = len(pages)
    kids = " ".join(f"{4 + 2 * i} 0 R" for i in range(count))
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        f"<< /Type /Pages /Kids [{kids}] /Count {count} >>".encode(),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    for i, text in enumerate(pages):
        stream = f"BT /F1 12 Tf 72 720 Td ({esc(text)}) Tj ET".encode()
        objs.append(
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 3 0 R >> >> "
            f"/Contents {5 + 2 * i} 0 R >>".encode()
        )
        objs.append(f"<< /Length {len(stream)} >>\nstream\n".encode() + stream + b"\nendstream")
    out = b"%PDF-1.4\n"
    offsets = []
    for number, body in enumerate(objs, start=1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n".encode() + b"0000000000 65535 f \n"
    out += b"".join(f"{offset:010d} 00000 n \n".encode() for offset in offsets)
    out += f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF".encode()
    return out
