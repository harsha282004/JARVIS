"""Security foundation: the permission boundary between the LLM and tools.

Architectural rule enforced by this module (see docs/security.md):

    LLM -> PermissionManager -> Tool -> External System

The LLM must never invoke a Tool directly. Every tool invocation is routed
through PermissionManager.authorize() first. Phase 0 defines this boundary
and a deny-by-default default policy; it does not implement fine-grained
permission logic (per-user rules, scopes, prompts, audit storage) — that
belongs to a later phase.
"""

from dataclasses import dataclass

from backend.core.logging import get_logger

logger = get_logger(__name__)


@dataclass(frozen=True)
class PermissionRequest:
    """Describes a single tool-invocation request awaiting authorization."""

    tool_name: str
    action: str
    requested_by: str = "agent"


class PermissionDenied(Exception):
    """Raised when a tool invocation is not authorized."""


class PermissionManager:
    """Gatekeeper that all tool execution must pass through.

    Phase 0 default policy: deny everything. Later phases will replace
    `authorize` with real rules (user consent, scopes, per-tool policy)
    without changing the call sites that depend on this interface.
    """

    def authorize(self, request: PermissionRequest) -> bool:
        logger.warning(
            "Permission check for tool=%s action=%s denied by default policy "
            "(no permission rules implemented yet)",
            request.tool_name,
            request.action,
        )
        return False

    def require(self, request: PermissionRequest) -> None:
        """Raise PermissionDenied if the request is not authorized."""
        if not self.authorize(request):
            raise PermissionDenied(
                f"Tool '{request.tool_name}' action '{request.action}' was not authorized"
            )
