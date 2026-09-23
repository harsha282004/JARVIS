"""Tool abstraction.

Every capability the agent can invoke (email, calendar, browser, desktop,
...) implements this interface. Tool execution always goes through
backend.core.security.PermissionManager before touching an external
system — no tool subclass may bypass that boundary. No concrete tools
(EmailTool, CalendarTool, BrowserTool, DesktopTool, ...) exist yet.
"""

from abc import ABC, abstractmethod
from typing import Any


class Tool(ABC):
    """Base interface for an agent-invokable tool."""

    name: str
    description: str

    @abstractmethod
    def run(self, **kwargs: Any) -> Any:
        """Execute the tool. Not implemented in Phase 0."""
        raise NotImplementedError
