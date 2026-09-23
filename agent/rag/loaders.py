"""Document loaders: file -> plain text (with page numbers where the format has them).

Text is extracted, never executed: no scripts, macros, embedded actions or
links are run or followed. Unsupported, corrupt or unreadable files raise
typed errors; nothing is ever fabricated.
"""

import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from agent.rag.models import CorruptDocument, SourceType, UnsupportedDocument

_EXTENSIONS = {".txt": SourceType.TXT, ".md": SourceType.MARKDOWN, ".markdown": SourceType.MARKDOWN, ".pdf": SourceType.PDF}
SUPPORTED_EXTENSIONS = frozenset(_EXTENSIONS)
_BINARY_SNIFF_BYTES = 8192


@dataclass(frozen=True)
class Page:
    number: int | None  # 1-based; None for formats without pages
    text: str


@dataclass(frozen=True)
class ExtractedDocument:
    source_type: SourceType
    pages: list[Page]
    title: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def page_count(self) -> int | None:
        return len(self.pages) if self.source_type is SourceType.PDF else None


def detect_source_type(path: Path) -> SourceType:
    try:
        return _EXTENSIONS[path.suffix.lower()]
    except KeyError:
        supported = ", ".join(sorted(_EXTENSIONS))
        raise UnsupportedDocument(f"Unsupported file type '{path.suffix}' (supported: {supported})") from None


class DocumentLoader(ABC):
    @abstractmethod
    def load(self, path: Path) -> ExtractedDocument:
        """Extract text from `path` or raise CorruptDocument."""
        raise NotImplementedError


class TextLoader(DocumentLoader):
    """TXT and Markdown (Markdown syntax is kept as written)."""

    def __init__(self, source_type: SourceType = SourceType.TXT):
        self._source_type = source_type

    def load(self, path: Path) -> ExtractedDocument:
        try:
            data = path.read_bytes()
        except OSError as exc:
            raise CorruptDocument(f"Could not read the file ({type(exc).__name__})") from None
        if b"\x00" in data[:_BINARY_SNIFF_BYTES]:
            raise CorruptDocument("File looks binary, not text")
        try:
            text = data.decode("utf-8-sig")
        except UnicodeDecodeError:
            raise CorruptDocument("File is not valid UTF-8 text") from None
        title = None
        if self._source_type is SourceType.MARKDOWN:
            heading = re.search(r"^\s{0,3}#\s+(.+?)\s*#*\s*$", text, re.MULTILINE)
            title = heading.group(1) if heading else None
        return ExtractedDocument(self._source_type, [Page(None, text)], title=title)


class PdfLoader(DocumentLoader):
    """PDF text via pypdf, one Page per PDF page. Text only: no JavaScript,
    launch actions or links are executed or followed. Encrypted PDFs and
    scanned (image-only) pages are not supported."""

    def load(self, path: Path) -> ExtractedDocument:
        try:
            from pypdf import PdfReader
        except ImportError as exc:  # pragma: no cover - dependency is in requirements.txt
            raise CorruptDocument("pypdf is not installed. Run: pip install -r requirements.txt") from exc
        try:
            reader = PdfReader(str(path))
            if reader.is_encrypted:
                raise CorruptDocument("Encrypted PDFs are not supported")
            pages = [Page(i, (page.extract_text() or "")) for i, page in enumerate(reader.pages, start=1)]
            title = (reader.metadata.title if reader.metadata else None) or None
        except CorruptDocument:
            raise
        except Exception as exc:  # noqa: BLE001 - parser boundary: pypdf raises many types for malformed files
            raise CorruptDocument(f"Could not parse the PDF ({type(exc).__name__})") from None
        return ExtractedDocument(SourceType.PDF, pages, title=title)


def load_document(path: Path) -> ExtractedDocument:
    source_type = detect_source_type(path)
    loader: DocumentLoader = PdfLoader() if source_type is SourceType.PDF else TextLoader(source_type)
    return loader.load(path)
