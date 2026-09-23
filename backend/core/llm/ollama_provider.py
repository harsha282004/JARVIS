"""Ollama-backed LLMProvider implementation.

Talks to a local Ollama server over its REST API (`/api/generate`). Requires
Ollama to be installed, running, and have the configured model pulled —
this module never fabricates a response if the server is unreachable or
the model is missing.
"""

import httpx

from backend.core.llm.base import LLMProvider, LLMProviderError
from backend.core.logging import get_logger

logger = get_logger(__name__)


class OllamaProvider(LLMProvider):
    """LLMProvider implementation backed by a local Ollama server."""

    def __init__(self, base_url: str, model: str, timeout: float = 60.0):
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._timeout = timeout

    def generate(self, prompt: str, system: str | None = None, **kwargs) -> str:
        payload = {
            "model": self._model,
            "prompt": prompt,
            "stream": False,
        }
        if system:
            payload["system"] = system

        try:
            response = httpx.post(
                f"{self._base_url}/api/generate",
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

        data = response.json()
        text = data.get("response", "").strip()
        if not text:
            raise LLMProviderError(
                f"Ollama returned an empty response for model '{self._model}'"
            )
        return text
