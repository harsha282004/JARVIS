"""LLM provider abstraction.

Defines the contract future providers (OllamaProvider, a cloud provider,
etc.) will implement, so the rest of the system never depends on a
specific LLM backend. No provider is implemented in Phase 0.
"""

from abc import ABC, abstractmethod


class LLMProvider(ABC):
    """Base interface for a chat-completion capable LLM backend."""

    @abstractmethod
    def generate(self, prompt: str, **kwargs) -> str:
        """Return a completion for the given prompt. Not implemented in Phase 0."""
        raise NotImplementedError
