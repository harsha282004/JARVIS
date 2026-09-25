"""Cross-integration workflows (deterministic, no personal accounts): the ten scenarios of the Phase 22 specification, over the real planner/runner/tools."""

import pytest

from agent.tasks.models import TaskPriority
from tests.calendar_helpers import cal_event
from tests.intelligence_helpers import email_raw, ist
from tests.workflow_helpers import OpRig, internship_email
from workflows.models import SStatus, WStatus


@pytest.fixture
def rig(tmp_path):
    r = OpRig(tmp_path, emails=[internship_email()])
    yield r
    r.close()


# ---- 1. Gmail -> Task ---------------------------------------------------------------------------------------------------------------------

def test_gmail_to_task_with_provenance(rig):
    text = rig.say("Find the internship email and create a task for the deadline.")
    assert text.startswith("The deadline is October 15") and "Created the task" in text
    (task,) = rig.tasks()
    assert task.due_at.astimezone(rig.h.hub.zone).date().isoformat() == "2026-10-15"
    prov = task.metadata["provenance"]
    assert prov["source"] == "gmail" and prov["source_id"] == "m1" and prov["status"] == "VERIFIED"
    assert task.metadata["idempotency_key"] and "workflow_id" in task.metadata
    assert not rig.reminders()


# ---- 2. Gmail -> Reminder -----------------------------------------------------------------------------------------------------------------

def test_gmail_to_reminder_two_days_before(rig):
    text = rig.say("Find the internship email and remind me two days before the deadline.")
    assert "October 13" in text
    (rem,) = rig.reminders()
    local = rem.scheduled_at.astimezone(rig.h.hub.zone)
    assert (local.month, local.day, local.hour) == (10, 13, 9)
    assert rem.metadata["provenance"]["source_id"] == "m1"
    assert not rig.tasks()


# ---- 3. Calendar -> Gmail -----------------------------------------------------------------------------------------------------------------

def test_calendar_to_related_email(tmp_path):
    r = OpRig(tmp_path, emails=[email_raw()], calendar_events=[cal_event("rev1", "JARVIS Project Review", ist(25, 11), ist(25, 12))])
    try:
        text = r.say("Check tomorrow's calendar and related emails and prepare for my meetings.")
        assert "JARVIS Project Review" in text and "Related emails" in text and "JARVIS project review" in text
        wf = r.op.last()
        assert wf.status is WStatus.COMPLETED and wf.template == "meeting_preparation"
        assert {"calendar", "gmail"} <= wf.scope
    finally:
        r.close()


# ---- 4. GitHub -> Task --------------------------------------------------------------------------------------------------------------------

def test_github_activity_to_tasks_verifies_identity(tmp_path):
    r = OpRig(tmp_path)
    try:
        text = r.say("Find my GitHub activity from this week and add anything important to my task list.")
        assert "harsh" in text and "Created 2 tasks" in text
        wf = r.op.last()
        assert wf.status is WStatus.COMPLETED
        assert wf.steps[0].tool == "github_identity" and wf.steps[0].output["login"] == "harsh"
        titles = [t.title for t in r.tasks()]
        assert any("Dashboard cards" in t for t in titles) and any("Add GitHub adapter" in t for t in titles)
        assert all(t.due_at is None for t in r.tasks())           # a GitHub item is not a deadline
        assert r.tasks()[0].metadata["provenance"]["source"] == "github"
    finally:
        r.close()


# ---- 5. Document -> Task ------------------------------------------------------------------------------------------------------------------

def test_document_deadline_to_task(tmp_path):
    r = OpRig(tmp_path)
    try:
        (r.h.docs_dir / "scholarship.txt").write_text("Scholarship guide. The scholarship application must be submitted by November 20, 2026.\n\nRequired documents:\n- Transcript\n- Recommendation letter\n", encoding="utf-8")
        r.h.sync("documents")
        text = r.say("Find the scholarship deadline in my documents and create a task for it.")
        wf = r.op.last()
        assert wf.status is WStatus.COMPLETED, (text, [(s.tool, s.status, s.note) for s in wf.steps])
        (task,) = r.tasks()
        assert task.due_at.astimezone(r.h.hub.zone).date().isoformat() == "2026-11-20"
        assert task.metadata["provenance"]["source"] == "documents"
    finally:
        r.close()


# ---- 6. Gmail -> Browser ------------------------------------------------------------------------------------------------------------------

def test_email_link_opens_and_reads_page(tmp_path):
    r = OpRig(tmp_path, emails=[internship_email()], browser=True)
    try:
        text = r.say("Open the application link from the internship email.")
        wf = r.op.last()
        assert wf.status is WStatus.COMPLETED, (text, [(s.tool, s.status, s.note) for s in wf.steps])
        assert "jobs.example.com" in r.engine.status()["url"]
        assert [s.tool for s in wf.steps] == ["gmail_search", "email_links", "open_url", "read_page"]
    finally:
        r.close()


# ---- 7. Calendar -> Notification ----------------------------------------------------------------------------------------------------------

def test_calendar_to_notification_is_deduplicated(tmp_path):
    r = OpRig(tmp_path, emails=[email_raw()], calendar_events=[cal_event("rev1", "JARVIS Project Review", ist(25, 11), ist(25, 12))])
    try:
        r.say("Prepare for tomorrow's meetings and notify me.")
        assert len(r.notified) == 1 and "JARVIS Project Review" in r.notified[0][0]
        again = r.say("Prepare for tomorrow's meetings and notify me.")
        assert len(r.notified) == 1                              # the same announcement is not repeated
        assert "already told you" in " ".join(s.note for s in r.op.last().steps) and again
    finally:
        r.close()


# ---- 8. Email -> Deadline -> Task -> Reminder ------------------------------------------------------------------------------------------

def test_email_deadline_task_and_reminder(rig):
    text = rig.say("Find the internship email, identify the deadline, create a task for it, and remind me two days before.")
    assert "Done" in text or text.startswith("The deadline is October 15")
    assert "Created the task" in text and "Scheduled a reminder" in text
    assert len(rig.tasks()) == 1 and len(rig.reminders()) == 1
    wf = rig.op.last()
    assert [s.status for s in wf.steps] == [SStatus.DONE] * len(wf.steps)
    assert wf.result.actions_completed and "Gmail" in wf.result.sources


# ---- 9. Morning briefing --------------------------------------------------------------------------------------------------------------

def test_morning_briefing_and_tell_me_more(tmp_path):
    r = OpRig(tmp_path, emails=[email_raw(subject="URGENT: submit the report", body="Urgent: please submit the report today.")],
              calendar_events=[cal_event("m1", "Team sync", ist(24, 15), ist(24, 16))],
              tasks=[("Finish JARVIS documentation", ist(25, 9), TaskPriority.HIGH, None)])
    try:
        text = r.say("Give me my morning briefing.")
        assert text.startswith("Good morning.") and "1 meeting today" in text and "task" in text
        more = r.say("Tell me more.")
        assert "due tomorrow" in more or "you marked it high priority" in more
        wf = r.op.last()
        assert {"calendar", "gmail", "tasks"} <= wf.scope
    finally:
        r.close()


def test_briefing_names_what_it_could_not_check(tmp_path):
    r = OpRig(tmp_path, tasks=[("Write notes", ist(24, 17), TaskPriority.MEDIUM, None)], with_gmail=False)
    r.h.hub.registry.set_enabled("gmail", False)
    try:
        text = r.say("Prepare my morning briefing")
        assert "Write notes" in text and "couldn't check" in text.lower() and "Gmail" in text
    finally:
        r.close()


# ---- 10. Cancellation -------------------------------------------------------------------------------------------------------------------

def test_cancel_stops_workflow_before_sensitive_step(tmp_path):
    r = OpRig(tmp_path, emails=[internship_email()], browser=True, threaded=True)
    try:
        first = r.say("Apply for the internship from my email.")
        wf = r.op.current()
        assert wf.status is WStatus.WAITING_FOR_CONFIRMATION and "go ahead" in first
        assert r.confirmations.has_pending("s1")
        assert r.say("Cancel the workflow.").startswith("Okay, I stopped")
        assert r.wait(lambda: wf.terminal)
        assert wf.status is WStatus.CANCELLED and not r.confirmations.has_pending("s1")
        assert "application submitted" not in (r.engine.status().get("title", "") + str(r.web.log if hasattr(r.web, "log") else ""))
        assert r.store.history()[0]["status"] == "CANCELLED"                       # history is preserved
        assert r.say("yes") is None                                                 # nothing left to confirm; a stray "yes" does nothing and never resumes it
        assert wf.status is WStatus.CANCELLED
    finally:
        r.close()


def test_briefing_with_only_local_sources_reached_is_still_a_briefing(tmp_path):
    """Nothing on the task list and Gmail/Calendar unreachable: say what was checked and what was not, instead of failing or inventing."""
    r = OpRig(tmp_path)
    try:
        r.h.hub.registry.set_enabled("gmail", False)
        r.h.hub.registry.set_enabled("calendar", False)
        text = r.say("Give me my morning briefing")
        wf = r.op.last()
        assert wf.status is WStatus.COMPLETED and "don't see anything" in text and "couldn't check" in text and "Gmail" in text and "your calendar" in text
    finally:
        r.close()


def test_briefing_with_nothing_reachable_fails_honestly(tmp_path):
    r = OpRig(tmp_path)
    try:
        r.h.hub.registry.set_enabled("gmail", False)
        r.h.hub.registry.set_enabled("calendar", False)
        r.op.router.ctx.tasks = None
        text = r.say("Give me my morning briefing")
        assert "nothing to put in a briefing" in text and r.op.last().status is WStatus.WAITING_FOR_DATA
    finally:
        r.close()
