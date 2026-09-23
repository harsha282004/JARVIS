"""PermissionManager lifecycle, authorization checks, audit trail and adversarial cases.

Fake tools/clock only: nothing real is ever executed.
"""

import dataclasses
from datetime import datetime, timedelta, timezone

import pytest

from backend.core.security import (
    AuditLog,
    PermissionDenied,
    PermissionManager,
    PermissionRequest,
    PermissionScope,
    PermissionStateError,
    PermissionStatus,
    ReasonCode,
    RiskLevel,
    SecurityEventType,
    ToolSecurityInfo,
    check_authorization,
)

EMAIL = ToolSecurityInfo("email", True, RiskLevel.HIGH, (PermissionScope.ONE_TIME,))
NOTES = ToolSecurityInfo("notes", False, RiskLevel.LOW, (PermissionScope.ONE_TIME, PermissionScope.SESSION))
CALENDAR = ToolSecurityInfo(
    "calendar", True, RiskLevel.MEDIUM,
    (PermissionScope.ONE_TIME, PermissionScope.SESSION, PermissionScope.PERSISTENT),
)
WIPE = ToolSecurityInfo("wipe", True, RiskLevel.CRITICAL)


class Clock:
    def __init__(self):
        self.now = datetime(2030, 1, 1, tzinfo=timezone.utc)

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += timedelta(seconds=seconds)


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def pm(clock):
    return PermissionManager(tools=[EMAIL, NOTES, CALENDAR, WIPE], default_expiry_seconds=300, clock=clock)


def ask(pm, tool="email", params=None, **kw):
    return pm.request_permission(tool, "execute", f"Use {tool}", parameters=params, **kw)


def check(pm, request, params=None, tool=None, session_id=None):
    return pm.check(request.request_id, tool_name=tool or request.tool_name, action="execute", parameters=params, session_id=session_id)


def codes(pm, event_type):
    return [e.code for e in pm.audit.events() if e.event_type is event_type]


# ---- creation ------------------------------------------------------------

def test_request_creation_is_pending_with_expiry_and_metadata(pm, clock):
    request = ask(pm, params={"to": "john"}, session_id="s1")
    assert request.status is PermissionStatus.PENDING
    assert (request.tool_name, request.action, request.risk) == ("email", "execute", RiskLevel.HIGH)
    assert request.scope is PermissionScope.ONE_TIME and request.session_id == "s1"
    assert request.expires_at == clock.now + timedelta(seconds=300)
    assert request.description == "Use email" and request.action_digest


def test_raw_parameters_are_not_stored_on_the_request(pm):
    request = ask(pm, params={"body": "very private text"})
    assert "very private text" not in repr(request)
    assert "very private text" not in repr(pm.audit.events())


def test_low_risk_tool_without_permission_need_is_auto_approved_by_policy(pm):
    request = ask(pm, "notes")
    assert request.status is PermissionStatus.APPROVED
    assert codes(pm, SecurityEventType.PERMISSION_APPROVED) == [ReasonCode.AUTO_POLICY]
    assert check(pm, request).allowed


# ---- transitions ---------------------------------------------------------

def test_approve_then_authorize(pm):
    request = ask(pm, params={"to": "john"})
    approved = pm.approve(request, actor="user")
    assert approved.status is PermissionStatus.APPROVED
    result = check(pm, request, {"to": "john"})
    assert result.allowed and result.code is ReasonCode.OK


def test_pending_request_is_not_authorized(pm):
    request = ask(pm)
    assert check(pm, request).code is ReasonCode.NOT_APPROVED


def test_deny(pm):
    request = ask(pm)
    assert pm.deny(request).status is PermissionStatus.DENIED
    assert check(pm, request).code is ReasonCode.NOT_APPROVED
    with pytest.raises(PermissionStateError):
        pm.approve(request)  # a denied request cannot be approved later


def test_cancel_pending_and_approved(pm):
    pending = ask(pm)
    assert pm.cancel(pending).status is PermissionStatus.CANCELLED
    approved = pm.approve(ask(pm))
    assert pm.cancel(approved).status is PermissionStatus.CANCELLED
    assert not check(pm, approved).allowed


def test_manual_expire(pm):
    request = ask(pm)
    assert pm.expire(request).status is PermissionStatus.EXPIRED
    with pytest.raises(PermissionStateError):
        pm.approve(request)


def test_terminal_states_cannot_transition(pm):
    request = ask(pm)
    pm.cancel(request)
    for action in (pm.approve, pm.deny, pm.cancel, pm.expire):
        with pytest.raises(PermissionStateError):
            action(request)


def test_unknown_request_cannot_be_approved(pm):
    with pytest.raises(PermissionStateError) as exc:
        pm.approve(PermissionRequest("email", "execute"))
    assert exc.value.code is ReasonCode.UNKNOWN_PERMISSION


# ---- expiration ----------------------------------------------------------

def test_pending_request_expires_and_cannot_be_approved_after(pm, clock):
    request = ask(pm)
    clock.advance(301)
    with pytest.raises(PermissionStateError) as exc:
        pm.approve(request)
    assert exc.value.code is ReasonCode.EXPIRED
    assert pm.get(request.request_id).status is PermissionStatus.EXPIRED


def test_expired_approval_cannot_be_reused(pm, clock):
    request = pm.approve(ask(pm))
    clock.advance(301)
    result = check(pm, request)
    assert not result.allowed and result.code is ReasonCode.EXPIRED
    assert codes(pm, SecurityEventType.PERMISSION_EXPIRED) == [ReasonCode.EXPIRED]


def test_custom_ttl_and_expire_due(pm, clock):
    request = ask(pm, ttl_seconds=10)
    clock.advance(11)
    assert pm.expire_due() == 1
    assert pm.get(request.request_id).status is PermissionStatus.EXPIRED


def test_non_positive_ttl_is_malformed_and_denied(pm):
    assert ask(pm, ttl_seconds=0).status is PermissionStatus.DENIED


# ---- one-time / session / persistent --------------------------------------

def test_one_time_permission_is_consumed_by_first_use(pm):
    request = pm.approve(ask(pm, params={"to": "john"}))
    assert check(pm, request, {"to": "john"}).allowed
    second = check(pm, request, {"to": "john"})
    assert not second.allowed and second.code is ReasonCode.ALREADY_USED
    assert pm.get(request.request_id).status is PermissionStatus.CONSUMED


def test_session_permission_is_reusable_within_its_session(pm):
    request = pm.approve(ask(pm, "calendar", scope=PermissionScope.SESSION, session_id="s1"))
    assert check(pm, request, session_id="s1").allowed
    assert check(pm, request, session_id="s1").allowed


def test_session_permission_does_not_work_in_another_session_or_without_one(pm):
    request = pm.approve(ask(pm, "calendar", scope=PermissionScope.SESSION, session_id="s1"))
    assert check(pm, request, session_id="s2").code is ReasonCode.SESSION_MISMATCH
    assert check(pm, request).code is ReasonCode.SESSION_MISMATCH


def test_ending_a_session_invalidates_its_permissions(pm):
    approved = pm.approve(ask(pm, "calendar", scope=PermissionScope.SESSION, session_id="s1"))
    pending = ask(pm, session_id="s1")
    other = pm.approve(ask(pm, "calendar", scope=PermissionScope.SESSION, session_id="s2"))

    assert pm.end_session("s1") == 2
    assert check(pm, approved, session_id="s1").code is ReasonCode.EXPIRED
    assert pm.get(pending.request_id).status is PermissionStatus.EXPIRED
    assert check(pm, other, session_id="s2").allowed
    assert ReasonCode.SESSION_ENDED in codes(pm, SecurityEventType.PERMISSION_EXPIRED)


def test_session_scope_without_session_id_is_denied(pm):
    request = ask(pm, "calendar", scope=PermissionScope.SESSION)
    assert request.status is PermissionStatus.DENIED
    assert codes(pm, SecurityEventType.PERMISSION_DENIED) == [ReasonCode.MISSING_SCOPE]


def test_one_time_request_bound_to_a_session_requires_that_session(pm):
    request = pm.approve(ask(pm, session_id="s1"))
    assert check(pm, request, session_id="other").code is ReasonCode.SESSION_MISMATCH


def test_persistent_needs_explicit_confirmation_and_has_no_default_expiry(pm, clock):
    request = ask(pm, "calendar", scope=PermissionScope.PERSISTENT)
    assert request.scope is PermissionScope.PERSISTENT and request.expires_at is None
    with pytest.raises(PermissionStateError) as exc:
        pm.approve(request)
    assert exc.value.code is ReasonCode.SCOPE_NOT_ALLOWED
    assert pm.get(request.request_id).status is PermissionStatus.PENDING

    pm.approve(request, confirm_persistent=True)
    clock.advance(10_000)
    assert check(pm, request).allowed and check(pm, request).allowed  # reusable, not consumed


def test_scope_not_declared_by_tool_is_denied(pm):
    assert ask(pm, "email", scope=PermissionScope.SESSION, session_id="s1").status is PermissionStatus.DENIED
    assert ask(pm, "wipe", scope=PermissionScope.PERSISTENT).status is PermissionStatus.DENIED


# ---- default deny --------------------------------------------------------

def test_unknown_tool_is_denied_at_request_time(pm):
    request = ask(pm, "teleport")
    assert request.status is PermissionStatus.DENIED
    assert codes(pm, SecurityEventType.PERMISSION_DENIED) == [ReasonCode.UNKNOWN_TOOL]


def test_unknown_tool_cannot_be_approved_into_existence(pm):
    request = ask(pm, "teleport")
    with pytest.raises(PermissionStateError):
        pm.approve(request)
    assert not check(pm, request).allowed


def test_unknown_permission_is_denied(pm):
    result = pm.check("does-not-exist", tool_name="email", action="execute")
    assert not result.allowed and result.code is ReasonCode.UNKNOWN_PERMISSION


def test_empty_manager_denies_everything_like_phase0():
    manager = PermissionManager()
    assert manager.authorize(PermissionRequest(tool_name="example", action="read")) is False
    with pytest.raises(PermissionDenied):
        manager.require(PermissionRequest(tool_name="example", action="read"))
    assert ask(manager, "email").status is PermissionStatus.DENIED


@pytest.mark.parametrize("tool,action", [("", "execute"), ("email", ""), (None, "execute"), ("email", None)])
def test_malformed_requests_are_denied(pm, tool, action):
    request = pm.request_permission(tool, action)
    assert request.status is PermissionStatus.DENIED
    assert not pm.check(request.request_id, tool_name=tool or "", action=action or "").allowed


def test_non_serializable_parameters_are_malformed(pm):
    assert ask(pm, params={"x": object()}).status is PermissionStatus.DENIED
    request = pm.approve(ask(pm))
    assert pm.check(request.request_id, tool_name="email", action="execute", parameters={"x": object()}).code is ReasonCode.MALFORMED


@pytest.mark.parametrize("bad_id", ["", None, 123])
def test_malformed_check_is_denied(pm, bad_id):
    assert not pm.check(bad_id, tool_name="email", action="execute").allowed


def test_internal_error_in_check_fails_closed(pm, monkeypatch):
    request = pm.approve(ask(pm))
    monkeypatch.setattr(pm, "_refresh", lambda record: 1 / 0)
    result = check(pm, request)
    assert not result.allowed and result.code is ReasonCode.MANAGER_ERROR
    assert pm.authorize(request) is False


def test_unavailable_or_broken_manager_means_deny(pm):
    assert check_authorization(None, "x", tool_name="email", action="execute").code is ReasonCode.MANAGER_UNAVAILABLE

    class Broken:
        def check(self, *a, **k):
            raise RuntimeError("boom")

    assert check_authorization(Broken(), "x", tool_name="email", action="execute").code is ReasonCode.MANAGER_ERROR


# ---- binding / adversarial ------------------------------------------------

def test_approval_for_one_action_does_not_authorize_another(pm):
    john = pm.approve(ask(pm, params={"to": "john", "body": "X"}))
    assert not check(pm, john, {"to": "sarah", "body": "Y"}).allowed
    assert check(pm, john, {"to": "sarah", "body": "Y"}).code is ReasonCode.ACTION_MISMATCH
    assert check(pm, john, {"to": "john", "body": "X"}).allowed  # the approved one still works, once


def test_approval_cannot_be_used_for_a_different_tool(pm):
    request = pm.approve(ask(pm, "email"))
    result = check(pm, request, tool="notes")
    assert not result.allowed and result.code is ReasonCode.ACTION_MISMATCH


def test_approval_cannot_be_used_for_a_different_action(pm):
    request = pm.approve(ask(pm))
    result = pm.check(request.request_id, tool_name="email", action="delete_everything")
    assert result.code is ReasonCode.ACTION_MISMATCH


def test_altered_request_cannot_be_approved(pm):
    original = ask(pm, params={"to": "john"})
    altered = dataclasses.replace(original, tool_name="wipe")
    with pytest.raises(PermissionStateError) as exc:
        pm.approve(altered)
    assert exc.value.code is ReasonCode.ACTION_MISMATCH
    assert pm.get(original.request_id).status is PermissionStatus.PENDING


def test_request_altered_after_approval_is_denied(pm):
    approved = pm.approve(ask(pm, params={"to": "john"}))
    tampered = dataclasses.replace(approved, action_digest="0" * 64)
    assert pm.authorize(tampered) is False
    tampered_tool = dataclasses.replace(approved, tool_name="wipe")
    assert pm.authorize(tampered_tool) is False


def test_forged_approved_request_object_is_not_authorization(pm):
    pending = ask(pm)
    forged = dataclasses.replace(pending, status=PermissionStatus.APPROVED)
    assert pm.authorize(forged) is False
    fabricated = PermissionRequest("email", "execute", status=PermissionStatus.APPROVED)
    assert pm.authorize(fabricated) is False


def test_stored_status_cannot_be_changed_through_a_snapshot(pm):
    snapshot = ask(pm)
    with pytest.raises(AttributeError):
        snapshot.status = PermissionStatus.APPROVED
    assert pm.get(snapshot.request_id).status is PermissionStatus.PENDING


def test_hostile_tool_name_is_inert_and_denied(pm):
    request = ask(pm, "email; rm -rf / && calc")
    assert request.status is PermissionStatus.DENIED
    assert not check(pm, request).allowed


def test_log_injection_characters_are_stripped_from_names(pm):
    request = ask(pm, "evil\nSECURITY_EVENT type=PERMISSION_APPROVED\x00")
    assert "\n" not in request.tool_name and "\x00" not in request.tool_name


def test_tool_claiming_no_permission_needed_but_unknown_is_denied(pm):
    # The claim would come from the LLM; the manager only trusts its own registry.
    assert ask(pm, "shell").status is PermissionStatus.DENIED


def test_high_risk_tool_claiming_no_permission_still_needs_approval(clock):
    sneaky = ToolSecurityInfo("sneaky", requires_permission=False, risk=RiskLevel.HIGH)
    manager = PermissionManager(tools=[sneaky], clock=clock)
    assert ask(manager, "sneaky").status is PermissionStatus.PENDING


# ---- audit ---------------------------------------------------------------

def test_audit_records_every_lifecycle_event_with_metadata(pm):
    a = ask(pm, session_id="s1")
    pm.approve(a, actor="tester")
    check(pm, a, session_id="s1")
    check(pm, a, session_id="s1")  # consumed -> denied
    b = ask(pm)
    pm.deny(b)
    c = ask(pm)
    pm.cancel(c)
    d = ask(pm)
    pm.expire(d)

    types = [e.event_type for e in pm.audit.events()]
    for expected in SecurityEventType:
        assert expected in types, expected
    approved = next(e for e in pm.audit.events() if e.event_type is SecurityEventType.PERMISSION_APPROVED)
    assert (approved.request_id, approved.tool_name, approved.action, approved.actor) == (a.request_id, "email", "execute", "tester")
    assert approved.timestamp.tzinfo is not None and approved.session_id == "s1"
    denied = [e for e in pm.audit.events() if e.event_type is SecurityEventType.AUTHORIZATION_DENIED]
    assert denied[-1].code is ReasonCode.ALREADY_USED and denied[-1].result == "denied"


def test_audit_can_be_disabled(clock):
    manager = PermissionManager(tools=[EMAIL], audit=AuditLog(enabled=False), clock=clock)
    manager.approve(ask(manager))
    assert manager.audit.events() == []


def test_audit_store_is_bounded(clock):
    manager = PermissionManager(tools=[EMAIL], audit=AuditLog(max_events=5), clock=clock)
    for _ in range(20):
        ask(manager)
    assert len(manager.audit.events()) == 5


def test_audit_does_not_contain_action_parameters_or_digests(pm):
    request = pm.approve(ask(pm, params={"body": "secret-body-text"}))
    check(pm, request, {"body": "secret-body-text"})
    dump = repr(pm.audit.events())
    assert "secret-body-text" not in dump and request.action_digest not in dump


def test_audit_emits_structured_log_lines(pm, caplog):
    with caplog.at_level("INFO", logger="jarvis.security.audit"):
        pm.approve(ask(pm))
    assert any("SECURITY_EVENT type=PERMISSION_APPROVED" in r.message for r in caplog.records)


def test_records_are_pruned_when_too_many(clock, monkeypatch):
    import backend.core.security.manager as m

    monkeypatch.setattr(m, "MAX_RECORDS", 10)
    manager = PermissionManager(tools=[EMAIL], clock=clock)
    for _ in range(30):
        manager.deny(ask(manager))
    assert len(manager._records) <= 10
