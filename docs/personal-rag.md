# Personal RAG (Phase 7)

JARVIS can index your own documents locally and answer questions **grounded in
them**, with source references. It is separate from Phase 6 personal memory:
memory holds short structured facts about you; RAG holds document text.

## Architecture

```
Ingestion (scripts/rag_cli.py, RagService.ingest_file)
  file -> validate -> SHA-256 -> Loader -> Chunker -> EmbeddingProvider -> VectorStore

Question
  VoiceEngine -> ConversationEngine -> AgentBrain  (classifies: document_question + standalone query)
                       |                                   (the brain never sees document text)
                       v
                  RagService.answer
                     Retriever -> VectorStore (top-k, min score)
                       |  nothing relevant -> "I couldn't find enough information..." (no LLM call)
                       v
                  grounded prompt: rules + <retrieved_context> + reminder (+ <personal_memory>)
                       v
                  LLMProvider -> answer  (+ sources from retrieval, not from the model)
```

| Module | Role |
|--------|------|
| `agent/rag/models.py` | `Document`, `DocumentChunk`, `RetrievalResult`, `GroundedContext`, `RAGAnswer`, `SourceRef`, statuses, errors |
| `agent/rag/loaders.py` | `DocumentLoader` -> `TextLoader` (TXT/Markdown), `PdfLoader` (pypdf) |
| `agent/rag/chunker.py` | Deterministic character chunker |
| `agent/rag/embeddings.py` | `EmbeddingProvider`, `SentenceTransformerProvider` |
| `agent/rag/store.py` | `VectorStore`, `SqlVectorStore` |
| `agent/rag/documents.py` | `DocumentRepository` (document metadata) |
| `agent/rag/retriever.py` | `Retriever` (top-k + threshold) |
| `agent/rag/grounding.py` | Grounded prompt construction and fixed replies |
| `agent/rag/service.py` | `RagService`: ingestion, reindex, delete, search, answer |
| `backend/models/rag.py`, migration `0002_rag_tables` | `rag_documents`, `rag_chunks` |
| `scripts/rag_cli.py` | `ingest`, `list`, `reindex`, `delete`, `search` |

## Supported documents

TXT, Markdown (`.md`, `.markdown`), PDF (text only). DOCX was not added (it would
need another dependency). Not supported and rejected clearly: any other type,
encrypted PDFs, binary or non-UTF-8 text, scanned/image-only PDFs (no OCR; they
fail with "no extractable text"). No Gmail/WhatsApp/Calendar ingestion, no web
crawling, no cloud drives.

## Ingestion

`ingest_file(path)` returns an `IngestResult`:

| Outcome | Meaning |
|---------|---------|
| `indexed` / `reindexed` | new document / changed content replaced |
| `unchanged` | same file, same content hash: nothing done |
| `duplicate` | identical content is already indexed under another file: no second index (the file is left alone) |
| `rejected` | refused before processing: missing, not a file, unsupported type, empty, over the size limit (no record kept) |
| `failed` | processing failed (corrupt, no text, too many chunks, embedding error); document marked `FAILED` with a short reason that never contains document text |

Identity: content **hash** (SHA-256 of the bytes), not the filename; the absolute
path identifies "the same document changed". Statuses: `PENDING`, `PROCESSING`,
`INDEXED`, `FAILED`, `DELETED`. Originals are only read: never copied, modified,
moved or deleted. Text is extracted only: nothing in a document is executed
(no scripts, macros or PDF actions) and no links are followed.

Limits (rejected or failed as a whole, never partially indexed):
`JARVIS_RAG_MAX_DOCUMENT_SIZE_MB` (25) and `JARVIS_RAG_MAX_CHUNKS_PER_DOCUMENT` (2000).

## Extraction and chunking

TXT/Markdown are read as UTF-8 (Markdown title = first `#` heading). PDFs give one
page per PDF page, so every chunk keeps its page number. The chunker works per
page (a chunk never spans pages), splits into windows of at most
`JARVIS_RAG_CHUNK_SIZE` characters (800) with `JARVIS_RAG_CHUNK_OVERLAP` (100),
preferring paragraph, sentence then word boundaries, drops empty chunks and is fully
deterministic. It is character based, not semantic, and not token based.

## Embeddings

`EmbeddingProvider` (`embed_text`, `embed_documents`) with a local
`SentenceTransformerProvider` (default `sentence-transformers/all-MiniLM-L6-v2`,
384 dimensions, CPU). Documents and queries are embedded on this machine; nothing is
sent to a cloud API. The model is downloaded once from Hugging Face on first use
(the only network access RAG needs; loading takes several seconds, so the first
document question after starting JARVIS is slow) and cached. Vectors are
L2-normalized; each chunk records which model made it, and vectors from different
models are never compared (retrieval logs a warning if some chunks need re-indexing
after you change the model).

## Vector store: the choice

**PostgreSQL tables with float32 embeddings and an exact NumPy cosine search
(`SqlVectorStore`), not pgvector and not ChromaDB.**

- The project already runs on PostgreSQL/SQLAlchemy/Alembic (Phases 0 and 6), so
  documents (relational) and vectors live in one database with one migration path,
  and there is no second persistence system (ChromaDB would add one).
- pgvector needs an extension installed in the server, which is awkward on Windows
  and would be a hidden production requirement. Exact search over a personal
  collection (tens of thousands of chunks) does not need an index: a query scans the
  chunk vectors in batches (about 1.5 KB per chunk) and keeps the top-k.
- It runs identically on the isolated SQLite used by the unit tests and was verified
  here with real embeddings, unlike a pgvector setup I could not run.
- The `VectorStore` interface (`add`, `replace_document`, `delete_document`, `search`,
  `count`, `clear`) means pgvector or another engine can replace it later.
Limit: query time grows linearly with the number of chunks. Requires only the two
new tables (`alembic -c database/alembic.ini upgrade head`).

## Retrieval and threshold

`Retriever` embeds the query and returns the `JARVIS_RAG_TOP_K` (5) best chunks whose
cosine similarity is at least `JARVIS_RAG_MIN_SCORE` (0.35), only from `INDEXED`
documents. With the default model, related questions scored about 0.5 to 0.7 and
an unrelated one about 0.1; tune the threshold for your documents. Results carry chunk
id, document id, text, score, filename, title, source type and page; raw vectors are
never returned or shown to the LLM. Hybrid (lexical + vector) retrieval and
reranking are **not** implemented in Phase 7 (they would add complexity for a marginal
gain here); they are the natural next enhancements behind the same interfaces.

## Grounding and citations

If nothing clears the threshold JARVIS replies "I couldn't find enough information in
your personal documents to answer that." without calling the LLM. Otherwise the
system prompt tells the model to answer only from the passages, not to invent
facts, to reply exactly `INSUFFICIENT_CONTEXT` when the passages do not contain the
answer (treated as the insufficient-context result, not a grounded one), and to say
when it adds general knowledge. Passages appear as
`[Source: resume.pdf, page 2]` + text inside `<retrieved_context>`. Sources in
`RAGAnswer.sources` / `.citations` come from retrieval, never from model text; page
numbers exist only for PDFs and are omitted otherwise. Statuses: `GROUNDED`,
`INSUFFICIENT_CONTEXT`, `ERROR` (subsystem unavailable). `ConversationEngine` exposes
the latest one as `last_rag_answer`; the spoken reply is the answer text (citations are
not read aloud). Grounding depends on the model following the rules: a small model can
still over-reach, and "grounded" means retrieved evidence was supplied and the model did
not report insufficiency, not that every claim was verified.

## Agent brain and conversation

The brain gained one intent, `document_question` (offered only when RAG is enabled),
with a standalone `query` that resolves pronouns from the conversation ("What dataset
did I use?" after a question about a satellite project). The brain decides from the
conversation only; retrieved text never reaches it. `ConversationEngine` then calls
`RagService.answer`, passing the history it owns; RAG keeps no history. General
questions stay `information_request` and never touch RAG. If RAG is disabled the reply
says so; if the RAG subsystem fails the reply says it could not search; if the LLM
fails the error propagates and nothing is recorded (no grounded answer is claimed).

## Memory versus RAG

Separate tables, services and prompts. RAG answers are never saved as personal memory
(memory extraction only reads the user's own words, as in Phase 6). Both can inform one
answer: the `<personal_memory>` block is appended after the document block.

## Security and prompt injection

Document text is untrusted data. It is sanitized (angle brackets and control characters
removed, length-limited, labels cleaned so a filename cannot forge a source), wrapped in
`<retrieved_context>` and followed by a fixed reminder that it carries no authority.
The hierarchy is: system rules, then permission/security, then agent logic, then
document content. Concretely: the agent decision is made before retrieval and never sees
documents; the grounded answer path has no tools, no ingestion and no memory writes; the
`PermissionManager` is unchanged; and the LLM has no path to ingest or delete
documents (only `RagService` from code/CLI). A poisoned document is therefore quoted
text that a fooled model could at worst repeat, not act on. It is a mitigation, not a
guarantee against a model being persuaded in its wording.

## Deletion and reindex

`delete_document(id)` removes all chunks and vectors and marks the document `DELETED`
(the original file is untouched; the same file can be ingested again).
`reindex_document(id)` re-reads the file. Changed content is replaced atomically
(`replace_document` swaps old for new chunks in one transaction) so no stale chunks stay
active. If re-indexing a changed document fails, the previous index stays searchable and
the failure is recorded in `last_error`; a new document that fails is `FAILED`. A storage
outage raises `RAGStorageError` (no results are fabricated).

## Privacy

Local processing only; no cloud upload, external embedding API or telemetry. Logs carry
document ids, filenames and counts, never document text, chunks, embeddings or queries.
Database error messages are reduced to the exception type. Chunk text is stored in your
PostgreSQL (needed to show passages), so anyone with database access can read indexed
content; delete documents from the index to remove it. With local Ollama the whole path
stays on the machine.

## Configuration

| Setting | Default |
|---------|---------|
| `JARVIS_RAG_ENABLED` | `true` |
| `JARVIS_RAG_EMBEDDING_MODEL` | `sentence-transformers/all-MiniLM-L6-v2` |
| `JARVIS_RAG_TOP_K` | `5` |
| `JARVIS_RAG_CHUNK_SIZE` / `JARVIS_RAG_CHUNK_OVERLAP` | `800` / `100` |
| `JARVIS_RAG_MIN_SCORE` | `0.35` |
| `JARVIS_RAG_MAX_DOCUMENT_SIZE_MB` | `25` |
| `JARVIS_RAG_MAX_CHUNKS_PER_DOCUMENT` | `2000` |

## Using it

```powershell
alembic -c database/alembic.ini upgrade head
python scripts/rag_cli.py ingest C:\path\to\resume.pdf C:\path\to\notes-folder
python scripts/rag_cli.py list
python scripts/rag_cli.py search "programming languages"
```
Then ask JARVIS, e.g. "What technologies are listed in my resume?".

## Testing

`tests/test_rag_*.py`: loaders (TXT, Markdown, PDF, unsupported, corrupt), chunking,
ingestion, hashing, duplicates, unchanged, reindex, failed reindex, deletion, limits,
top-k, threshold, source/page metadata, insufficient context, grounded prompt
construction, `RAGAnswer`, brain integration, conversation and follow-up integration,
memory separation, prompt injection and the permission boundary. They use deterministic
bag-of-words embeddings, a scripted LLM and an isolated in-memory SQLite database (or a
disposable PostgreSQL when `JARVIS_TEST_DATABASE_URL` is set). A separate integration test
runs the **real** embedding model and store code on a scratch SQLite database (skipped if
the model is not downloaded). None of this proves behaviour on your PostgreSQL or with a
real Ollama model.

## Limitations

- Exact search scales linearly; very large collections need pgvector or another index.
- Character chunking; no OCR, tables, images or DOCX; PDFs with odd layouts extract poorly.
- No hybrid search or reranking; no incremental change detection inside a document.
- One embedding model at a time; changing it requires re-indexing.
- Generation quality and honesty depend on the local LLM.
- No UI: manage documents with `scripts/rag_cli.py` or the service API.
- Citations name the file and PDF page, not exact sentences.
