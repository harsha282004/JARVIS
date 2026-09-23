"""Integration abstraction.

Defines the contract every external-service integration (Gmail, Calendar,
messaging, GitHub, browser, documents, ...) will implement. An Integration
is a boundary around a specific external system; it does not grant the LLM
or agent direct access — actions still flow through
backend.core.security.PermissionManager and a Tool. No concrete integration
is implemented in Phase 0.
"""

from abc import ABC, abstractmethod


class Integration(ABC):
    """Base interface for an external-service integration."""

    name: str

    @abstractmethod
    def is_configured(self) -> bool:
        """Report whether required credentials/config are present. Not implemented in Phase 0."""
        raise NotImplementedError
