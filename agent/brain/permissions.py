"""Turns an AgentDecision into PermissionRequests held by the PermissionManager.

This only *asks*. Nothing here approves a request or runs a tool, and the
decision's own claims (`available`, `requires_permission`) are ignored: the
PermissionManager's own tool registry and policy decide. A tool it does not
know is denied on the spot.
"""

from agent.brain.models import AgentDecision
from agent.tools.base import EXECUTE_ACTION
from backend.core.security import PermissionManager, PermissionRequest


def request_permissions(
    decision: AgentDecision, manager: PermissionManager, session_id: str | None = None
) -> list[PermissionRequest]:
    if not decision.action_required:
        return []
    return [
        manager.request_permission(
            tool_name=selection.name,
            action=EXECUTE_ACTION,
            description=f"Use the {selection.name} tool",
            session_id=session_id,
            requested_by="agent",
        )
        for selection in decision.selected_tools
    ]
