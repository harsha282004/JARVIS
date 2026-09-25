"""REAL_WORLD_TESTS: the Personal Operator's browser steps driven through a REAL browser (installed Edge/Chrome, headless) against a local test website. Offline and deterministic (no
live site, no account); skipped, with the reason, where no browser can be launched. Email and calendar data are the deterministic fakes; only the browser is real."""

import pytest

from tests.browser.test_real_browser_local import PAGES, make_real, site  # noqa: F401 - `site` is the local test server fixture
from tests.workflow_helpers import OpRig
from workflows.models import SStatus, WStatus

PAGES["/apply"] = "<title>Apply</title><h1>Internship application</h1><p>Fill in the form.</p><button onclick=\"document.title='submitted'\">Submit application</button>"


@pytest.fixture
def real(tmp_path, site):  # noqa: F811
    engine = make_real(tmp_path)
    probe = engine.open_url(site + "/")
    if not probe.success:
        engine.shutdown()
        pytest.skip(f"no real browser could be launched here: {probe.error}")
    rig = OpRig(tmp_path / "op", threaded=True, engine=engine) if (tmp_path / "op").mkdir() is None else None
    yield rig, site, engine
    rig.close()


def plan(site, *tail):
    steps = [{"id": "s1", "tool": "open_url", "arguments": {"url": site + "/apply"}, "description": "Open the application page"},
             {"id": "s2", "tool": "read_page", "arguments": {}, "depends_on": ["s1"], "description": "Read the page"}]
    steps.extend(tail)
    return {"steps": steps}


def test_real_browser_read_only_workflow(real):
    rig, site, engine = real
    reply = rig.op.submit_proposal("open and read the page", plan(site), "s1")
    wf = reply.workflow
    assert rig.wait(lambda: wf.terminal, 60) and wf.status is WStatus.COMPLETED, [(s.tool, s.status, s.note) for s in wf.steps]
    assert engine.status()["url"].endswith("/apply") and engine.status()["title"] == "Apply"


def test_real_browser_submit_waits_for_confirmation_and_only_then_clicks(real):
    rig, site, engine = real
    click = {"id": "s3", "tool": "click_element", "arguments": {"name": "Submit application", "role": "button"}, "depends_on": ["s2"], "description": "Submit the application"}
    reply = rig.op.submit_proposal("apply", plan(site, click), "s1")
    wf = reply.workflow
    assert rig.wait(lambda: wf.status is WStatus.WAITING_FOR_CONFIRMATION, 60), [(s.tool, s.status, s.note) for s in wf.steps]
    assert engine.status()["title"] == "Apply"                                     # the page has NOT been submitted while the question is open
    assert rig.say("yes") == "Okay, continuing."
    assert rig.wait(lambda: wf.terminal, 60) and wf.status is WStatus.COMPLETED
    assert engine.status()["title"] == "submitted"


def test_real_browser_hostile_page_is_read_as_data_and_nothing_is_clicked(real):
    rig, site, engine = real
    hostile = {"steps": [{"id": "s1", "tool": "open_url", "arguments": {"url": site + "/evil"}, "description": "Open the page"},
                         {"id": "s2", "tool": "read_page", "arguments": {}, "depends_on": ["s1"], "description": "Read the page"}]}
    wf = rig.op.submit_proposal("read", hostile, "s1").workflow
    assert rig.wait(lambda: wf.terminal, 60) and wf.status is WStatus.COMPLETED
    assert all(s.tool in ("open_url", "read_page") for s in wf.steps) and engine.status()["title"] == "Nice page"
    assert not rig.confirmations.has_pending("s1")
