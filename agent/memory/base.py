"""Memory abstraction.

The contract for a long-term personal memory store. Phase 0 defined this as
a generic `store(key, value)` / `retrieve(query)` placeholder; Phase 6 makes
the operations structured (type, source, confidence) and adds update, delete
and search. Nothing here depends on PostgreSQL: see service.py (rules) and
repository.py (persistence) behind it.
"""

from abc import ABC, abstractmethod
from collections.abc import Sequence

from agent.memory.models import Memory, MemoryCandidate, MemoryStatus, MemoryType, StoreResult


class MemoryInterface(ABC):
    """Base interface for a memory store the agent can read/write."""

    @abstractmethod
    def store(self, candidate: MemoryCandidate) -> StoreResult:
        """Persist a memory candidate (deduplicating and resolving conflicts)."""
        raise NotImplementedError

    @abstractmethod
    def retrieve(self, query: str, limit: int | None = None) -> list[Memory]:
        """Return the active memories relevant to `query`, and record that they were used."""
        raise NotImplementedError

    @abstractmethod
    def update(self, memory_id: str, content: str) -> Memory:
        """Change the content of an existing memory."""
        raise NotImplementedError

    @abstractmethod
    def delete(self, memory_id: str) -> bool:
        """Deactivate (soft-delete) a memory. Returns False if it does not exist."""
        raise NotImplementedError

    @abstractmethod
    def search(
        self,
        text: str | None = None,
        types: Sequence[MemoryType] | None = None,
        statuses: Sequence[MemoryStatus] | None = None,
        limit: int = 20,
    ) -> list[Memory]:
        """Look memories up without marking them as used."""
        raise NotImplementedError
