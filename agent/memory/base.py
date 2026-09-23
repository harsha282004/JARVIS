"""Memory abstraction.

Defines the contract for storing and retrieving agent memory (short-term
conversation state, long-term personal memory, RAG retrieval, ...).
No concrete memory backend or storage logic is implemented in Phase 0.
"""

from abc import ABC, abstractmethod
from typing import Any


class MemoryInterface(ABC):
    """Base interface for a memory store the agent can read/write."""

    @abstractmethod
    def store(self, key: str, value: Any) -> None:
        """Persist a memory item. Not implemented in Phase 0."""
        raise NotImplementedError

    @abstractmethod
    def retrieve(self, query: str) -> list[Any]:
        """Retrieve memory items relevant to a query. Not implemented in Phase 0."""
        raise NotImplementedError
