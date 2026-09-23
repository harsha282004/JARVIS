"""Unit tests for OllamaProvider, with httpx.post mocked.

No real Ollama server is contacted here — that is covered by manual
end-to-end testing (docs/voice-system.md). These tests only verify request
shape and that failures are surfaced as LLMProviderError, never faked.
"""

import httpx
import pytest

from backend.core.llm.base import LLMProviderError
from backend.core.llm.ollama_provider import OllamaProvider


class _FakeResponse:
    def __init__(self, json_data, status_code=200):
        self._json_data = json_data
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("error", request=None, response=self)

    def json(self):
        return self._json_data


def test_generate_returns_response_text(monkeypatch):
    captured = {}

    def fake_post(url, json, timeout):
        captured["url"] = url
        captured["json"] = json
        return _FakeResponse({"response": "Today is Tuesday."})

    monkeypatch.setattr(httpx, "post", fake_post)

    provider = OllamaProvider(base_url="http://localhost:11434", model="llama3")
    result = provider.generate("What is today's date?", system="Be brief.")

    assert result == "Today is Tuesday."
    assert captured["url"] == "http://localhost:11434/api/generate"
    assert captured["json"]["model"] == "llama3"
    assert captured["json"]["system"] == "Be brief."
    assert captured["json"]["stream"] is False


def test_generate_raises_on_connection_failure(monkeypatch):
    def fake_post(url, json, timeout):
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(httpx, "post", fake_post)

    provider = OllamaProvider(base_url="http://localhost:11434", model="llama3")
    with pytest.raises(LLMProviderError, match="Is it running"):
        provider.generate("hello")


def test_generate_raises_on_empty_response(monkeypatch):
    def fake_post(url, json, timeout):
        return _FakeResponse({"response": ""})

    monkeypatch.setattr(httpx, "post", fake_post)

    provider = OllamaProvider(base_url="http://localhost:11434", model="llama3")
    with pytest.raises(LLMProviderError, match="empty response"):
        provider.generate("hello")
