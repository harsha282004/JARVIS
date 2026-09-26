#!/usr/bin/env python
"""Manage the personal RAG index (Phase 7). Documents are only read, never modified or deleted.

Usage:
    python scripts/rag_cli.py ingest <file-or-folder> [...]
    python scripts/rag_cli.py list
    python scripts/rag_cli.py reindex <document_id>
    python scripts/rag_cli.py delete <document_id>     # removes it from the index only
    python scripts/rag_cli.py search "<query>"         # shows sources and scores, not text

Needs the schema (`alembic -c database/alembic.ini upgrade head`) and the local
embedding model (downloaded on first use).
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent.rag.loaders import SUPPORTED_EXTENSIONS  # noqa: E402
from agent.rag.models import RAGError  # noqa: E402
from backend.core.config import get_settings  # noqa: E402
from backend.core.llm.factory import build_llm  # noqa: E402
from backend.core.logging import configure_logging  # noqa: E402
from voice.bootstrap import build_rag_service  # noqa: E402


def _files(target: Path) -> list[Path]:
    if target.is_dir():
        return sorted(p for p in target.rglob("*") if p.is_file() and p.suffix.lower() in SUPPORTED_EXTENSIONS)
    return [target]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("ingest").add_argument("paths", nargs="+", type=Path)
    sub.add_parser("list")
    sub.add_parser("reindex").add_argument("document_id")
    sub.add_parser("delete").add_argument("document_id")
    sub.add_parser("search").add_argument("query")
    args = parser.parse_args(argv)

    settings = get_settings()
    configure_logging(settings.LOG_LEVEL)
    rag = build_rag_service(settings, build_llm(settings))
    if rag is None:
        print("JARVIS_RAG_ENABLED is false.")
        return 1

    try:
        if args.command == "ingest":
            failures = 0
            for target in args.paths:
                for path in _files(target):
                    result = rag.ingest_file(path)
                    doc = result.document
                    print(f"{result.outcome.value:10} {path.name}" + (f"  id={doc.document_id}" if doc else "")
                          + (f"  ({result.error})" if result.error else ""))
                    failures += result.outcome.value in ("failed", "rejected")
            return 1 if failures else 0
        if args.command == "list":
            for doc in rag.list_documents():
                print(f"{doc.document_id}  {doc.status.value:10} chunks={doc.chunk_count:<4} {doc.filename}"
                      + (f"  error={doc.last_error}" if doc.last_error else ""))
        elif args.command == "reindex":
            result = rag.reindex_document(args.document_id)
            print(result.outcome.value, result.error or "")
            return 0 if result.outcome.value in ("indexed", "reindexed", "unchanged") else 1
        elif args.command == "delete":
            print("Removed from the index." if rag.delete_document(args.document_id) else "No such indexed document.")
        elif args.command == "search":
            for r in rag.search(args.query):
                print(f"{r.score:.3f}  {r.source.citation}  chunk={r.chunk_id}")
    except RAGError as exc:
        print(f"RAG error: {exc}")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
