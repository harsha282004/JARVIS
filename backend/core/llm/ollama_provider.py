"""Ollama-backed LLMProvider implementation.

Talks to a local Ollama server over its REST API (`/api/chat`). Requires
Ollama to be installed, running, and have the configured model pulled.
This module never fabricates a response if the server is unreachable or
the model is missing.
"""

from collections.abc import Sequence

import httpx

from backend.core.llm.base import LLMProvider, LLMProviderError
from backend.core.llm.messages import Message
from backend.core.logging import get_logger

logger = get_logger(__name__)


class OllamaProvider(LLMProvider):
    """LLMProvider implementation backed by a local Ollama server."""

    def __init__(self, base_url: str, model: str, timeout: float = 60.0):
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._timeout = timeout

    def chat(self, messages: Sequence[Message]) -> str:
        payload = {
            "model": self._model,
            "messages": [{"role": m.role.value, "content": m.content} for m in messages],
            "stream": False,
        }

        try:
            response = httpx.post(
                f"{self._base_url}/api/chat",
                json=payload,
                timeout=self._timeout,
            )
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise LLMProviderError(
                f"Ollama returned an error for model '{self._model}': {exc}"
            ) from exc
        except httpx.HTTPError as exc:
            raise LLMProviderError(
                f"Could not reach Ollama at {self._base_url}. Is it running? ({exc})"
            ) from exc

        text = response.json().get("message", {}).get("content", "").strip()
        if not text:
            raise LLMProviderError(
                f"Ollama returned an empty response for model '{self._model}'"
            )
        return text
