"""Unit tests for permission models, risk, policy, action binding and tool security metadata."""

from datetime import datetime, timezone

import pytest

from agent.tools.base import Tool, ToolDescriptor
from backend.core.security import (
    PermissionPolicy,
    PermissionRequest,
    PermissionScope,
    PermissionStatus,
    PolicyDecision,
    ReasonCode,
    RiskLevel,
    ToolSecurityInfo,
    compute_action_digest,
)


def test_risk_levels_are_ordered():
    assert RiskLevel.LOW < RiskLevel.MEDIUM < RiskLevel.HIGH < RiskLevel.CRITICAL


def test_status_and_scope_values_are_typed_enums():
    assert {s.name for s in PermissionStatus} >= {"PENDING", "APPROVED", "DENIED", "EXPIRED", "CANCELLED"}
    assert {s.name for s in PermissionScope} == {"ONE_TIME", "SESSION", "PERSISTENT"}


def test_phase0_style_request_still_constructs_with_safe_defaults():
    request = PermissionRequest(tool_name="example", action="read")
    assert request.requested_by == "agent"
    assert request.status is PermissionStatus.PENDING
    assert request.scope is PermissionScope.ONE_TIME  # default is one-time, never persistent
    assert request.risk is RiskLevel.HIGH  # unknown risk is treated as high
    assert request.session_id is None and request.expires_at is None
    assert len(request.request_id) == 32 and request.created_at.tzinfo is not None


def test_request_ids_are_unique():
    assert PermissionRequest("t", "a").request_id != PermissionRequest("t", "a").request_id


def test_request_is_immutable():
    request = PermissionRequest("t", "a")
    with pytest.raises(AttributeError):
        request.status = PermissionStatus.APPROVED


def test_naive_timestamps_are_rejected():
    with pytest.raises(ValueError):
        PermissionRequest("t", "a", created_at=datetime(2030, 1, 1))
    with pytest.raises(ValueError):
        PermissionRequest("t", "a", expires_at=datetime(2030, 1, 1))
    PermissionRequest("t", "a", expires_at=datetime(2030, 1, 1, tzinfo=timezone.utc))


def info(**kw):
    return ToolSecurityInfo(**{"name": "t", **kw})


def test_policy_unknown_tool_is_denied():
    outcome = PermissionPolicy().evaluate(None, PermissionScope.ONE_TIME)
    assert (outcome.decision, outcome.code) == (PolicyDecision.DENY, ReasonCode.UNKNOWN_TOOL)


def test_policy_default_tool_requires_approval():
    assert PermissionPolicy().evaluate(info(), PermissionScope.ONE_TIME).decision is PolicyDecision.REQUIRE_APPROVAL


def test_policy_auto_allows_only_low_risk_tools_that_need_no_permission():
    policy = PermissionPolicy()
    assert policy.evaluate(info(requires_permission=False, risk=RiskLevel.LOW), PermissionScope.ONE_TIME).decision is PolicyDecision.ALLOW
    # A tool claiming "no permission needed" above LOW risk still needs a human.
    for risk in (RiskLevel.MEDIUM, RiskLevel.HIGH, RiskLevel.CRITICAL):
        outcome = policy.evaluate(info(requires_permission=False, risk=risk), PermissionScope.ONE_TIME)
        assert outcome.decision is PolicyDecision.REQUIRE_APPROVAL


def test_policy_scope_must_be_declared_by_the_tool():
    outcome = PermissionPolicy().evaluate(info(risk=RiskLevel.LOW), PermissionScope.SESSION)
    assert (outcome.decision, outcome.code) == (PolicyDecision.DENY, ReasonCode.SCOPE_NOT_ALLOWED)


def test_policy_reusable_scopes_are_denied_for_high_and_critical_risk():
    scopes = (PermissionScope.ONE_TIME, PermissionScope.SESSION, PermissionScope.PERSISTENT)
    for risk in (RiskLevel.HIGH, RiskLevel.CRITICAL):
        for scope in (PermissionScope.SESSION, PermissionScope.PERSISTENT):
            assert PermissionPolicy().evaluate(info(risk=risk, allowed_scopes=scopes), scope).decision is PolicyDecision.DENY
    assert PermissionPolicy().evaluate(info(risk=RiskLevel.MEDIUM, allowed_scopes=scopes), PermissionScope.SESSION).decision is PolicyDecision.REQUIRE_APPROVAL


def test_action_digest_is_deterministic_and_order_independent():
    a = compute_action_digest("email", "execute", {"to": "john", "body": "x"})
    b = compute_action_digest("email", "execute", {"body": "x", "to": "john"})
    assert a == b and len(a) == 64


@pytest.mark.parametrize(
    "other",
    [
        ("sms", "execute", {"to": "john", "body": "x"}),
        ("email", "delete", {"to": "john", "body": "x"}),
        ("email", "execute", {"to": "sarah", "body": "x"}),
        ("email", "execute", {"to": "john", "body": "y"}),
        ("email", "execute", {"to": "john"}),
    ],
)
def test_action_digest_changes_with_any_part_of_the_action(other):
    assert compute_action_digest("email", "execute", {"to": "john", "body": "x"}) != compute_action_digest(*other)


def test_action_digest_rejects_non_serializable_parameters():
    with pytest.raises(TypeError):
        compute_action_digest("t", "a", {"x": object()})
    with pytest.raises(ValueError):
        compute_action_digest("t", "a", {"x": float("nan")})


def test_tool_descriptor_and_tool_default_to_high_risk_one_time():
    descriptor = ToolDescriptor(name="x", description="d")
    assert descriptor.risk is RiskLevel.HIGH and descriptor.allowed_scopes == [PermissionScope.ONE_TIME]
    assert descriptor.security_info() == ToolSecurityInfo("x", True, RiskLevel.HIGH, (PermissionScope.ONE_TIME,))


def test_tool_declares_risk_and_scopes_for_future_tools():
    class ReadOnlyTool(Tool):
        name = "notes"
        description = "read"
        requires_permission = False
        risk = RiskLevel.LOW
        allowed_scopes = (PermissionScope.ONE_TIME, PermissionScope.SESSION)

        def run(self, **kwargs):
            return "ok"

    security = ReadOnlyTool().descriptor().security_info()
    assert (security.risk, security.requires_permission) == (RiskLevel.LOW, False)
    assert security.allowed_scopes == (PermissionScope.ONE_TIME, PermissionScope.SESSION)
