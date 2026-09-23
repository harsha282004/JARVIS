"""Unit tests for OllamaProvider, with httpx.post mocked.

No real Ollama server is contacted here; that is covered by the integration
tests. These only verify request shape and that failures are surfaced as
LLMProviderError, never faked.
"""

import httpx
import pytest

from backend.core.llm.base import LLMProvider, LLMProviderError
from backend.core.llm.messages import Message, Role
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


def _provider():
    return OllamaProvider(base_url="http://localhost:11434", model="llama3")


def test_ollama_is_an_llm_provider():
    assert isinstance(_provider(), LLMProvider)


def test_chat_sends_full_message_list_and_returns_reply(monkeypatch):
    captured = {}

    def fake_post(url, json, timeout):
        captured["url"] = url
        captured["json"] = json
        return _FakeResponse({"message": {"role": "assistant", "content": " Paris. "}})

    monkeypatch.setattr(httpx, "post", fake_post)

    result = _provider().chat(
        [
            Message(Role.SYSTEM, "Be brief."),
            Message(Role.USER, "Capital of France?"),
            Message(Role.ASSISTANT, "Paris."),
            Message(Role.USER, "How far is it from London?"),
        ]
    )

    assert result == "Paris."
    assert captured["url"] == "http://localhost:11434/api/chat"
    assert captured["json"]["model"] == "llama3"
    assert captured["json"]["stream"] is False
    assert captured["json"]["messages"] == [
        {"role": "system", "content": "Be brief."},
        {"role": "user", "content": "Capital of France?"},
        {"role": "assistant", "content": "Paris."},
        {"role": "user", "content": "How far is it from London?"},
    ]


def test_generate_wraps_prompt_as_system_plus_user(monkeypatch):
    captured = {}

    def fake_post(url, json, timeout):
        captured["messages"] = json["messages"]
        return _FakeResponse({"message": {"content": "ok"}})

    monkeypatch.setattr(httpx, "post", fake_post)

    assert _provider().generate("hello", system="Be brief.") == "ok"
    assert captured["messages"] == [
        {"role": "system", "content": "Be brief."},
        {"role": "user", "content": "hello"},
    ]


def test_generate_without_system_sends_only_user_message(monkeypatch):
    captured = {}

    def fake_post(url, json, timeout):
        captured["messages"] = json["messages"]
        return _FakeResponse({"message": {"content": "ok"}})

    monkeypatch.setattr(httpx, "post", fake_post)

    _provider().generate("hello")
    assert captured["messages"] == [{"role": "user", "content": "hello"}]


def test_chat_raises_on_connection_failure(monkeypatch):
    def fake_post(url, json, timeout):
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(httpx, "post", fake_post)

    with pytest.raises(LLMProviderError, match="Is it running"):
        _provider().chat([Message(Role.USER, "hello")])


def test_chat_raises_on_empty_response(monkeypatch):
    def fake_post(url, json, timeout):
        return _FakeResponse({"message": {"content": ""}})

    monkeypatch.setattr(httpx, "post", fake_post)

    with pytest.raises(LLMProviderError, match="empty response"):
        _provider().chat([Message(Role.USER, "hello")])
