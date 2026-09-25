"""End-to-end scenarios and failure injection for the Personal Operator. Every failure must be reported honestly and leave no half-made or duplicated side effect."""

import pytest

from tests.calendar_helpers import cal_event
from tests.intelligence_helpers import ist
from tests.workflow_helpers import HEDGED, INJECTION, OpRig, internship_email
from integrations.hub.models import ErrorKind, ToolResult
from workflows.models import FailureKind, SStatus, WStatus


class Crash(BaseException):
    """A process death: not an Exception, so nothing in the runner may swallow it."""


def make(tmp_path, **kw):
    return OpRig(tmp_path, **kw)


def fail_tool(rig, name, kind, message="It failed.", times=None):
    orig = rig.h.hub.tools.call
    calls = {"n": 0}

    def wrapped(tool, args=None):
        if tool == name and (times is None or calls["n"] < times):
            calls["n"] += 1
            return ToolResult.fail("x", kind, message)
        return orig(tool, args)

    rig.h.hub.tools.call = wrapped
    return calls


# ============================================ end-to-end scenarios ============================================================================

def test_scenario_deadline_reminder_is_never_duplicated(tmp_path):
    r = make(tmp_path, emails=[internship_email()])
    try:
        r.say("Find the internship email and remind me two days before the deadline.")
        again = r.say("Find the internship email and remind me two days before the deadline.")
        assert len(r.reminders()) == 1
        assert "already exists" in again
    finally:
        r.close()


def test_scenario_conflict_between_email_and_calendar_is_reported_not_resolved(tmp_path):
    r = make(tmp_path, emails=[internship_email()], calendar_events=[cal_event("c1", "Internship application deadline", ist(17, 9, month=10), ist(17, 10, month=10))])
    try:
        text = r.say("Find the internship email and create a task for the deadline.")
        wf = r.op.last()
        assert wf.status is WStatus.WAITING_FOR_USER
        assert "conflicting dates" in text and "October 15" in text and "October 17" in text
        assert not r.tasks()                                                     # neither date was silently chosen
        assert [e.summary for e in r.base.calendar_client.events.values()] == ["Internship application deadline"]
        done = r.say("the email")
        assert "Created the task" in done and wf.status is WStatus.COMPLETED
        (task,) = r.tasks()
        assert task.due_at.astimezone(r.h.hub.zone).date().isoformat() == "2026-10-15"
        assert task.metadata["provenance"]["source_id"] == "m1"
        (event,) = r.base.calendar_client.events.values()
        assert event.start.astimezone(r.h.hub.zone).day == 17                    # the calendar entry was never modified
        assert not [c for c in r.base.calendar_client.calls if c[0] in ("create_event", "update_event", "delete_event", "patch_event")]
    finally:
        r.close()


def test_scenario_conflict_choose_calendar_date(tmp_path):
    r = make(tmp_path, emails=[internship_email()], calendar_events=[cal_event("c1", "Internship application deadline", ist(17, 9, month=10), ist(17, 10, month=10))])
    try:
        r.say("Find the internship email and create a task for the deadline.")
        r.say("the calendar")
        (task,) = r.tasks()
        assert task.due_at.astimezone(r.h.hub.zone).date().isoformat() == "2026-10-17"
    finally:
        r.close()


def test_scenario_github_personal_identity_required(tmp_path):
    r = make(tmp_path, github=False)
    try:
        text = r.say("Find my GitHub activity from this week and add anything important to my task list.")
        assert "can't do that yet" in text and "GitHub" in text
        assert not r.tasks()
        assert r.h.github.calls == []                                            # nothing public was searched and presented as "mine"
    finally:
        r.close()


def test_scenario_github_identity_lookup_failure_is_not_presented_as_personal(tmp_path):
    r = make(tmp_path)
    try:
        r.h.github.status_override = 401
        text = r.say("Find my GitHub activity from this week and add anything important to my task list.")
        wf = r.op.last()
        assert wf.status in (WStatus.WAITING_FOR_DATA, WStatus.FAILED)
        assert "won't call any results yours" in text or "GitHub" in text
        assert not r.tasks()
    finally:
        r.close()


def test_scenario_sensitive_action_stops_before_submission_and_asks(tmp_path):
    r = make(tmp_path, emails=[internship_email()], browser=True, threaded=True)
    try:
        ask = r.say("Apply for the internship from my email.")
        wf = r.op.current()
        assert wf.status is WStatus.WAITING_FOR_CONFIRMATION and "jobs.example.com" in ask and "can't be undone" in ask
        click = next(s for s in wf.steps if s.tool == "click_element")
        assert click.risk.needs_confirmation and click.status is SStatus.PENDING
        assert r.confirmations.has_pending("s1")
        assert r.say("yes") == "Okay, continuing."
        assert r.wait(lambda: wf.terminal)
        assert wf.status is WStatus.COMPLETED and click.status is SStatus.DONE
        assert "Done" in wf.result.summary
    finally:
        r.close()


def test_scenario_declined_confirmation_skips_the_submission(tmp_path):
    r = make(tmp_path, emails=[internship_email()], browser=True, threaded=True)
    try:
        r.say("Apply for the internship from my email.")
        wf = r.op.current()
        assert r.say("no") == "Okay, I won't do that."
        assert r.wait(lambda: wf.terminal)
        click = next(s for s in wf.steps if s.tool == "click_element")
        assert click.status is SStatus.SKIPPED and "you chose not to" in click.note
        assert not r.store.effect_lookup(f"browser:{wf.workflow_id}:{click.step_id}")
    finally:
        r.close()


def test_scenario_restart_after_crash_never_repeats_a_side_effect(tmp_path):
    r = make(tmp_path, emails=[internship_email()])
    real_create = r.base.tasks.create_task

    def create_then_die(*a, **k):
        task = real_create(*a, **k)
        raise Crash()                                                            # the process dies right after the task was written

    r.base.tasks.create_task = create_then_die
    with pytest.raises(Crash):
        r.op.start("Find the internship email, identify the deadline, create a task for it, and remind me two days before.", "s1")
    r.base.tasks.create_task = real_create
    assert len(r.tasks()) == 1 and not r.reminders()

    op2 = r.new_operator()
    assert len(op2.recover()) == 1
    (wf,) = op2.active()
    assert wf.status is WStatus.PAUSED and wf.recovered                          # nothing resumed by itself
    assert len(r.tasks()) == 1 and not r.reminders()
    assert op2.resume(wf.workflow_id) in ("Resuming.", "Okay, trying again.")
    assert wf.status is WStatus.COMPLETED, [(s.tool, s.status, s.note) for s in wf.steps]
    assert len(r.tasks()) == 1                                                   # the task written before the crash was adopted, not repeated
    assert len(r.reminders()) == 1
    r.close()


def test_scenario_restart_between_task_and_reminder(tmp_path):
    r = make(tmp_path, emails=[internship_email()])
    real = r.base.reminders.create_reminder

    def die(*a, **k):
        raise Crash()

    r.base.reminders.create_reminder = die
    with pytest.raises(Crash):
        r.op.start("Find the internship email, create a task for it and remind me two days before.", "s1")
    r.base.reminders.create_reminder = real
    op2 = r.new_operator()
    op2.recover()
    (wf,) = op2.active()
    assert [s.status for s in wf.steps if s.tool == "task_create"] == [SStatus.DONE]
    op2.resume(wf.workflow_id)
    assert wf.status is WStatus.COMPLETED and len(r.tasks()) == 1 and len(r.reminders()) == 1
    r.close()


def test_scenario_recovered_workflow_can_be_cancelled_and_never_resumes(tmp_path):
    r = make(tmp_path, emails=[internship_email()])
    real = r.base.reminders.create_reminder
    r.base.reminders.create_reminder = lambda *a, **k: (_ for _ in ()).throw(Crash())
    with pytest.raises(Crash):
        r.op.start("Find the internship email, create a task for it and remind me two days before.", "s1")
    r.base.reminders.create_reminder = real
    op2 = r.new_operator()
    op2.recover()
    (wf,) = op2.active()
    assert op2.cancel(wf.workflow_id).startswith("Okay, I stopped")
    assert wf.status is WStatus.CANCELLED
    assert not r.new_operator().recover()                                        # a cancelled workflow is not recovered again
    assert not r.reminders()
    r.close()


def test_scenario_prompt_injection_in_email_causes_no_action(tmp_path):
    r = make(tmp_path, emails=[internship_email(body=INJECTION)])
    try:
        text = r.say("Find the internship email and create a task for the deadline.")
        assert not r.tasks() and not r.reminders()
        assert "trying to give me instructions" in text or "can't rely" in text
        wf = r.op.last()
        assert wf.status is WStatus.FAILED and wf.failure_kind in (FailureKind.SECURITY_BLOCK, FailureKind.AMBIGUOUS)
        # no outbound path exists at all
        assert not hasattr(r.h.base.gmail_client, "sent") or not r.h.base.gmail_client.sent
    finally:
        r.close()


# ============================================ failure injection ===============================================================================

def test_gmail_switched_off(tmp_path):
    r = make(tmp_path, emails=[internship_email()])
    r.h.hub.registry.set_enabled("gmail", False)
    text = r.say("Find the internship email and create a task for the deadline.")
    assert "can't do that yet" in text and "Gmail" in text and not r.tasks()
    r.close()


def test_gmail_auth_expired_waits_and_changes_nothing(tmp_path):
    r = make(tmp_path, emails=[internship_email()])
    fail_tool(r, "search_email", ErrorKind.AUTH_ERROR, "Gmail sign-in expired. Please reconnect Gmail.")
    text = r.say("Find the internship email and create a task for the deadline.")
    wf = r.op.last()
    assert wf.status is WStatus.WAITING_FOR_DATA and wf.failure_kind is FailureKind.AUTHENTICATION
    assert "reconnect Gmail" in text and "Nothing was changed" in text and not r.tasks()
    r.close()


def test_network_failure_retries_reads_only_then_waits(tmp_path):
    r = make(tmp_path, emails=[internship_email()])
    calls = fail_tool(r, "search_email", ErrorKind.NETWORK_ERROR, "I couldn't reach Gmail.")
    r.say("Find the internship email and create a task for the deadline.")
    wf = r.op.last()
    assert calls["n"] == 3 and wf.retries == 2                                   # one attempt + two retries of a READ
    assert wf.status is WStatus.WAITING_FOR_DATA and wf.failure_kind is FailureKind.TEMPORARY
    r.close()


def test_transient_failure_recovers_on_retry(tmp_path):
    r = make(tmp_path, emails=[internship_email()])
    fail_tool(r, "search_email", ErrorKind.NETWORK_ERROR, "blip", times=1)
    text = r.say("Find the internship email and create a task for the deadline.")
    assert "Created the task" in text and len(r.tasks()) == 1
    r.close()


def test_try_again_after_waiting_for_data(tmp_path):
    r = make(tmp_path, emails=[internship_email()])
    calls = fail_tool(r, "search_email", ErrorKind.AUTH_ERROR, "Gmail sign-in expired.", times=1)
    r.say("Find the internship email and create a task for the deadline.")
    assert r.op.last().status is WStatus.WAITING_FOR_DATA
    assert r.say("try again") in ("Okay, trying again.", "Resuming.")
    wf = r.op.last()
    assert wf.status is WStatus.COMPLETED and len(r.tasks()) == 1 and calls["n"] == 1
    r.close()


def test_permission_error_is_a_failure_not_a_retry(tmp_path):
    r = make(tmp_path, emails=[internship_email()])
    calls = fail_tool(r, "search_email", ErrorKind.PERMISSION_ERROR, "Gmail search isn't allowed.")
    r.say("Find the internship email and create a task for the deadline.")
    wf = r.op.last()
    assert wf.status is WStatus.FAILED and wf.failure_kind is FailureKind.PERMISSION and calls["n"] == 1
    r.close()


def test_calendar_unreachable_does_not_block_the_task_but_is_reported(tmp_path):
    r = make(tmp_path, emails=[internship_email()])
    fail_tool(r, "search_calendar", ErrorKind.NETWORK_ERROR, "I couldn't reach your calendar.")
    r.say("Find the internship email and create a task for the deadline.")
    wf = r.op.last()
    assert wf.status is WStatus.COMPLETED and len(r.tasks()) == 1
    assert any("calendar" in w.lower() for w in wf.warnings)
    r.close()


def test_calendar_switched_off_is_disclosed(tmp_path):
    r = make(tmp_path, emails=[internship_email()])
    r.h.hub.registry.set_enabled("calendar", False)
    r.say("Find the internship email and create a task for the deadline.")
    wf = r.op.last()
    assert len(r.tasks()) == 1 and any("your calendar" in w for w in wf.warnings)
    assert "calendar" not in wf.scope
    r.close()


def test_browser_unavailable(tmp_path):
    r = make(tmp_path, emails=[internship_email()], browser=False)
    r.tool_router.browser = None
    text = r.say("Open the application link from the internship email.")
    assert "can't do that yet" in text
    r.close()


def test_missing_deadline_is_reported(tmp_path):
    r = make(tmp_path, emails=[internship_email(body="Thank you for applying. We will be in touch soon.")])
    text = r.say("Find the internship email and create a task for the deadline.")
    assert "didn't find a date" in text and not r.tasks()
    assert r.op.last().failure_kind is FailureKind.DATA_MISSING
    r.close()


def test_hedged_deadline_is_not_turned_into_a_fact(tmp_path):
    r = make(tmp_path, emails=[internship_email(body=HEDGED)])
    text = r.say("Find the internship email and create a task for the deadline.")
    assert not r.tasks() and not r.reminders()
    assert "possible deadline" in text or "didn't find a date" in text
    r.close()


def test_duplicate_task_is_adopted_not_created_twice(tmp_path):
    r = make(tmp_path, emails=[internship_email()])
    r.base.tasks.create_task("Submit your internship application", due_at=ist(15, 23, 59, month=10))
    text = r.say("Find the internship email and create a task for the deadline.")
    assert len(r.tasks()) == 1 and "already exists" in text
    r.close()


def test_reminder_time_already_passed_is_reported_but_task_is_still_made(tmp_path):
    body = "Please submit your internship application by September 26, 2026."
    r = make(tmp_path, emails=[internship_email(body=body)])
    text = r.say("Find the internship email, create a task for it and remind me three days before.")
    wf = r.op.last()
    assert len(r.tasks()) == 1 and not r.reminders()
    assert "already passed" in text and "Created the task" in text
    assert wf.status is WStatus.FAILED
    assert not text.startswith("Done")
    r.close()


def test_llm_unavailable_for_a_compound_request(tmp_path):
    r = make(tmp_path, emails=[internship_email()])
    text = r.say("Check my email and my calendar and my GitHub, then summarize everything into a document")
    assert text == "I can handle that workflow, but the conversational model is currently unavailable."
    r.close()


def test_confirmation_timeout_does_not_act(tmp_path):
    r = make(tmp_path, emails=[internship_email()], browser=True, threaded=True, confirmation_timeout_s=0.4)
    try:
        r.say("Apply for the internship from my email.")
        wf = r.op.current() or r.op.last()
        assert r.wait(lambda: wf.terminal, 8)
        assert wf.status is WStatus.FAILED and "didn't get your confirmation in time" in wf.result.summary
        assert not any(s.tool == "click_element" and s.status is SStatus.DONE for s in wf.steps)
    finally:
        r.close()


def test_no_confirmation_channel_means_consequential_steps_never_run(tmp_path):
    r = make(tmp_path, emails=[internship_email()], browser=True, threaded=True)
    r.op._confirmations = None
    try:
        r.say("Apply for the internship from my email.")
        wf = r.op.last()
        assert r.wait(lambda: wf.terminal)
        click = next(s for s in wf.steps if s.tool == "click_element")
        assert click.status is SStatus.SKIPPED
    finally:
        r.close()


def test_stop_by_voice_control_cancels_a_workflow(tmp_path):
    r = make(tmp_path, emails=[internship_email()], browser=True, threaded=True)
    try:
        r.say("Apply for the internship from my email.")
        wf = r.op.current()
        assert r.intel.task_active() and r.intel.cancel_task()
        assert r.wait(lambda: wf.terminal) and wf.status is WStatus.CANCELLED
    finally:
        r.close()


@pytest.mark.parametrize("phrase", ["Stop.", "Cancel this.", "Never mind.", "Cancel the workflow."])
def test_cancellation_phrases(tmp_path, phrase):
    r = make(tmp_path, emails=[internship_email()], browser=True, threaded=True)
    try:
        r.say("Apply for the internship from my email.")
        wf = r.op.current()
        assert r.say(phrase).startswith("Okay, I stopped")
        assert r.wait(lambda: wf.terminal) and wf.status is WStatus.CANCELLED
        assert not r.confirmations.has_pending("s1")
    finally:
        r.close()


def test_pause_and_resume(tmp_path):
    r = make(tmp_path, emails=[internship_email()], browser=True, threaded=True)
    try:
        r.say("Apply for the internship from my email.")
        wf = r.op.current()
        runner = r.op._runners[wf.workflow_id]
        assert wf.status is WStatus.WAITING_FOR_CONFIRMATION
        r.op.cancel()
        assert r.wait(lambda: wf.terminal)
        assert r.op.pause() == "There's no running workflow to pause."
    finally:
        r.close()
