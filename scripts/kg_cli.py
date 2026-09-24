#!/usr/bin/env python
"""Inspect and maintain the personal knowledge graph (Phase 8). Read-only except the two sync commands.

Usage:
    python scripts/kg_cli.py stats
    python scripts/kg_cli.py sync-memory                 # rebuild memory-derived facts from active memories
    python scripts/kg_cli.py extract-doc <document_id>   # LLM extraction from an indexed document (uses Ollama)
    python scripts/kg_cli.py entities [text]
    python scripts/kg_cli.py related "<entity name>"
    python scripts/kg_cli.py path "<entity A>" "<entity B>"
    python scripts/kg_cli.py context "<question>"        # what would be shown to the LLM

Needs the schema (`alembic -c database/alembic.ini upgrade head`).
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent.knowledge_graph.context import GraphContextProvider, build_graph_block  # noqa: E402
from agent.knowledge_graph.extraction import GraphExtractor  # noqa: E402
from agent.knowledge_graph.models import GraphError  # noqa: E402
from agent.knowledge_graph.sync import DocumentGraphIngestor, MemoryGraphSync  # noqa: E402
from backend.core.config import get_settings  # noqa: E402
from backend.core.llm.ollama_provider import OllamaProvider  # noqa: E402
from backend.core.logging import configure_logging  # noqa: E402
from voice.bootstrap import _build_memory, build_graph_service, build_rag_service  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("stats")
    sub.add_parser("sync-memory")
    sub.add_parser("extract-doc").add_argument("document_id")
    sub.add_parser("entities").add_argument("text", nargs="?")
    sub.add_parser("related").add_argument("name")
    p = sub.add_parser("path")
    p.add_argument("a")
    p.add_argument("b")
    sub.add_parser("context").add_argument("question")
    args = parser.parse_args(argv)

    settings = get_settings()
    configure_logging(settings.LOG_LEVEL)
    graph = build_graph_service(settings)
    if graph is None:
        print("JARVIS_KG_ENABLED is false.")
        return 1

    try:
        if args.command == "stats":
            print(graph.stats())
        elif args.command == "sync-memory":
            memory = _build_memory(settings)
            if memory is None:
                print("JARVIS_MEMORY_ENABLED is false.")
                return 1
            print(f"{MemoryGraphSync(graph).sync_all(memory)} memories produced graph facts.")
        elif args.command == "extract-doc":
            llm = OllamaProvider(settings.OLLAMA_BASE_URL, settings.LLM_MODEL)
            rag = build_rag_service(settings, llm)
            if rag is None:
                print("JARVIS_RAG_ENABLED is false.")
                return 1
            report = DocumentGraphIngestor(graph, rag, GraphExtractor(llm, graph.min_confidence)).extract_document(args.document_id)
            print(report)
        elif args.command == "entities":
            for e in graph.search_entities(args.text):
                print(f"{e.entity_id}  {e.entity_type.value:12} {e.canonical_name}")
        elif args.command == "related":
            entity = graph.resolve_entity(args.name)
            if entity is None:
                print("No unambiguous entity with that name.")
                return 1
            for r in graph.find_related_entities(entity.entity_id):
                arrow = "-->" if r.direction == "out" else "<--"
                print(f"{entity.canonical_name} {arrow}[{r.relationship.relationship_type.value}] {r.entity.canonical_name}"
                      f"  (trust={r.relationship.trust.name.lower()}, confidence={r.relationship.confidence.name.lower()})")
        elif args.command == "path":
            a, b = graph.resolve_entity(args.a), graph.resolve_entity(args.b)
            if a is None or b is None:
                print("Both names must resolve to exactly one entity.")
                return 1
            for path in graph.find_paths(a.entity_id, b.entity_id):
                print(" -> ".join(str(graph.get_entity(i).canonical_name) for i in path.entity_ids))
        elif args.command == "context":
            print(build_graph_block(GraphContextProvider(graph).context_for(args.question)) or "(no relevant graph facts)")
    except GraphError as exc:
        print(f"Graph error: {exc}")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
