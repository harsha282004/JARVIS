"""Builds the configured LLMProvider from settings (the one place that knows provider names). Building never contacts the network."""

from backend.core.config import Settings
from backend.core.llm.base import LLMProvider


class UnknownProviderError(ValueError):
    """LLM_PROVIDER names a provider that does not exist."""


def build_llm(settings: Settings) -> LLMProvider:
    name = settings.LLM_PROVIDER.strip().lower()
    if name == "groq":
        from backend.core.llm.groq_provider import GroqProvider

        return GroqProvider(settings.GROQ_API_KEY.get_secret_value(), settings.LLM_MODEL, settings.GROQ_BASE_URL, timeout=settings.LLM_TIMEOUT_SECONDS,
                            max_retries=settings.LLM_MAX_RETRIES, temperature=settings.LLM_TEMPERATURE, max_tokens=settings.LLM_MAX_TOKENS,
                            reasoning_effort=settings.GROQ_REASONING_EFFORT)
    if name == "ollama":
        from backend.core.llm.ollama_provider import OllamaProvider

        return OllamaProvider(base_url=settings.OLLAMA_BASE_URL, model=settings.LLM_MODEL)
    raise UnknownProviderError(f"Unknown LLM_PROVIDER '{settings.LLM_PROVIDER}' (use groq or ollama)")
