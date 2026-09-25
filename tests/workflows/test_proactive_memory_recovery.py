"""Proactive suggestions, personal-memory context, checkpoint hardening and history."""

import json

from agent.memory.models import MemoryType
from backend.core.events import Event, SystemEvent
from tests.workflow_helpers import OpRig, internship_email
from workflows.models import SStatus


def deadline_event(source="gmail"):
    return Event(SystemEvent.DEADLINE_DETECTED, {"source": source, "source_id": "m1", "kind": "deadline"})


def test_proactive_event_suggests_but_never_writes(tmp_path):
    r = OpRig(tmp_path, emails=[internship_email()])
    try:
        r.op.on_deadline_event(deadline_event())
        assert not r.tasks() and not r.reminders()
        said = [t for t, _ in r.announced]
        assert len(said) == 1 and "I noticed a deadline in an email" in said[0] and "October 15" in said[0] and "turn that into a task" in said[0]
        wf = r.op.last()
        assert wf.requested_by == "proactive" and next(s for s in wf.steps if s.tool == "task_create_batch").status is SStatus.SKIPPED
        # the user accepts: the normal workflow (from the remembered fact) does the write
        r.say("Turn that into a task")
        assert len(r.tasks()) == 1 and r.tasks()[0].metadata["provenance"]["source_id"] == "m1"
    finally:
        r.close()


def test_proactive_suggestions_are_throttled_deduplicated_and_gmail_only(tmp_path):
    r = OpRig(tmp_path, emails=[internship_email()])
    try:
        r.op.on_deadline_event(deadline_event("calendar"))
        assert not r.announced and r.op.last() is None
        r.op.on_deadline_event(deadline_event())
        r.op.on_deadline_event(deadline_event())                                    # within the throttle window
        r.op._last_proactive = -1e9
        r.op.on_deadline_event(deadline_event())                                    # same deadline: announced once only
        assert len(r.announced) == 1
    finally:
        r.close()


def test_proactive_can_be_switched_off(tmp_path):
    from workflows.runner import OperatorConfig

    r = OpRig(tmp_path, emails=[internship_email()], cfg=OperatorConfig(proactive=False))
    try:
        r.op.on_deadline_event(deadline_event())
        assert r.op.suggest("Find the internship email and create a task for the deadline.") is None
        assert not r.announced and not r.tasks()
    finally:
        r.close()


def test_proactive_never_runs_over_the_users_own_workflow(tmp_path):
    r = OpRig(tmp_path, emails=[internship_email()], browser=True, threaded=True)
    try:
        r.say("Apply for the internship from my email.")
        before = len(r.op.active())
        r.op.on_deadline_event(deadline_event())
        assert len(r.op.active()) == before and not r.announced or all("noticed" not in t for t, _ in r.announced)
    finally:
        r.close()


def test_memory_is_context_only_in_the_confirmation_preview(tmp_path):
    r = OpRig(tmp_path, emails=[internship_email()], browser=True, threaded=True, memories=[("User prefers to apply using the internship portal with the résumé v3.", MemoryType.CONTEXT)])
    try:
        ask = r.say("Apply for the internship from my email.")
        assert "From what you've told me before" in ask and "résumé v3" in ask
        wf = r.op.current()
        assert next(s for s in wf.steps if s.tool == "memory_context").status is SStatus.DONE
        assert next(s for s in wf.steps if s.tool == "click_element").status is SStatus.PENDING     # memory never lowers the confirmation requirement
    finally:
        r.close()


def test_memory_never_overrides_a_current_source(tmp_path):
    """Memory says the deadline is Oct 20; the email (a current source) says Oct 15: the task follows the email."""
    r = OpRig(tmp_path, emails=[internship_email()], memories=[("The internship deadline is October 20.", MemoryType.CONTEXT)])
    try:
        r.say("Find the internship email and create a task for the deadline.")
        (task,) = r.tasks()
        assert task.due_at.astimezone(r.h.hub.zone).date().isoformat() == "2026-10-15"
    finally:
        r.close()


def test_edited_checkpoint_is_validated_and_recovery_skips_what_it_cannot_rebuild(tmp_path):
    r = OpRig(tmp_path, emails=[internship_email()])
    try:
        path = r.state / "workflow_checkpoints.json"
        cp = {"workflow_id": "evil1", "goal": "x", "template": "no_such_template", "status": "RUNNING", "session_id": "s1", "requested_by": "user", "risk": "READ_ONLY", "scope": ["gmail"],
              "params": {}, "steps": [], "updated_at": 1.0, "created_at": 1.0, "completed_steps": [], "current_step": None, "pending_step": None, "facts": []}
        bad = dict(cp, workflow_id="evil2", template="deadline_to_task", params={"topic": ["not", "a", "string"]})
        path.write_text(json.dumps({"evil1": cp, "evil2": bad}), encoding="utf-8")
        op2 = r.new_operator()
        assert op2.recover() == []                                                     # neither is loaded, nothing runs, nothing crashes
        assert all(v["status"] in ("CANCELLED", "RUNNING") for v in r.store.all_checkpoints().values())
        assert not r.tasks()
    finally:
        r.close()


def test_active_workflow_cap_and_history_size(tmp_path):
    from workflows.runner import OperatorConfig

    r = OpRig(tmp_path, emails=[internship_email()], cfg=OperatorConfig(history_size=3))
    try:
        for i in range(6):
            r.say("Give me my morning briefing")
        assert len(r.store.history(50)) >= 3 and len(r.op.snapshot()["history"]) == 3
    finally:
        r.close()


def test_resume_and_pause_are_honest_when_idle(tmp_path):
    r = OpRig(tmp_path, emails=[internship_email()])
    try:
        assert r.op.resume() == "There's no paused workflow." and r.op.pause() == "There's no running workflow to pause."
        assert r.op.cancel() == "There's nothing running to stop."
        assert r.say("Stop.") is None                                                  # nothing of ours is running: another router may handle "stop"
        assert r.say("what workflows are unfinished") == "There are no unfinished workflows."
    finally:
        r.close()
