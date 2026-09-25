"""DOCX loading (hostile input included), document intelligence, and corrupted-configuration handling."""

import os
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

from agent.rag.loaders import load_document
from agent.rag.models import CorruptDocument, SourceType, UnsupportedDocument

ROOT = Path(__file__).resolve().parents[2]
W = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'


def make_docx(path, body_xml):
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("word/document.xml", f'<?xml version="1.0"?><w:document {W}><w:body>{body_xml}</w:body></w:document>')
    return path


def test_docx_text_is_extracted(tmp_path):
    p = make_docx(tmp_path / "plan.docx", "<w:p><w:r><w:t>Project plan</w:t></w:r></w:p><w:p><w:r><w:t>Submit report by Friday.</w:t></w:r><w:r><w:br/></w:r></w:p>")
    doc = load_document(p)
    assert doc.source_type is SourceType.DOCX and "Project plan" in doc.pages[0].text and "Submit report by Friday." in doc.pages[0].text


def test_docx_xml_bomb_and_doctype_are_refused(tmp_path):
    p = tmp_path / "bomb.docx"
    with zipfile.ZipFile(p, "w") as z:
        z.writestr("word/document.xml", '<?xml version="1.0"?><!DOCTYPE lolz [<!ENTITY a "aaaa">]><w:document ' + W + "><w:body/></w:document>")
    with pytest.raises(CorruptDocument):
        load_document(p)


@pytest.mark.parametrize("payload", [b"not a zip", b""])
def test_corrupt_docx_raises_typed_error(tmp_path, payload):
    p = tmp_path / "bad.docx"
    p.write_bytes(payload)
    with pytest.raises(CorruptDocument):
        load_document(p)


def test_docm_is_not_accepted(tmp_path):
    with pytest.raises(UnsupportedDocument):
        load_document(tmp_path / "macro.docm")


def test_document_extraction_finds_deadlines_with_source_reference():
    """Document intelligence: text from an indexed document becomes deadlines/events that point back at the document."""
    from agent.intelligence.context_engine import PersonalContextEngine
    from agent.intelligence.models import DocumentItem, Snapshot, SourceKind, SourceState
    from tests.intelligence_helpers import IST, NOW

    snap = Snapshot(now=NOW, zone=IST, documents=[DocumentItem("d1", "Course syllabus", NOW, "Assignment 2 is due on October 3. The midterm exam is on October 12 at 10 AM.")],
                    states={"documents": SourceState.OK})
    r = PersonalContextEngine(IST, lambda: NOW).build(snap)
    assert r.deadlines and all(d.source.source_type is SourceKind.DOCUMENT and d.source.source_id == "d1" for d in r.deadlines)
    assert any(e.kind.value == "exam" for e in r.graph.entities.values())


def test_corrupted_configuration_exits_with_code_2_and_never_starts(tmp_path):
    env = {**os.environ, "DATABASE_URL": "sqlite://", "JARVIS_WORKDAY_START": "25:99", "JARVIS_STATE_DIR": str(tmp_path)}
    r = subprocess.run([sys.executable, "-m", "desktop.launcher"], cwd=ROOT, env=env, capture_output=True, text=True, timeout=60)
    assert r.returncode == 2
