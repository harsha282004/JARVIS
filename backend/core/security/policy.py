"""Centralized permission policy. Decisions come only from this module and the
tool's registered security info, never from LLM output.

    unknown tool            -> DENY
    scope not allowed       -> DENY
    reusable scope, risk>MEDIUM -> DENY (high-impact actions are one-time only)
    LOW risk and tool says no permission needed -> ALLOW (auto)
    everything else         -> REQUIRE_APPROVAL
"""

from dataclasses import dataclass
from enum import StrEnum

from backend.core.security.models import PermissionScope, ReasonCode, RiskLevel, ToolSecurityInfo


class PolicyDecision(StrEnum):
    ALLOW = "allow"
    REQUIRE_APPROVAL = "require_approval"
    DENY = "deny"


@dataclass(frozen=True)
class PolicyOutcome:
    decision: PolicyDecision
    code: ReasonCode


MAX_REUSABLE_RISK = RiskLevel.MEDIUM


class PermissionPolicy:
    def evaluate(self, tool: ToolSecurityInfo | None, scope: PermissionScope) -> PolicyOutcome:
        if tool is None:
            return PolicyOutcome(PolicyDecision.DENY, ReasonCode.UNKNOWN_TOOL)
        if scope not in tool.allowed_scopes:
            return PolicyOutcome(PolicyDecision.DENY, ReasonCode.SCOPE_NOT_ALLOWED)
        if scope is not PermissionScope.ONE_TIME and tool.risk > MAX_REUSABLE_RISK:
            return PolicyOutcome(PolicyDecision.DENY, ReasonCode.SCOPE_NOT_ALLOWED)
        if not tool.requires_permission and tool.risk is RiskLevel.LOW:
            return PolicyOutcome(PolicyDecision.ALLOW, ReasonCode.AUTO_POLICY)
        # Risk above LOW always needs a human, whatever the tool claims.
        return PolicyOutcome(PolicyDecision.REQUIRE_APPROVAL, ReasonCode.REQUEST_CREATED)
