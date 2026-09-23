"""Tool abstraction.

Every capability the agent can invoke (email, calendar, browser, desktop,
...) implements this interface. Tool execution always goes through
backend.core.security.PermissionManager before touching an external
system; no tool subclass may bypass that boundary. No concrete tools
(EmailTool, CalendarTool, BrowserTool, DesktopTool, ...) exist yet.

The reasoning layer (AgentBrain) only ever sees a `ToolDescriptor`, never
the Tool object, so it has no way to call `run`.
"""

from abc import ABC, abstractmethod
from typing import Any

from pydantic import BaseModel, Field


class ToolDescriptor(BaseModel):
    """Read-only description of a tool, safe to show to the LLM."""

    name: str = Field(min_length=1, max_length=64)
    description: str
    input_schema: dict[str, Any] = Field(default_factory=dict)
    requires_permission: bool = True


class Tool(ABC):
    """Base interface for an agent-invokable tool."""

    name: str
    description: str
    input_schema: dict[str, Any] = {}
    # Fail-safe default: a tool needs explicit permission unless it says otherwise.
    requires_permission: bool = True

    def descriptor(self) -> ToolDescriptor:
        return ToolDescriptor(
            name=self.name,
            description=self.description,
            input_schema=self.input_schema,
            requires_permission=self.requires_permission,
        )

    @abstractmethod
    def run(self, **kwargs: Any) -> Any:
        """Execute the tool. Never called by the reasoning layer."""
        raise NotImplementedError
