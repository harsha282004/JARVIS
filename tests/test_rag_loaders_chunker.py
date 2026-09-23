"""Document loaders (TXT, Markdown, PDF) and the deterministic chunker."""

import pytest

from agent.rag.chunker import Chunker, normalize_text
from agent.rag.loaders import Page, detect_source_type, load_document
from agent.rag.models import CorruptDocument, SourceType, UnsupportedDocument
from tests.rag_helpers import make_pdf


# ---- loaders ----

def test_txt_is_loaded_as_a_single_page(tmp_path):
    f = tmp_path / "notes.txt"
    f.write_bytes(b"Hello world.\nSecond line.")  # bytes: avoid Windows newline translation
    doc = load_document(f)
    assert doc.source_type is SourceType.TXT and [(p.number, p.text) for p in doc.pages] == [(None, "Hello world.\nSecond line.")]
    assert doc.page_count is None and doc.title is None


def test_markdown_title_comes_from_the_first_heading(tmp_path):
    f = tmp_path / "readme.md"
    f.write_text("intro\n\n# Project Report\n\nBody text.", encoding="utf-8")
    doc = load_document(f)
    assert doc.source_type is SourceType.MARKDOWN and doc.title == "Project Report"
    assert "Body text." in doc.pages[0].text


def test_utf8_bom_is_handled(tmp_path):
    f = tmp_path / "bom.txt"
    f.write_bytes(b"\xef\xbb\xbfcaf\xc3\xa9")
    assert load_document(f).pages[0].text == "café"


def test_pdf_pages_keep_their_page_numbers(tmp_path):
    f = tmp_path / "report.pdf"
    f.write_bytes(make_pdf(["Page one text about FastAPI.", "Page two text about React."]))
    doc = load_document(f)
    assert doc.source_type is SourceType.PDF and doc.page_count == 2
    assert [p.number for p in doc.pages] == [1, 2]
    assert "FastAPI" in doc.pages[0].text and "React" in doc.pages[1].text


@pytest.mark.parametrize("name", ["a.docx", "a.exe", "a", "a.png", "a.html"])
def test_unsupported_types_are_refused(tmp_path, name):
    with pytest.raises(UnsupportedDocument):
        detect_source_type(tmp_path / name)


def test_binary_and_non_utf8_text_are_corrupt(tmp_path):
    binary = tmp_path / "b.txt"
    binary.write_bytes(b"abc\x00\x01\x02def")
    latin = tmp_path / "l.txt"
    latin.write_bytes(b"caf\xe9")
    for f in (binary, latin):
        with pytest.raises(CorruptDocument):
            load_document(f)


def test_corrupt_pdf_fails_clearly_without_echoing_content(tmp_path):
    f = tmp_path / "bad.pdf"
    f.write_bytes(b"%PDF-1.4 this is not really a pdf SECRET-CONTENT")
    with pytest.raises(CorruptDocument) as exc:
        load_document(f)
    assert "SECRET-CONTENT" not in str(exc.value)


def test_missing_file_is_a_corrupt_document_error(tmp_path):
    with pytest.raises(CorruptDocument):
        load_document(tmp_path / "gone.txt")


def test_document_text_is_never_executed(tmp_path):
    f = tmp_path / "evil.md"
    f.write_text("# x\n<script>alert(1)</script>\n[link](http://example.com/steal)\n```\nimport os; os.system('calc')\n```",
                 encoding="utf-8")
    assert "os.system" in load_document(f).pages[0].text  # kept as inert text


# ---- chunker ----

def test_chunker_validates_its_configuration():
    for size, overlap in [(10, 0), (100, 100), (100, -1)]:
        with pytest.raises(ValueError):
            Chunker(size, overlap)


def test_short_text_is_one_chunk():
    [chunk] = Chunker(200, 20).chunk_pages([Page(None, "A short note.")])
    assert (chunk.index, chunk.text, chunk.page, chunk.char_start) == (0, "A short note.", None, 0)


def test_chunks_respect_size_order_and_indexes():
    text = " ".join(f"word{i}" for i in range(400))
    chunks = Chunker(200, 40).chunk_pages([Page(None, text)])
    assert len(chunks) > 5
    assert all(0 < len(c.text) <= 200 for c in chunks)
    assert [c.index for c in chunks] == list(range(len(chunks)))
    assert [c.char_start for c in chunks] == sorted(c.char_start for c in chunks)


def test_overlap_repeats_text_between_neighbours():
    text = " ".join(f"w{i:03d}" for i in range(200))
    chunks = Chunker(120, 40).chunk_pages([Page(None, text)])
    for a, b in zip(chunks, chunks[1:]):
        tail_words = a.text.split()[-3:]
        assert any(w in b.text for w in tail_words)


def test_zero_overlap_partitions_the_text():
    text = "abcdefghij " * 60
    chunks = Chunker(100, 0).chunk_pages([Page(None, text)])
    assert "".join(c.text.replace(" ", "") for c in chunks) == text.replace(" ", "")


def test_chunking_is_deterministic():
    pages = [Page(1, "Alpha beta gamma. " * 80), Page(2, "Delta epsilon. " * 80)]
    assert Chunker(150, 30).chunk_pages(pages) == Chunker(150, 30).chunk_pages(pages)


def test_chunks_never_span_pages_and_keep_page_numbers():
    pages = [Page(1, "first page " * 40), Page(2, "second page " * 40), Page(3, "third page " * 40)]
    chunks = Chunker(120, 20).chunk_pages(pages)
    for chunk in chunks:
        word = {1: "first", 2: "second", 3: "third"}[chunk.page]
        assert word in chunk.text and all(w not in chunk.text for w in {"first", "second", "third"} - {word})
    assert [c.page for c in chunks] == sorted(c.page for c in chunks)
    assert {c.page for c in chunks} == {1, 2, 3}


def test_empty_and_whitespace_documents_produce_no_chunks():
    assert Chunker().chunk_pages([Page(None, ""), Page(None, "  \n\t \n ")]) == []
    assert Chunker().chunk_pages([]) == []


def test_no_chunk_is_ever_empty():
    text = "x" * 500 + "\n\n\n\n" + " " * 300 + "y" * 500
    assert all(c.text.strip() for c in Chunker(100, 10).chunk_pages([Page(None, text)]))


def test_long_unbroken_text_still_advances():
    chunks = Chunker(100, 30).chunk_pages([Page(None, "z" * 1000)])
    assert len(chunks) >= 10 and all(len(c.text) <= 100 for c in chunks)


def test_normalize_text_collapses_whitespace_and_nulls():
    assert normalize_text("a \t b\r\n\r\n\r\n\r\nc\x00") == "a b\n\nc"
