"""Groq provider: request shape, multi-turn history, JSON mode, retries, classified failures, key secrecy, staged health, factory/config, and the full agent path
(conversation, tool call through the PermissionManager, hostile/malformed model output). Groq is simulated with httpx.MockTransport: NO real key is used or needed,
and nothing here contacts the network. The real-inference counterpart is scripts/llm_real_check.py (needs your GROQ_API_KEY)."""

import json
import logging
from pathlib import Path

import httpx
import pytest

from backend.core.config import Settings
from backend.core.llm.base import LLMProvider, LLMProviderError
from backend.core.llm.factory import UnknownProviderError, build_llm
from backend.core.llm.groq_provider import DEFAULT_BASE_URL, DEFAULT_MODEL, GroqProvider
from backend.core.llm.messages import Message, Role
from backend.core.redaction import redact

FAKE_KEY = "gsk_TESTKEYTESTKEYTESTKEYTESTKEY1234"     # obviously fake, matches the key shape so redaction is exercised
ROOT = Path(__file__).resolve().parents[2]


def completion(text, finish="stop"):
    return {"id": "x", "choices": [{"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": finish}]}


class Groq:
    """A scripted api.groq.com: `script` items are (status, json|None, headers) or an exception to raise; records every request."""

    def __init__(self, *script, models=(DEFAULT_MODEL, "llama-3.3-70b-versatile")):
        self.script, self.requests, self.models = list(script), [], list(models)

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.method == "GET" and request.url.path.endswith("/models"):
            if request.headers.get("authorization") != f"Bearer {FAKE_KEY}":
                return httpx.Response(401, json={"error": {"message": "Invalid API Key"}})
            return httpx.Response(200, json={"data": [{"id": m} for m in self.models]})
        item = self.script.pop(0) if self.script else (200, completion("ok"), {})
        if isinstance(item, Exception):
            raise item
        status, body, headers = (item + ({},))[:3] if len(item) == 2 else item
        return httpx.Response(status, json=body, headers=headers)

    def provider(self, key=FAKE_KEY, **kw):
        kw.setdefault("sleep", lambda s: None)
        return GroqProvider(key, transport=httpx.MockTransport(self.handler), **kw)

    def bodies(self):
        return [json.loads(r.content) for r in self.requests if r.method == "POST"]


# ---- request shape, history ----------------------------------------------------------------------------------------------------------------

def test_is_an_llm_provider_and_defaults():
    p = GroqProvider(FAKE_KEY)
    assert isinstance(p, LLMProvider) and p.model == "openai/gpt-oss-20b" and DEFAULT_BASE_URL == "https://api.groq.com/openai/v1"


def test_chat_request_shape_auth_and_multi_turn_history():
    g = Groq((200, completion(" Paris. ")))
    out = g.provider(temperature=0.2, max_tokens=300).chat([Message(Role.SYSTEM, "Be brief."), Message(Role.USER, "Capital of France?"), Message(Role.ASSISTANT, "Paris."),
                                                             Message(Role.USER, "How far is London?")])
    assert out == "Paris."
    req = g.requests[0]
    assert str(req.url) == "https://api.groq.com/openai/v1/chat/completions" and req.headers["authorization"] == f"Bearer {FAKE_KEY}"
    body = g.bodies()[0]
    assert body["model"] == "openai/gpt-oss-20b" and body["temperature"] == 0.2 and body["max_completion_tokens"] == 300 and body["stream"] is False
    assert body["reasoning_effort"] == "low" and "response_format" not in body
    assert [(m["role"], m["content"]) for m in body["messages"]] == [("system", "Be brief."), ("user", "Capital of France?"), ("assistant", "Paris."), ("user", "How far is London?")]


def test_reasoning_effort_only_for_gpt_oss_models():
    g = Groq((200, completion("hi")))
    g.provider(model="llama-3.3-70b-versatile").chat([Message(Role.USER, "hi")])
    assert "reasoning_effort" not in g.bodies()[0]


def test_generate_wrapper_and_json_mode():
    g = Groq((200, completion("hello")), (200, completion('{"a": 1}')))
    p = g.provider()
    assert p.generate("hi", system="s") == "hello"
    assert [m["role"] for m in g.bodies()[0]["messages"]] == ["system", "user"]
    assert p.chat([Message(Role.USER, "json")], json_mode=True) == '{"a": 1}'
    assert g.bodies()[1]["response_format"] == {"type": "json_object"}


def test_json_mode_falls_back_when_the_model_rejects_response_format():
    g = Groq((400, {"error": {"message": "response_format json_object is not supported with this model"}}), (200, completion('{"a": 1}')))
    assert g.provider().chat([Message(Role.USER, "x")], json_mode=True) == '{"a": 1}'
    assert "response_format" in g.bodies()[0] and "response_format" not in g.bodies()[1]


# ---- failures ---------------------------------------------------------------------------------------------------------------------------------

def test_missing_key_fails_before_any_request():
    g = Groq()
    with pytest.raises(LLMProviderError, match="Groq API key missing") as e:
        g.provider(key="").chat([Message(Role.USER, "x")])
    assert e.value.kind == "config" and g.requests == []


@pytest.mark.parametrize("status,body,kind,text", [
    (401, {"error": {"message": "Invalid API Key"}}, "auth", "Groq authentication failed"),
    (403, {"error": {"message": "forbidden"}}, "auth", "Groq authentication failed"),
    (404, {"error": {"message": "The model `nope` does not exist"}}, "model", "Groq model unavailable"),
    (400, {"error": {"message": "The model `x` has been decommissioned and is no longer supported"}}, "model", "Groq model unavailable"),
    (400, {"error": {"message": "messages must not be empty"}}, "bad_request", "rejected the request"),
])
def test_classified_failures_are_not_retried_and_never_leak_the_key(status, body, kind, text):
    g = Groq((status, body))
    with pytest.raises(LLMProviderError) as e:
        g.provider().chat([Message(Role.USER, "x")])
    assert e.value.kind == kind and text in str(e.value) and FAKE_KEY not in str(e.value) and len(g.requests) == 1


def test_rate_limit_is_retried_with_retry_after_then_succeeds():
    slept = []
    g = Groq((429, {"error": {"message": "slow down"}}, {"retry-after": "2"}), (200, completion("done")))
    assert g.provider(sleep=slept.append).chat([Message(Role.USER, "x")]) == "done" and slept == [2.0] and len(g.requests) == 2


def test_rate_limit_persisting_raises_after_bounded_retries():
    g = Groq(*[(429, {"error": {"message": "slow"}})] * 5)
    with pytest.raises(LLMProviderError, match="rate limit") as e:
        g.provider(max_retries=2).chat([Message(Role.USER, "x")])
    assert e.value.kind == "rate_limit" and len(g.requests) == 3


def test_server_errors_are_retried():
    g = Groq((503, {"error": {"message": "down"}}), (200, completion("back")))
    assert g.provider().chat([Message(Role.USER, "x")]) == "back"


def test_timeout_and_network_errors_are_classified_and_bounded():
    g = Groq(*[httpx.ReadTimeout("t")] * 3)
    with pytest.raises(LLMProviderError, match="timed out") as e:
        g.provider(max_retries=2).chat([Message(Role.USER, "x")])
    assert e.value.kind == "timeout" and len(g.requests) == 3
    g2 = Groq(*[httpx.ConnectError("no route")] * 2)
    with pytest.raises(LLMProviderError, match="network unavailable") as e2:
        g2.provider(max_retries=1).chat([Message(Role.USER, "x")])
    assert e2.value.kind == "network"


@pytest.mark.parametrize("body", [{}, {"choices": []}, {"choices": [{"message": {}}]}, {"choices": [{"message": {"content": "   "}}]}, ["not", "a", "dict"]])
def test_malformed_or_empty_responses_are_errors_never_fabricated(body):
    with pytest.raises(LLMProviderError) as e:
        Groq((200, body)).provider().chat([Message(Role.USER, "x")])
    assert e.value.kind == "bad_response"


def test_cut_off_reasoning_without_text_is_reported():
    with pytest.raises(LLMProviderError, match="LLM_MAX_TOKENS"):
        Groq((200, {"choices": [{"message": {"content": None, "reasoning": "..."}, "finish_reason": "length"}]})).provider().chat([Message(Role.USER, "x")])


def test_the_key_is_never_in_repr_logs_or_settings_repr(caplog):
    caplog.set_level(logging.DEBUG)
    g = Groq((401, {"error": {"message": f"bad key {FAKE_KEY}"}}))
    p = g.provider()
    assert FAKE_KEY not in repr(p) and "api_key=set" in repr(p)
    with pytest.raises(LLMProviderError) as e:
        p.chat([Message(Role.USER, "x")])
    assert FAKE_KEY not in str(e.value) and FAKE_KEY not in caplog.text
    s = Settings(GROQ_API_KEY=FAKE_KEY)
    assert FAKE_KEY not in repr(s) and FAKE_KEY not in str(s.model_dump()) and s.GROQ_API_KEY.get_secret_value() == FAKE_KEY
    assert FAKE_KEY not in redact(f"key={FAKE_KEY} and {FAKE_KEY}")


# ---- staged health -----------------------------------------------------------------------------------------------------------------------------

def test_health_healthy_with_and_without_inference():
    g = Groq((200, completion("OK")))
    h = g.provider().health()
    assert h.ok and h.configured and h.key_configured and h.reachable and h.authenticated and h.model_available and h.inference is None
    full = g.provider().health(inference=True)
    assert full.inference is True and "inference succeeded" in full.detail
    d = full.to_dict()
    assert d["provider"] == "Groq" and d["model"] == DEFAULT_MODEL and FAKE_KEY not in json.dumps(d)


def test_health_distinguishes_every_failure():
    assert Groq().provider(key="").health().problem == "config"
    bad_key = Groq().provider(key="gsk_wrongwrongwrongwrongwrong0000").health()
    assert bad_key.problem == "auth" and bad_key.reachable and bad_key.authenticated is False
    no_model = Groq(models=("other-model",)).provider().health()
    assert no_model.problem == "model" and no_model.model_available is False and no_model.authenticated
    net = GroqProvider(FAKE_KEY, transport=httpx.MockTransport(lambda r: (_ for _ in ()).throw(httpx.ConnectError("x")))).health()
    assert net.problem == "network" and net.reachable is False
    slow = GroqProvider(FAKE_KEY, transport=httpx.MockTransport(lambda r: (_ for _ in ()).throw(httpx.ReadTimeout("x")))).health()
    assert slow.problem == "timeout"
    infer_fail = Groq((429, {"error": {"message": "limit"}}), (429, {"error": {"message": "limit"}}), (429, {"error": {"message": "limit"}})).provider().health(inference=True)
    assert infer_fail.problem == "rate_limit" and infer_fail.inference is False and infer_fail.authenticated


def test_health_check_states_for_the_monitor():
    from backend.core.health import ServiceState
    from desktop.runtime.health_checks import llm_provider_check

    ok = llm_provider_check(Groq().provider())()
    assert ok.state is ServiceState.HEALTHY and "openai/gpt-oss-20b" in ok.detail and "inference not probed" in ok.detail
    assert llm_provider_check(Groq().provider(key=""))().state is ServiceState.FAILED and "GROQ_API_KEY" in llm_provider_check(Groq().provider(key=""))().detail
    assert llm_provider_check(Groq().provider(key="gsk_wrongwrongwrongwrongwrong0000"))().state is ServiceState.FAILED
    assert llm_provider_check(Groq(models=("x",)).provider())().state is ServiceState.FAILED
    assert llm_provider_check(GroqProvider(FAKE_KEY, transport=httpx.MockTransport(lambda r: (_ for _ in ()).throw(httpx.ConnectError("x")))))().state is ServiceState.DISCONNECTED
    assert llm_provider_check(Groq().provider(), offline=lambda: True)().state is ServiceState.DISABLED


# ---- factory, configuration, secrecy of files --------------------------------------------------------------------------------------------------

def test_factory_and_defaults():
    s = Settings()
    assert (s.LLM_PROVIDER, s.LLM_MODEL, s.GROQ_BASE_URL) == ("groq", "openai/gpt-oss-20b", "https://api.groq.com/openai/v1")
    p = build_llm(Settings(GROQ_API_KEY=FAKE_KEY, LLM_MODEL="llama-3.3-70b-versatile", LLM_MAX_TOKENS=99, LLM_TIMEOUT_SECONDS=7, LLM_MAX_RETRIES=1))
    assert isinstance(p, GroqProvider) and p.model == "llama-3.3-70b-versatile" and p._max_tokens == 99 and p._timeout == 7 and p._max_retries == 1
    from backend.core.llm.ollama_provider import OllamaProvider

    assert isinstance(build_llm(Settings(LLM_PROVIDER="ollama", LLM_MODEL="llama3")), OllamaProvider)          # switching providers is one setting
    with pytest.raises(UnknownProviderError):
        build_llm(Settings(LLM_PROVIDER="mystery"))
    for bad in ({"LLM_MAX_TOKENS": 1}, {"LLM_TEMPERATURE": 5}, {"LLM_TIMEOUT_SECONDS": 0}, {"LLM_MAX_RETRIES": 99}):
        with pytest.raises(Exception):
            Settings(**bad)


def test_env_example_has_placeholders_only_and_env_is_ignored():
    text = (ROOT / ".env.example").read_text(encoding="utf-8")
    assert "LLM_PROVIDER=groq" in text and "LLM_MODEL=openai/gpt-oss-20b" in text and "GROQ_BASE_URL=https://api.groq.com/openai/v1" in text
    assert [l for l in text.splitlines() if l.startswith("GROQ_API_KEY")] == ["GROQ_API_KEY="]
    assert "gsk_" not in text
    assert ".env" in (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()


def test_no_source_or_doc_contains_a_groq_key():
    import re

    pattern = re.compile(r"gsk_[A-Za-z0-9]{20,}")
    for path in list((ROOT / "backend").rglob("*.py")) + list((ROOT / "docs").rglob("*.md")) + [ROOT / "README.md", ROOT / ".env.example"]:
        assert not pattern.search(path.read_text(encoding="utf-8", errors="ignore")), path


def test_ollama_is_not_a_dependency():
    req = (ROOT / "requirements.txt").read_text(encoding="utf-8").lower()
    assert "ollama" not in req and "groq" not in req and "openai" not in req              # httpx (already required) is all Groq needs


# ---- the agent path over Groq ----------------------------------------------------------------------------------------------------------------------

@pytest.fixture
def stack(session_factory):
    from tests.test_task_actions_engine import Stack

    def make(groq: Groq):
        s = Stack(session_factory)
        provider = groq.provider()
        s.agent._llm = s.engine._llm = provider
        return s

    return make


def decision(intent="conversation", response="Hello.", **extra):
    return json.dumps({"intent": intent, "response": response, "summary": "x", "confidence": 0.9, **extra})


def test_conversation_and_history_flow_through_groq(stack):
    g = Groq((200, completion(decision(response="I'm JARVIS, your personal assistant."))), (200, completion(decision(response="You asked who I am."))))
    s = stack(g)
    assert s.say("Who are you?") == "I'm JARVIS, your personal assistant."
    assert s.say("What did I just ask you?") == "You asked who I am."
    second = g.bodies()[1]["messages"]
    assert any(m["role"] == "user" and m["content"] == "Who are you?" for m in second) and any(m["role"] == "assistant" for m in second)   # multi-turn history reached the model
    assert g.bodies()[0]["response_format"] == {"type": "json_object"}                                   # the brain asks for JSON


def test_a_tool_decision_from_groq_runs_through_permissions_and_is_verified(stack):
    action = {"name": "create_task", "arguments": {"title": "finish the JARVIS documentation", "priority": "high", "due": "friday at 5 pm"}}
    g = Groq((200, completion(decision(intent="action_request", response="", tools=["create_task"], action=action))))
    s = stack(g)
    reply = s.say("Create a high priority task to finish the JARVIS documentation by Friday 5 PM.")
    assert reply.startswith("Okay, I've added the task: finish the JARVIS documentation")
    [t] = s.tasks.list_tasks()                                                                           # the task really exists: the reply is not a claim
    assert t.title == "finish the JARVIS documentation" and len(g.requests) == 1


def test_a_hostile_tool_call_is_refused_and_nothing_runs(stack):
    action = {"name": "execute_shell", "arguments": {"command": "del /f /q C:\\*"}}
    g = Groq(*[(200, completion(decision(intent="action_request", response="", tools=["execute_shell"], action=action)))] * 3)
    s = stack(g)
    reply = s.say("Clean up my computer")
    assert "execute_shell" not in reply and not s.tasks.list_tasks()
    assert reply                                                                                          # a safe answer, never an executed action


def test_malformed_model_output_gets_a_safe_fallback_after_bounded_attempts(stack):
    from agent.brain.brain import FALLBACK_RESPONSE, MAX_ATTEMPTS

    g = Groq(*[(200, completion("this is not json at all"))] * 5)
    s = stack(g)
    assert s.say("Hello there") == FALLBACK_RESPONSE and len(g.requests) == MAX_ATTEMPTS


def test_a_tool_argument_validation_failure_does_not_claim_success(stack):
    action = {"name": "create_task", "arguments": {"title": "", "priority": "urgent-ish", "due": 42, "unknown": "x"}}
    g = Groq(*[(200, completion(decision(intent="action_request", response="", tools=["create_task"], action=action)))] * 3)
    s = stack(g)
    reply = s.say("make a task")
    assert not s.tasks.list_tasks() and "added the task" not in reply.lower()


@pytest.mark.parametrize("status,kind", [(401, "auth"), (404, "model"), (429, "rate_limit"), (503, "server")])
def test_provider_failures_surface_as_errors_and_leave_the_conversation_clean(stack, status, kind):
    g = Groq(*[(status, {"error": {"message": "model not found" if status == 404 else "err"}})] * 4)
    s = stack(g)
    with pytest.raises(LLMProviderError) as e:
        s.say("Hello")
    assert e.value.kind == kind and s.engine.session is None                                             # no half-made session; the runtime keeps listening


def test_the_runtime_survives_and_labels_an_llm_provider_failure():
    src = (ROOT / "desktop" / "runtime" / "manager.py").read_text(encoding="utf-8")
    assert "except LLMProviderError as exc:" in src and "LLM provider error [" in src


# ---- time: read from the clock, not from the model ------------------------------------------------------------------------------------------------

def test_what_time_is_it_uses_the_clock_and_never_calls_the_model(tmp_path):
    from tests.intelligence_helpers import build_harness

    h = build_harness(tmp_path)                                    # fixed clock: Thursday 2026-09-24 09:00 IST
    assert h.say("What time is it?") == "It's 9:00 AM."
    assert h.say("what's the time") == "It's 9:00 AM."
    assert h.say("What's the date today?") == "Today is Thursday, September 24, 2026."
    assert h.say("What day is it?") == "Today is Thursday, September 24, 2026."
    assert h.say("what time is the meeting") is None               # not a clock question: left to the normal paths
    h.clock.set(h.clock().replace(hour=13, minute=5))
    assert "PM" in h.say("what time is it")


def test_voice_stack_uses_the_configured_provider():
    src = (ROOT / "voice" / "bootstrap.py").read_text(encoding="utf-8")
    assert "build_llm(settings)" in src and "OllamaProvider" not in src
