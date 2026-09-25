"""Approval boundaries: which class of action may run automatically and which needs the user, decided in one place.

    READ / ANALYZE / PLAN     automatic (they change nothing outside JARVIS)
    CREATE_LOCAL              automatic only when the user asked for it in this turn (a task or reminder in JARVIS itself)
    CREATE_FROM_EXTERNAL      confirmation (the request came from someone else's text, not from the user)
    MODIFY_CALENDAR           confirmation naming the exact change
    SEND_EMAIL / EXTERNAL_MESSAGE / SENSITIVE_DESKTOP   confirmation naming the exact action
    DELETE_DATA               explicit confirmation (the reply must say to delete/confirm, a bare "yes" is not enough)

`ToolCategory` gives every tool one of the four categories from its risk level, so the tool list can be audited.
"""

from enum import StrEnum

from backend.core.security.models import RiskLevel


class ApprovalClass(StrEnum):
    READ = "read"
    ANALYZE = "analyze"
    PLAN = "plan"
    CREATE_LOCAL = "create_local"  # a task or reminder the user asked for in this very turn
    CREATE_FROM_EXTERNAL = "create_from_external"  # a task derived from an email/document: asks first unless the user turned that off
    MODIFY_CALENDAR = "modify_calendar"
    SEND_EMAIL = "send_email"
    EXTERNAL_MESSAGE = "external_message"
    SENSITIVE_DESKTOP = "sensitive_desktop"
    DELETE_DATA = "delete_data"
    CHANGE_INTEGRATION = "change_integration"  # disconnecting an account, revoking access, deleting synchronized data


_AUTOMATIC = frozenset({ApprovalClass.READ, ApprovalClass.ANALYZE, ApprovalClass.PLAN})
_EXPLICIT = frozenset({ApprovalClass.DELETE_DATA})


def requires_confirmation(action: ApprovalClass) -> bool:
    """Only reading, analyzing and planning are automatic. Creating a local task also goes through the user's own request."""
    return action not in _AUTOMATIC and action is not ApprovalClass.CREATE_LOCAL


def requires_explicit_confirmation(action: ApprovalClass) -> bool:
    """Destructive actions: a bare "yes" is not enough."""
    return action in _EXPLICIT


class ToolCategory(StrEnum):
    READ_ONLY = "read_only"
    LOW_RISK = "low_risk"
    CONFIRMATION_REQUIRED = "confirmation_required"
    SENSITIVE = "sensitive"


def tool_category(risk: RiskLevel, requires_permission: bool) -> ToolCategory:
    """READ_ONLY: local information, no permission. LOW_RISK: reversible, local. CONFIRMATION_REQUIRED: changes something
    external or is hard to undo. SENSITIVE: sends data out or destroys it. Unknown risk falls to the strictest category."""
    if risk <= RiskLevel.LOW:
        return ToolCategory.LOW_RISK if requires_permission else ToolCategory.READ_ONLY
    if risk == RiskLevel.MEDIUM:
        return ToolCategory.CONFIRMATION_REQUIRED
    return ToolCategory.SENSITIVE
