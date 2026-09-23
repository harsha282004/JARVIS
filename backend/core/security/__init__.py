"""Security layer: permission model, policy, audit trail and the PermissionManager.

Everything from the Phase 0 module is still importable from here
(`PermissionManager`, `PermissionRequest`, `PermissionDenied`).
See docs/security-and-permissions.md.
"""

from backend.core.security.audit import AuditLog, SecurityEvent, SecurityEventType
from backend.core.security.binding import compute_action_digest
from backend.core.security.manager import PermissionManager, check_authorization
from backend.core.security.models import (
    AuthorizationResult,
    PermissionDenied,
    PermissionRequest,
    PermissionScope,
    PermissionStateError,
    PermissionStatus,
    ReasonCode,
    RiskLevel,
    ToolSecurityInfo,
)
from backend.core.security.policy import PermissionPolicy, PolicyDecision

__all__ = [
    "AuditLog",
    "AuthorizationResult",
    "PermissionDenied",
    "PermissionManager",
    "PermissionPolicy",
    "PermissionRequest",
    "PermissionScope",
    "PermissionStateError",
    "PermissionStatus",
    "PolicyDecision",
    "ReasonCode",
    "RiskLevel",
    "SecurityEvent",
    "SecurityEventType",
    "ToolSecurityInfo",
    "check_authorization",
    "compute_action_digest",
]
