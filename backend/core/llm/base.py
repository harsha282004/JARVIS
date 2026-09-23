"""LLM provider abstraction.

Defines the contract LLM backends implement (see ollama_provider.py for the
Phase 1 concrete implementation) so the rest of the system never depends on
a specific LLM backend.
"""

from abc import ABC, abstractmethod


class LLMProviderError(Exception):
    """Raised when an LLM provider cannot fulfill a generate() request."""


class LLMProvider(ABC):
    """Base interface for a chat-completion capable LLM backend."""

    @abstractmethod
    def generate(self, prompt: str, system: str | None = None, **kwargs) -> str:
        """Return a completion for the given prompt.

        Raises LLMProviderError if the backend is unreachable or errors —
        never fabricates a response.
        """
        raise NotImplementedError
