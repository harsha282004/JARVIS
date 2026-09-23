"""RagService ingestion, indexing, retrieval, reindex and deletion, on an isolated database
(in-memory SQLite by default; also a disposable PostgreSQL if JARVIS_TEST_DATABASE_URL is set).
Deterministic fake embeddings; no model download."""

import hashlib
from datetime import datetime, timezone

import pytest

import backend.models.rag  # noqa: F401  (registers tables for the shared fixtures)
from agent.rag.chunker import Chunker
from agent.rag.documents import DocumentRepository
from agent.rag.models import DocumentStatus, IngestOutcome, RAGStorageError
from agent.rag.retriever import Retriever
from agent.rag.service import RagLimits, RagService
from agent.rag.store import SqlVectorStore
from backend.models.base import Base
from tests.rag_helpers import BagOfWordsEmbedder, ScriptedLLM, make_pdf

DOC = "JARVIS test document.\nThe project uses FastAPI for the backend.\nThe frontend uses React."


def build(session_factory, embedder=None, top_k=5, min_score=0.2, limits=None, chunk_size=800, overlap=100, llm=None):
    embedder = embedder or BagOfWordsEmbedder()
    store = SqlVectorStore(session_factory)
    service = RagService(
        DocumentRepository(session_factory), store, embedder, Retriever(embedder, store, top_k, min_score),
        llm or ScriptedLLM(), Chunker(chunk_size, overlap), limits or RagLimits(),
        clock=lambda: datetime(2030, 1, 1, tzinfo=timezone.utc),
    )
    return service, store, embedder


@pytest.fixture
def rag(session_factory):
    return build(session_factory)


def write(tmp_path, name, text):
    path = tmp_path / name
    path.write_bytes(text.encode("utf-8"))
    return path


# ---- ingestion ----

def test_txt_ingestion_indexes_and_records_metadata(rag, tmp_path):
    service, store, _ = rag
    result = service.ingest_file(write(tmp_path, "notes.txt", DOC))
    doc = result.document
    assert result.outcome is IngestOutcome.INDEXED and doc.status is DocumentStatus.INDEXED
    assert doc.filename == "notes.txt" and doc.source_type.value == "txt" and doc.chunk_count == 1
    assert doc.content_hash == hashlib.sha256(DOC.encode()).hexdigest()
    assert doc.indexed_at is not None and doc.last_error is None and store.count() == 1
    assert service.get_document(doc.document_id).source_location == str((tmp_path / "notes.txt").resolve())


def test_markdown_ingestion_records_title(rag, tmp_path):
    service, _, _ = rag
    doc = service.ingest_file(write(tmp_path, "report.md", "# Satellite Project\n\nWe used a U-Net model.")).document
    assert doc.source_type.value == "markdown" and doc.title == "Satellite Project"


def test_pdf_ingestion_keeps_page_information(rag, tmp_path):
    service, _, _ = rag
    path = tmp_path / "report.pdf"
    path.write_bytes(make_pdf(["Introduction about goals.", "The model architecture is a transformer encoder."]))
    doc = service.ingest_file(path).document
    assert doc.source_type.value == "pdf" and doc.page_count == 2 and doc.chunk_count == 2
    [hit] = service.search("model architecture transformer")
    assert hit.page == 2 and hit.filename == "report.pdf"


def test_unsupported_missing_and_directory_are_rejected_without_records(rag, tmp_path):
    service, _, _ = rag
    exe = write(tmp_path, "run.exe", "MZ")
    for target in (exe, tmp_path / "nope.txt", tmp_path):
        result = service.ingest_file(target)
        assert result.outcome is IngestOutcome.REJECTED and result.error and result.document is None
    assert service.list_documents() == []


def test_empty_file_is_rejected(rag, tmp_path):
    service, _, _ = rag
    assert service.ingest_file(write(tmp_path, "empty.txt", "")).outcome is IngestOutcome.REJECTED


def test_whitespace_only_document_fails_and_is_not_reported_indexed(rag, tmp_path):
    service, store, _ = rag
    result = service.ingest_file(write(tmp_path, "blank.txt", "   \n\n  \t "))
    assert result.outcome is IngestOutcome.FAILED and result.document.status is DocumentStatus.FAILED
    assert "No extractable text" in result.error and store.count() == 0


def test_corrupt_documents_fail_with_a_reason_but_no_content(rag, tmp_path):
    service, store, _ = rag
    bad_pdf = tmp_path / "bad.pdf"
    bad_pdf.write_bytes(b"%PDF-1.4 garbage TOP-SECRET-TEXT")
    binary = tmp_path / "bin.txt"
    binary.write_bytes(b"ab\x00cd")
    for path in (bad_pdf, binary):
        result = service.ingest_file(path)
        assert result.outcome is IngestOutcome.FAILED and result.document.status is DocumentStatus.FAILED
        assert "TOP-SECRET-TEXT" not in (result.error or "") and result.document.last_error
    assert store.count() == 0
    assert [d.status for d in service.list_documents(statuses=[DocumentStatus.INDEXED])] == []


# ---- hashing / duplicates / unchanged ----

def test_reingesting_an_unchanged_file_does_nothing(rag, tmp_path):
    service, store, embedder = rag
    path = write(tmp_path, "a.txt", DOC)
    first = service.ingest_file(path)
    calls = embedder.calls
    second = service.ingest_file(path)
    assert second.outcome is IngestOutcome.UNCHANGED and second.document.document_id == first.document.document_id
    assert embedder.calls == calls and store.count() == 1 and len(service.list_documents()) == 1


def test_identical_content_under_another_filename_is_recognized_as_duplicate(rag, tmp_path):
    service, store, _ = rag
    first = service.ingest_file(write(tmp_path, "a.txt", DOC))
    dup = service.ingest_file(write(tmp_path, "copy_of_a.md", DOC))
    assert dup.outcome is IngestOutcome.DUPLICATE and dup.duplicate_of == first.document.document_id
    assert store.count() == 1 and len(service.list_documents()) == 1
    assert (tmp_path / "copy_of_a.md").exists()  # the user's file is left alone


# ---- reindex ----

def test_changed_document_is_reindexed_and_old_chunks_are_gone(rag, tmp_path):
    service, store, _ = rag
    path = write(tmp_path, "a.txt", "The project uses FastAPI for the backend.")
    first = service.ingest_file(path).document
    write(tmp_path, "a.txt", "The project uses Django for the backend service.")
    result = service.ingest_file(path)
    assert result.outcome is IngestOutcome.REINDEXED and result.document.document_id == first.document_id
    assert result.document.content_hash != first.content_hash and store.count() == 1
    texts = [r.text for r in service.search("backend framework")]
    assert texts == ["The project uses Django for the backend service."]  # no stale FastAPI chunk


def test_failed_reindex_keeps_the_previous_valid_index(session_factory, tmp_path):
    embedder = BagOfWordsEmbedder()
    service, store, _ = build(session_factory, embedder)
    path = write(tmp_path, "a.txt", "The project uses FastAPI for the backend.")
    doc = service.ingest_file(path).document
    write(tmp_path, "a.txt", "Completely different content about databases.")
    embedder.fail = True
    result = service.ingest_file(path)
    assert result.outcome is IngestOutcome.FAILED and result.kept_previous_index is True
    kept = service.get_document(doc.document_id)
    assert kept.status is DocumentStatus.INDEXED and kept.last_error and kept.content_hash == doc.content_hash
    embedder.fail = False  # (the search below needs the embedder too)
    assert [r.text for r in service.search("backend framework")] == ["The project uses FastAPI for the backend."]
    assert service.ingest_file(path).outcome is IngestOutcome.REINDEXED  # retry succeeds later


def test_forced_reindex_of_an_unchanged_document(rag, tmp_path):
    service, _, embedder = rag
    doc = service.ingest_file(write(tmp_path, "a.txt", DOC)).document
    calls = embedder.calls
    assert service.reindex_document(doc.document_id).outcome is IngestOutcome.REINDEXED
    assert embedder.calls == calls + 1
    assert service.reindex_document("f" * 32).outcome is IngestOutcome.REJECTED


def test_embedding_failure_leaves_no_partial_index(session_factory, tmp_path):
    service, store, embedder = build(session_factory, BagOfWordsEmbedder(fail=True))
    result = service.ingest_file(write(tmp_path, "a.txt", DOC))
    assert result.outcome is IngestOutcome.FAILED and result.document.status is DocumentStatus.FAILED
    assert store.count() == 0


# ---- deletion ----

def test_delete_removes_chunks_and_vectors_but_not_the_file(rag, tmp_path):
    service, store, _ = rag
    path = write(tmp_path, "a.txt", DOC)
    doc = service.ingest_file(path).document
    assert service.delete_document(doc.document_id) is True
    assert store.count() == 0 and service.search("backend framework") == []
    assert service.get_document(doc.document_id).status is DocumentStatus.DELETED
    assert path.read_bytes() == DOC.encode()  # original untouched
    assert service.delete_document(doc.document_id) is False and service.delete_document("0" * 32) is False


def test_a_deleted_document_can_be_ingested_again(rag, tmp_path):
    service, store, _ = rag
    path = write(tmp_path, "a.txt", DOC)
    doc = service.ingest_file(path).document
    service.delete_document(doc.document_id)
    again = service.ingest_file(path)
    assert again.outcome is IngestOutcome.INDEXED and again.document.document_id == doc.document_id and store.count() == 1


# ---- limits ----

def test_oversized_document_is_rejected_not_partially_indexed(session_factory, tmp_path):
    service, store, _ = build(session_factory, limits=RagLimits(max_document_bytes=1000))
    result = service.ingest_file(write(tmp_path, "big.txt", "word " * 500))
    assert result.outcome is IngestOutcome.REJECTED and "limit" in result.error
    assert store.count() == 0 and service.list_documents() == []


def test_too_many_chunks_fails_cleanly_without_partial_index(session_factory, tmp_path):
    service, store, _ = build(session_factory, limits=RagLimits(max_chunks_per_document=3), chunk_size=100, overlap=10)
    result = service.ingest_file(write(tmp_path, "long.txt", "alpha beta gamma delta " * 100))
    assert result.outcome is IngestOutcome.FAILED and "chunks" in result.error and store.count() == 0
    assert result.document.status is DocumentStatus.FAILED


# ---- retrieval ----

def test_retrieval_returns_relevant_chunk_with_source_metadata(rag, tmp_path):
    service, _, _ = rag
    doc = service.ingest_file(write(tmp_path, "readme.md", "# JARVIS\n\n" + DOC)).document
    [hit] = service.search("What backend framework does the project use?")
    assert "FastAPI" in hit.text and hit.document_id == doc.document_id
    assert (hit.filename, hit.title, hit.source_type.value, hit.page) == ("readme.md", "JARVIS", "markdown", None)
    assert hit.score > 0.2 and hit.chunk_id and hit.chunk_index == 0
    assert hit.source.citation == "[Source: readme.md]"
    assert not hasattr(hit, "embedding") and "embedding" not in hit.model_dump()


def test_pdf_hits_carry_page_citations(rag, tmp_path):
    service, _, _ = rag
    path = tmp_path / "resume.pdf"
    path.write_bytes(make_pdf(["Skills: Python and Java.", "Education: Computer Science degree."]))
    service.ingest_file(path)
    [hit] = service.search("education degree")
    assert hit.source.citation == "[Source: resume.pdf, page 2]"


def test_top_k_limits_results_and_orders_by_score(session_factory, tmp_path):
    service, _, _ = build(session_factory, top_k=2, chunk_size=100, overlap=0)
    topics = ("code", "tests", "docs", "tools")
    text = "\n\n".join(f"python {topic} are described here in detail" for topic in topics) + "\n\njava only"
    service.ingest_file(write(tmp_path, "a.txt", text))
    hits = service.search("python")
    assert len(hits) == 2 and hits[0].score >= hits[1].score


def test_relevance_threshold_filters_everything_when_nothing_matches(rag, tmp_path):
    service, _, _ = rag
    service.ingest_file(write(tmp_path, "a.txt", DOC))
    assert service.search("What is the capital of France?") == []
    assert service.search("   ") == []


def test_chunks_from_a_different_embedding_model_are_ignored(session_factory, tmp_path):
    a, _, _ = build(session_factory, BagOfWordsEmbedder("model-a"))
    a.ingest_file(write(tmp_path, "a.txt", DOC))
    b, _, _ = build(session_factory, BagOfWordsEmbedder("model-b"))
    assert b.search("backend framework") == []
    assert len(a.search("backend framework")) == 1


def test_only_indexed_documents_are_searchable(rag, tmp_path):
    service, _, _ = rag
    doc = service.ingest_file(write(tmp_path, "a.txt", DOC)).document
    repo = service._docs
    repo.save(doc.model_copy(update={"status": DocumentStatus.PROCESSING}))
    assert service.search("backend framework") == []


# ---- storage failure ----

class BrokenSessions:
    def __call__(self):
        from sqlalchemy.exc import OperationalError

        raise OperationalError("SELECT document-text", {}, Exception("db down"))


def test_storage_outage_raises_a_content_free_error_and_fabricates_nothing(tmp_path):
    embedder = BagOfWordsEmbedder()
    store = SqlVectorStore(BrokenSessions())
    service = RagService(
        DocumentRepository(BrokenSessions()), store, embedder, Retriever(embedder, store, 5, 0.2), ScriptedLLM()
    )
    with pytest.raises(RAGStorageError) as exc:
        service.ingest_file(write(tmp_path, "a.txt", DOC))
    assert "document-text" not in str(exc.value)
    with pytest.raises(RAGStorageError):
        service.search("backend")


def test_logs_never_contain_document_text(rag, tmp_path, caplog):
    service, _, _ = rag
    with caplog.at_level("DEBUG"):
        service.ingest_file(write(tmp_path, "private.txt", "My salary secret is XYZZY-PRIVATE-TEXT."))
        service.search("salary")
    assert "XYZZY-PRIVATE-TEXT" not in caplog.text and "private.txt" in caplog.text
