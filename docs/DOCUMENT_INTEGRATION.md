# Document integration (hub)

Builds on Phase 7 RAG (`docs/personal-rag.md`) — chunking, local embeddings, PostgreSQL vector store, honest "not found" — and Phase 17's DOCX loader. Phase 18 adds the watcher and the hub view.

## Formats

PDF (text only), DOCX (text only; XML bombs and `.docm` refused), TXT, Markdown. Everything else is skipped.

## Watching folders (opt-in)

Set `JARVIS_DOCUMENT_DIRS=C:\Users\you\Projects\JARVIS\docs;C:\Users\you\Documents\College` (separate with `;`) and enable RAG. Nothing is watched otherwise. Hidden files/folders (any path part starting with `.`) and unsupported types are ignored. `INDEX_DOCUMENTS` is an opt-in permission.

**Incremental.** A scan stats every file and compares `(modified time, size)` with the last scan. Only new/changed files are handed to `RagService.ingest_file`, which itself skips identical content by SHA-256. Measured on 300 files: first full indexing ≈1.3 s of JARVIS-side work (fake indexer, 7 bounded passes), an unchanged tree ≈0.18 s and no file is opened, 20 changed files ≈0.24 s. Each sync indexes at most `batch` (50) files; the rest continue next sync. A file the parser rejects is recorded (`outcome: failed`), does not fail the sync, and is retried only after the file changes. Deleted files are reported as removed from the hub view; their index entries are kept unless `JARVIS_DOCUMENT_REMOVE_DELETED=true`.

Polling, not OS file-system events (`ReadDirectoryChangesW`/watchdog were not added); the interval is 5 minutes.

## Provenance

Hub item per file: path, modified time, size, outcome, chunk/page counts. RAG results carry filename and page: "I found matches in syllabus.txt, page 1." and "Where did you get that?" names the document. Deadlines/events found in indexed documents are extracted by the Phase 17 engine (`SourceKind.DOCUMENT`, document id, evidence sentence, confidence).

## Search and read

"Search my documents for final project" → `search_documents` (vector retrieval above the RAG score threshold). No hits: **"I couldn't find that information in your authorized documents."** — never invented. `read_document` returns the first 4000 characters, marked untrusted.

## Gmail attachments

Email → attachment → document is possible but **never automatic**: `GmailAdapter.download_attachment` (opt-in `READ_ATTACHMENT`, ≤10 MB, document types only) saves into `.jarvis/attachments/`. Indexing that folder needs it listed in `JARVIS_DOCUMENT_DIRS`. There is no spoken "index this attachment" command yet.

## Limits

Scanned/image-only PDFs have no text (no OCR). The DOCX loader reads paragraphs and table text as paragraphs only.
