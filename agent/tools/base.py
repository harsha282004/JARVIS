"""Tool abstraction.

Every capability the agent can invoke (email, calendar, browser, desktop,
...) implements this interface. Tool execution always goes through
backend.core.security.PermissionManager before touching an external
system; no tool subclass may bypass that boundary. No concrete tools
(EmailTool, CalendarTool, BrowserTool, DesktopTool, ...) exist yet.

The reasoning layer (AgentBrain) only ever sees a `ToolDescriptor`, never
the Tool object, so it has no way to call `run`. The sanctioned way to run a
tool is `execute`, which asks the PermissionManager first and denies on any
doubt. Nothing in JARVIS calls it yet: no real tools exist.
"""

from abc import ABC, abstractmethod
from typing import Any

from pydantic import BaseModel, Field

from backend.core.security import (
    PermissionDenied,
    PermissionManager,
    PermissionScope,
    RiskLevel,
    ToolSecurityInfo,
    check_authorization,
)


EXECUTE_ACTION = "execute"


class ToolDescriptor(BaseModel):
    """Read-only description of a tool, safe to show to the LLM."""

    name: str = Field(min_length=1, max_length=64)
    description: str
    input_schema: dict[str, Any] = Field(default_factory=dict)
    requires_permission: bool = True
    # Fail-safe defaults: unless a tool declares otherwise it is high risk and one-time only.
    risk: RiskLevel = RiskLevel.HIGH
    allowed_scopes: list[PermissionScope] = Field(default_factory=lambda: [PermissionScope.ONE_TIME])

    def security_info(self) -> ToolSecurityInfo:
        """What the PermissionManager needs to know. Registered by trusted
        code, never derived from LLM output."""
        return ToolSecurityInfo(
            name=self.name,
            requires_permission=self.requires_permission,
            risk=self.risk,
            allowed_scopes=tuple(self.allowed_scopes),
        )


class Tool(ABC):
    """Base interface for an agent-invokable tool."""

    name: str
    description: str
    input_schema: dict[str, Any] = {}
    # Fail-safe default: a tool needs explicit permission unless it says otherwise.
    requires_permission: bool = True
    risk: RiskLevel = RiskLevel.HIGH
    allowed_scopes: tuple[PermissionScope, ...] = (PermissionScope.ONE_TIME,)

    def descriptor(self) -> ToolDescriptor:
        return ToolDescriptor(
            name=self.name,
            description=self.description,
            input_schema=self.input_schema,
            requires_permission=self.requires_permission,
            risk=self.risk,
            allowed_scopes=list(self.allowed_scopes),
        )

    def execute(
        self,
        permissions: PermissionManager | None,
        request_id: str,
        *,
        session_id: str | None = None,
        **parameters: Any,
    ) -> Any:
        """Run the tool only if `permissions` authorizes exactly this call.

        The parameters passed here are re-hashed and compared with what was
        approved. Raises PermissionDenied otherwise (including when
        `permissions` is missing or fails).
        """
        result = check_authorization(
            permissions,
            request_id,
            tool_name=self.name,
            action=EXECUTE_ACTION,
            parameters=parameters,
            session_id=session_id,
        )
        if not result.allowed:
            raise PermissionDenied(f"Tool '{self.name}' was not authorized: {result.code.value}")
        return self.run(**parameters)

    @abstractmethod
    def run(self, **kwargs: Any) -> Any:
        """Execute the tool. Never called by the reasoning layer."""
        raise NotImplementedError
