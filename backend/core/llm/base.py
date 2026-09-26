"""LLM provider abstraction.

Defines the contract LLM backends implement (see ollama_provider.py for the
concrete implementation) so the rest of the system never depends on a
specific LLM backend.
"""

from abc import ABC, abstractmethod
from collections.abc import Sequence

from backend.core.llm.messages import Message, Role


class LLMProviderError(Exception):
    """Raised when an LLM provider cannot fulfill a request. `kind` classifies it (config, auth, network, timeout, rate_limit, model, bad_request, server, bad_response,
    unavailable) so callers and health checks can tell an API-key problem from an outage without parsing text."""

    def __init__(self, message: str = "", kind: str = "unavailable"):
        super().__init__(message)
        self.kind = kind


class LLMProvider(ABC):
    """Base interface for a chat-completion capable LLM backend."""

    @abstractmethod
    def chat(self, messages: Sequence[Message], json_mode: bool = False) -> str:
        """Return the assistant reply to an ordered message list.

        `json_mode` asks the backend to constrain its output to JSON where it
        can. It is a hint: callers must still validate what they receive.

        Raises LLMProviderError if the backend is unreachable or errors,
        and never fabricates a response.
        """
        raise NotImplementedError

    def generate(self, prompt: str, system: str | None = None) -> str:
        """Single-turn convenience wrapper around `chat`."""
        messages = [Message(Role.SYSTEM, system)] if system else []
        messages.append(Message(Role.USER, prompt))
        return self.chat(messages)
