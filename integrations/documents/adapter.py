"""DocumentsAdapter: watched local folders -> the existing RAG index, incrementally. Wraps RagService (chunking, embeddings, vector store are not re-implemented).

Privacy first: nothing is watched unless you list the folder (`JARVIS_DOCUMENT_DIRS`) and the INDEX_DOCUMENTS permission is granted; hidden files and unsupported types are skipped;
file size is capped by the RAG limits. Incremental: a scan compares (modified time, size) with what it saw last time and only *changed* files are handed to the RAG service,
which itself skips identical content by hash; an unchanged tree costs one `stat` per file and no reading. Deleted files are reported as removed from the hub view;
their index entries are left alone unless you enable removal.
Provenance kept per document: file path, modified time, size, index outcome, chunk/page counts; RAG results carry filename and page.
"""

import hashlib
import json
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

from agent.rag.loaders import SUPPORTED_EXTENSIONS
from agent.rag.models import IngestOutcome
from backend.core.state_store import JsonFile
from integrations.hub.models import ErrorKind, HubError, ItemKind, NormalizedItem, Permission, utcnow
from integrations.hub.registry import IntegrationAdapter, SyncBatch

MAX_FILES = 2000


class DocumentsAdapter(IntegrationAdapter):
    name = "documents"
    display_name = "Documents"
    permissions = frozenset({Permission.READ_DOCUMENTS, Permission.INDEX_DOCUMENTS})
    sync_interval_seconds = 300.0

    def __init__(self, rag, directories: list[Path], state_file: Path | None = None, clock: Callable[[], datetime] = utcnow, remove_deleted: bool = False,
                 on_indexed: Callable[[str, str], None] | None = None):
        self._rag = rag
        self._dirs = [Path(d).expanduser() for d in directories]
        self._state = JsonFile(state_file, {}) if state_file else None
        self._mem: dict[str, list] = {}
        self._clock, self._remove_deleted, self._on_indexed = clock, remove_deleted, on_indexed

    @property
    def directories(self) -> list[Path]:
        return list(self._dirs)

    def is_configured(self) -> bool:
        return self._rag is not None and any(d.is_dir() for d in self._dirs)

    def health_check(self) -> str:
        missing = [str(d) for d in self._dirs if not d.is_dir()]
        if missing:
            raise HubError(ErrorKind.CONFIGURATION_ERROR, f"{len(missing)} watched folder(s) not found.")
        self._rag.list_documents()
        return f"{len(self._dirs)} folder(s) watched"

    # ---- scanning ----------------------------------------------------------------------------------------------------------
    def _scan(self) -> dict[str, list]:
        found: dict[str, list] = {}
        for root in self._dirs:
            if not root.is_dir():
                continue
            for path in root.rglob("*"):
                if len(found) >= MAX_FILES:
                    return found
                try:
                    if path.suffix.lower() not in SUPPORTED_EXTENSIONS or not path.is_file() or any(part.startswith(".") for part in path.relative_to(root).parts):
                        continue
                    st = path.stat()
                except OSError:
                    continue
                found[str(path)] = [st.st_mtime_ns, st.st_size]
        return found

    def _seen(self) -> dict[str, list]:
        return self._mem if self._state is None else self._state.read()

    def _save_seen(self, data: dict[str, list]) -> None:
        if self._state is None:
            self._mem = data
        else:
            self._state.write(data)

    def _item(self, path: str, stat: list, doc, outcome: str) -> NormalizedItem:
        mtime = datetime.fromtimestamp(stat[0] / 1e9, tz=timezone.utc)
        return NormalizedItem(ItemKind.DOCUMENT, "documents", path, mtime, Path(path).name, "", {
            "size": stat[1], "outcome": outcome, "document_id": getattr(doc, "document_id", None), "chunks": getattr(doc, "chunk_count", None),
            "pages": getattr(doc, "page_count", None), "directory": str(Path(path).parent),
        }, "high", getattr(doc, "document_id", None), self._clock())

    def sync(self, cursor: str | None, limit: int) -> SyncBatch:
        seen, current = self._seen(), self._scan()
        items, removed, new_seen = [], [], dict(seen)
        indexed = 0
        for path, stat in current.items():
            if seen.get(path) == stat:
                continue  # unchanged since the last scan: not read, not hashed, not re-indexed
            result = self._rag.ingest_file(path)
            if result.outcome in (IngestOutcome.FAILED, IngestOutcome.REJECTED):
                new_seen[path] = stat  # remember it so a broken file is not retried on every scan; a change to it will retry
                items.append(self._item(path, stat, None, result.outcome.value))
                continue
            new_seen[path] = stat
            items.append(self._item(path, stat, result.document, result.outcome.value))
            if result.outcome in (IngestOutcome.INDEXED, IngestOutcome.REINDEXED):
                indexed += 1
                if self._on_indexed:
                    self._on_indexed(path, getattr(result.document, "document_id", ""))
            if indexed >= max(1, limit):
                break  # bounded work per sync; the rest is picked up next time (their stats still differ)
        for path in list(seen):
            if path not in current:
                removed.append((ItemKind.DOCUMENT, path))
                new_seen.pop(path, None)
                if self._remove_deleted:
                    doc = self._rag.get_document_by_location(path) if hasattr(self._rag, "get_document_by_location") else None
                    if doc is not None:
                        self._rag.delete_document(doc.document_id)
        self._save_seen(new_seen)
        digest = hashlib.sha1(json.dumps(sorted(new_seen)).encode()).hexdigest()[:12]
        return SyncBatch(items, digest, removed, f"{indexed} indexed" if indexed else "")

    # ---- search / fetch (the RAG index; provenance = file + page) ----------------------------------------------------------
    def search(self, query: str, limit: int) -> list[NormalizedItem]:
        now = self._clock()
        out = []
        for r in self._rag.search(query)[:limit]:
            out.append(NormalizedItem(ItemKind.DOCUMENT, "documents", f"{r.document_id}#{r.chunk_id}", None, r.title or r.filename, r.text[:240],
                                      {"filename": r.filename, "page": r.page, "score": round(r.score, 3), "document_id": r.document_id}, "high", r.document_id, now))
        return out

    def fetch(self, source_id: str) -> NormalizedItem:
        document_id = source_id.split("#", 1)[0]
        doc = self._rag.get_document(document_id)
        if doc is None:
            raise HubError(ErrorKind.NOT_FOUND, "That document isn't in the index.")
        text = "\n".join(c.text for c in self._rag.get_chunks(document_id))[:4000]
        return NormalizedItem(ItemKind.DOCUMENT, "documents", document_id, doc.indexed_at, doc.title or doc.filename, text[:600],
                              {"filename": doc.filename, "pages": doc.page_count, "chunks": doc.chunk_count, "location": doc.source_location}, "high", document_id, self._clock())
