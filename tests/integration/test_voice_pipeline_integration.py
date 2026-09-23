"""Integration tests: exercise real providers against real local backends.

These are skipped automatically unless the required model files and
services are actually present/reachable — they never fabricate a pass.
Run manually once you've completed the setup in docs/voice-system.md:

    pytest tests/integration -v

For the full hardware loop (microphone + speaker + "Hey JARVIS"), see the
manual end-to-end test procedure in docs/voice-system.md — that cannot be
automated in CI/a sandbox.
"""

from pathlib import Path

import httpx
import pytest

from backend.core.config import get_settings


def _ollama_reachable(base_url: str) -> bool:
    try:
        httpx.get(f"{base_url.rstrip('/')}/api/tags", timeout=2.0)
        return True
    except httpx.HTTPError:
        return False


@pytest.mark.integration
def test_ollama_generate_real_response():
    settings = get_settings()
    if not _ollama_reachable(settings.OLLAMA_BASE_URL):
        pytest.skip(f"Ollama not reachable at {settings.OLLAMA_BASE_URL}")

    from backend.core.llm.ollama_provider import OllamaProvider

    provider = OllamaProvider(base_url=settings.OLLAMA_BASE_URL, model=settings.LLM_MODEL)
    result = provider.generate("Reply with exactly the word: OK")
    assert isinstance(result, str) and result.strip() != ""


@pytest.mark.integration
def test_wakeword_model_loads_if_configured():
    settings = get_settings()
    if not settings.WAKE_WORD_MODEL_PATH or not Path(settings.WAKE_WORD_MODEL_PATH).is_file():
        pytest.skip("WAKE_WORD_MODEL_PATH not configured or file missing")

    from voice.wakeword.openwakeword_provider import OpenWakeWordProvider

    provider = OpenWakeWordProvider(
        model_path=settings.WAKE_WORD_MODEL_PATH, threshold=settings.WAKE_WORD_THRESHOLD
    )
    assert provider.is_ready()


@pytest.mark.integration
def test_piper_model_loads_if_configured():
    settings = get_settings()
    if not settings.TTS_MODEL_PATH or not Path(settings.TTS_MODEL_PATH).is_file():
        pytest.skip("TTS_MODEL_PATH not configured or file missing")

    from voice.tts.piper_provider import PiperProvider

    provider = PiperProvider(model_path=settings.TTS_MODEL_PATH)
    assert provider.is_ready()
    audio, sample_rate = provider.synthesize("Yes?")
    assert len(audio) > 0
    assert sample_rate > 0


@pytest.mark.integration
def test_real_ollama_multi_turn_conversation_in_one_session():
    settings = get_settings()
    if not _ollama_reachable(settings.OLLAMA_BASE_URL):
        pytest.skip(f"Ollama not reachable at {settings.OLLAMA_BASE_URL}")

    from backend.core.conversation.engine import ConversationEngine
    from backend.core.llm.ollama_provider import OllamaProvider

    engine = ConversationEngine(
        OllamaProvider(base_url=settings.OLLAMA_BASE_URL, model=settings.LLM_MODEL),
        max_messages=settings.JARVIS_MAX_CONVERSATION_MESSAGES,
        timeout_seconds=settings.JARVIS_CONVERSATION_TIMEOUT_SECONDS,
    )
    first = engine.respond("What is Python?")
    session_id = engine.session.session_id
    second = engine.respond("Who created it?")

    assert first.strip() and second.strip()
    assert engine.session.session_id == session_id
    assert len(engine.session.messages) == 4


@pytest.mark.integration
def test_real_ollama_agent_brain_classifies_info_and_action_requests():
    settings = get_settings()
    if not _ollama_reachable(settings.OLLAMA_BASE_URL):
        pytest.skip(f"Ollama not reachable at {settings.OLLAMA_BASE_URL}")

    from agent.brain.brain import AgentBrain
    from agent.brain.models import Intent
    from backend.core.llm.ollama_provider import OllamaProvider

    llm = OllamaProvider(base_url=settings.OLLAMA_BASE_URL, model=settings.LLM_MODEL)
    brain = AgentBrain(llm, tools=[], max_plan_steps=settings.JARVIS_AGENT_MAX_PLAN_STEPS)

    info = brain.decide(brain.build_request("What is Python?", []))
    assert info.intent in (Intent.INFORMATION_REQUEST, Intent.CONVERSATION)
    assert info.action_required is False and info.error is None

    action = brain.decide(brain.build_request("Send an email to John saying hello.", []))
    assert action.intent is Intent.ACTION_REQUEST
    assert action.action_required and action.requires_permission
    assert action.plan is not None  # described only; nothing was sent
