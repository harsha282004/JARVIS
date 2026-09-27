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


# Distinct, honest text per `LLMProviderError.kind`, shared by every caller that speaks or displays an LLM failure
# (the voice engine and the dashboard chat endpoint) so a transient rate limit, a slow response and a real outage are
# never all reported as the same flat "unavailable" -- and so a Gmail/tool failure (which never raises this) can never
# be mistaken for one either. See docs/GROQ_RATE_LIMITS.md for what was measured.
GENERIC_LLM_ERROR = "I can't reach the language model right now. I can still help with reminders, your calendar and your email."
LLM_ERROR_MESSAGES: dict[str, str] = {
    "rate_limit": "I'm being rate-limited by my language model right now. Give me a moment and ask again.",
    "timeout": "That took too long for my language model to answer. Please try asking again.",
    "auth": "There's a problem with my language model's setup (an authentication error). This needs the API key checked.",
    "config": "My language model isn't configured correctly. This needs a look at my settings.",
    "model": "The language model I'm set up to use isn't available right now.",
    "server": "My language model's service is having trouble right now. Please try again shortly.",
    "network": "I can't reach my language model right now -- there may be a network problem.",
    "bad_response": "My language model gave me a response I couldn't use. Please try asking again.",
}


def describe_llm_error(exc: LLMProviderError) -> str:
    """The honest, user-facing text for `exc`, by its classified `kind` (falls back to the generic message)."""
    return LLM_ERROR_MESSAGES.get(getattr(exc, "kind", ""), GENERIC_LLM_ERROR)


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
