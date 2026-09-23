"""Deterministic character-based chunking.

Each page is chunked on its own, so a chunk never spans pages and always keeps
its page number. Windows of at most `chunk_size` characters overlap by
`overlap`; a window ends at a paragraph/sentence/word boundary when one is
available near its end. Same input and settings always give the same chunks.
"""

import re
from collections.abc import Sequence
from dataclasses import dataclass

from agent.rag.loaders import Page

_SEPARATORS = ("\n\n", "\n", ". ", "? ", "! ", " ")
_MIN_FILL = 0.6  # a boundary must leave at least this fraction of the window filled


@dataclass(frozen=True)
class ChunkSpec:
    index: int  # document-wide order, starting at 0
    text: str
    page: int | None
    char_start: int  # offset within the (normalized) page text


def normalize_text(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\x00", "")
    text = re.sub(r"[ \t\f\v]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


class Chunker:
    def __init__(self, chunk_size: int = 800, overlap: int = 100):
        if chunk_size < 50:
            raise ValueError("chunk_size must be at least 50 characters")
        if not 0 <= overlap < chunk_size:
            raise ValueError("overlap must be between 0 and chunk_size - 1")
        self._size = chunk_size
        self._overlap = overlap

    def chunk_pages(self, pages: Sequence[Page]) -> list[ChunkSpec]:
        chunks: list[ChunkSpec] = []
        for page in pages:
            for start, text in self._windows(normalize_text(page.text)):
                chunks.append(ChunkSpec(len(chunks), text, page.number, start))
        return chunks

    def _windows(self, text: str) -> list[tuple[int, str]]:
        windows: list[tuple[int, str]] = []
        start = 0
        while start < len(text):
            end = min(start + self._size, len(text))
            if end < len(text):
                end = self._boundary(text, start, end)
            piece = text[start:end].strip()
            if piece:
                windows.append((start, piece))
            if end >= len(text):
                break
            next_start = end - self._overlap
            start = next_start if next_start > start else end
        return windows

    def _boundary(self, text: str, start: int, end: int) -> int:
        floor = start + int(self._size * _MIN_FILL)
        for separator in _SEPARATORS:
            cut = text.rfind(separator, floor, end)
            if cut != -1:
                return cut + len(separator)
        return end
