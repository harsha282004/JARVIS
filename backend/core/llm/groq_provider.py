"""Groq-backed LLMProvider (OpenAI-compatible chat completions over HTTPS).

Uses httpx, which the project already depends on: no extra SDK. The API key is only ever sent in the Authorization header; it never appears in an error message,
a log line, a repr or a health result. The model's reply is returned as text exactly like every other provider: callers (the agent brain, extraction, RAG) still
validate what they receive, and no model output reaches a tool except through the existing Tool Router / Permission Manager path.

Failures are classified (`LLMProviderError.kind`): config, auth, network, timeout, rate_limit, model, bad_request, server, bad_response. Reads are retried for rate limits,
5xx and transient network errors (bounded, with backoff); nothing is retried for auth/model/config problems and nothing is ever fabricated.
"""

import re
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

import httpx

from backend.core.llm.base import LLMProvider, LLMProviderError
from backend.core.llm.messages import Message, Role
from backend.core.logging import get_logger
from backend.core.redaction import redact

logger = get_logger(__name__)

DEFAULT_BASE_URL = "https://api.groq.com/openai/v1"
DEFAULT_MODEL = "openai/gpt-oss-20b"
_MAX_RETRY_AFTER = 5.0


def _safe_detail(response: httpx.Response) -> str:
    """The API's own short error message (never headers, never the request)."""
    try:
        body = response.json()
        text = str((body.get("error") or {}).get("message") or "")
    except Exception:  # noqa: BLE001
        text = ""
    return redact(re.sub(r"\s+", " ", text))[:200]


@dataclass
class LLMHealth:
    """Staged provider health: configured -> key -> reachable -> authenticated -> model available -> inference. Safe to show (no credentials)."""

    provider: str
    model: str
    configured: bool = True
    key_configured: bool = False
    reachable: bool | None = None
    authenticated: bool | None = None
    model_available: bool | None = None
    inference: bool | None = None          # None = not probed
    problem: str = ""                       # kind of the first failing stage
    detail: str = ""
    latency_ms: float | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.problem

    def to_dict(self) -> dict[str, Any]:
        return {"provider": self.provider, "model": self.model, "provider_configured": self.configured, "api_key_configured": self.key_configured, "provider_reachable": self.reachable,
                "authentication": self.authenticated, "model_available": self.model_available, "inference": self.inference, "problem": self.problem or None, "detail": self.detail,
                "latency_ms": self.latency_ms}


class GroqProvider(LLMProvider):
    name = "groq"

    def __init__(self, api_key: str, model: str = DEFAULT_MODEL, base_url: str = DEFAULT_BASE_URL, *, timeout: float = 30.0, max_retries: int = 2, temperature: float = 0.3,
                 max_tokens: int = 2048, reasoning_effort: str = "low", transport: httpx.BaseTransport | None = None, sleep: Callable[[float], None] = time.sleep):
        self._key = (api_key or "").strip()
        self.model = model
        self._base = base_url.rstrip("/")
        self._timeout = timeout
        self._max_retries = max(0, max_retries)
        self._temperature = temperature
        self._max_tokens = max_tokens
        self._reasoning = reasoning_effort
        self._transport = transport
        self._sleep = sleep

    def __repr__(self) -> str:  # never show the key
        return f"GroqProvider(model={self.model!r}, base_url={self._base!r}, api_key={'set' if self._key else 'missing'})"

    # ---- HTTP ------------------------------------------------------------------------------------------------------------------------

    def _client(self) -> httpx.Client:
        return httpx.Client(timeout=httpx.Timeout(self._timeout, connect=min(10.0, self._timeout)), transport=self._transport)

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._key}", "Content-Type": "application/json"}

    def _require_key(self) -> None:
        if not self._key:
            raise LLMProviderError("Groq API key missing: set GROQ_API_KEY in .env.", kind="config")

    def _classify(self, response: httpx.Response) -> LLMProviderError:
        code, detail = response.status_code, _safe_detail(response)
        low = detail.lower()
        if code in (401, 403):
            return LLMProviderError("Groq authentication failed: check GROQ_API_KEY.", kind="auth")
        if code == 429:
            return LLMProviderError("Groq rate limit reached; try again shortly.", kind="rate_limit")
        if "response_format" in low or "json_object" in low:
            return LLMProviderError(f"Groq rejected the request ({code}): {detail}", kind="bad_request")
        if code == 404 or (code in (400, 422) and "model" in low and any(w in low for w in ("not found", "does not exist", "decommissioned", "not supported", "no access"))):
            return LLMProviderError(f"Groq model unavailable: '{self.model}' ({detail or 'not found'}).", kind="model")
        if code >= 500:
            return LLMProviderError(f"Groq service error ({code}).", kind="server")
        return LLMProviderError(f"Groq rejected the request ({code}): {detail or 'bad request'}", kind="bad_request")

    def _post(self, path: str, payload: dict[str, Any]) -> httpx.Response:
        """One POST with bounded retries for rate limits, 5xx and transient network errors. Returns a 2xx response or raises LLMProviderError."""
        last: LLMProviderError | None = None
        for attempt in range(self._max_retries + 1):
            delay = 0.5 * (2 ** attempt)
            try:
                with self._client() as client:
                    response = client.post(f"{self._base}{path}", json=payload, headers=self._headers())
            except httpx.TimeoutException:
                last = LLMProviderError("Groq request timed out.", kind="timeout")
            except httpx.HTTPError as exc:
                last = LLMProviderError(f"Groq network unavailable ({type(exc).__name__}).", kind="network")
            else:
                if response.status_code < 300:
                    return response
                last = self._classify(response)
                if last.kind not in ("rate_limit", "server"):
                    raise last
                try:
                    delay = min(float(response.headers.get("retry-after", delay)), _MAX_RETRY_AFTER)
                except ValueError:
                    pass
            if attempt < self._max_retries:
                logger.warning("Groq request failed (%s); retrying (%d/%d)", last.kind, attempt + 1, self._max_retries)
                self._sleep(delay)
        assert last is not None
        raise last

    # ---- chat -------------------------------------------------------------------------------------------------------------------------

    def _payload(self, messages: Sequence[Message], json_mode: bool, *, max_tokens: int | None = None) -> dict[str, Any]:
        payload: dict[str, Any] = {"model": self.model, "messages": [{"role": m.role.value, "content": m.content} for m in messages], "temperature": self._temperature,
                                   "max_completion_tokens": max_tokens or self._max_tokens, "stream": False}
        if "gpt-oss" in self.model and self._reasoning:
            payload["reasoning_effort"] = self._reasoning            # keeps a spoken assistant's latency low
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        return payload

    def chat(self, messages: Sequence[Message], json_mode: bool = False) -> str:
        self._require_key()
        payload = self._payload(messages, json_mode)
        try:
            response = self._post("/chat/completions", payload)
        except LLMProviderError as exc:
            if json_mode and exc.kind == "bad_request" and "response_format" in str(exc).lower():
                payload.pop("response_format")                        # the model has no JSON mode: the caller's schema validation is the safety net
                response = self._post("/chat/completions", payload)
            else:
                raise
        return self._text(response)

    def _text(self, response: httpx.Response) -> str:
        try:
            choice = response.json()["choices"][0]
            text = (choice.get("message") or {}).get("content") or ""
            finish = choice.get("finish_reason")
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise LLMProviderError("Groq returned a malformed response.", kind="bad_response") from exc
        text = text.strip()
        if not text:
            if finish == "length":
                raise LLMProviderError("Groq's reply was cut off before any text (raise LLM_MAX_TOKENS).", kind="bad_response")
            raise LLMProviderError(f"Groq returned an empty response for model '{self.model}'.", kind="bad_response")
        return text

    # ---- health ------------------------------------------------------------------------------------------------------------------------

    def health(self, inference: bool = False) -> LLMHealth:
        """Staged, credential-free health. `/models` proves reachability, authentication and model availability at no token cost; `inference=True` also runs a tiny real chat."""
        h = LLMHealth("Groq", self.model, key_configured=bool(self._key))
        if not self._key:
            h.problem, h.detail = "config", "Groq API key missing: set GROQ_API_KEY in .env."
            return h
        began = time.perf_counter()
        try:
            with self._client() as client:
                response = client.get(f"{self._base}/models", headers=self._headers())
        except httpx.TimeoutException:
            h.reachable, h.problem, h.detail = False, "timeout", "Groq request timed out."
            return h
        except httpx.HTTPError as exc:
            h.reachable, h.problem, h.detail = False, "network", f"Groq network unavailable ({type(exc).__name__})."
            return h
        h.reachable = True
        h.latency_ms = round((time.perf_counter() - began) * 1000, 1)
        if response.status_code >= 300:
            err = self._classify(response)
            h.authenticated = False if err.kind == "auth" else (True if err.kind in ("rate_limit", "server") else None)
            h.problem, h.detail = err.kind, str(err)
            return h
        h.authenticated = True
        try:
            ids = {m.get("id") for m in response.json().get("data", [])}
        except Exception:  # noqa: BLE001
            ids = set()
        h.model_available = self.model in ids if ids else None
        if h.model_available is False:
            h.problem, h.detail = "model", f"Groq model unavailable: '{self.model}' is not offered to this key."
            return h
        h.detail = "reachable, authenticated, model available"
        if inference:
            try:
                self.chat([Message(Role.USER, "Reply with the single word: OK")])
                h.inference = True
                h.detail += ", inference succeeded"
            except LLMProviderError as exc:
                h.inference, h.problem, h.detail = False, exc.kind, str(exc)
        return h
