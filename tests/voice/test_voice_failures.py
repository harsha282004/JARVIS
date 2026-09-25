"""Failure injection for the voice path: STT failure, TTS failure, LLM failure, manual activation."""

import pytest

from backend.core.conversation.engine import ConversationEngine
from backend.core.llm.base import LLMProvider, LLMProviderError
from tests.test_voice_engine import FakeAudioInput, FakeAudioOutput, FakeLLM, FakeSTT, FakeTTS, FakeWakeWord
from voice.engine import VoiceEngine, VoiceState
from voice.exceptions import VoiceProviderError


def engine(stt=None, tts=None, llm=None, wake=None):
    return VoiceEngine(wakeword=wake or FakeWakeWord(1), stt=stt or FakeSTT("hello"), conversation=ConversationEngine(llm or FakeLLM(), 20, 120),
                       tts=tts or FakeTTS(), audio_input=FakeAudioInput(), audio_output=FakeAudioOutput(), sample_rate=16000, listen_seconds=1.0)


class BrokenSTT(FakeSTT):
    def transcribe(self, audio, sample_rate):
        raise VoiceProviderError("model crashed")


class BrokenTTS(FakeTTS):
    def synthesize(self, text):
        raise VoiceProviderError("audio device gone")


class DownLLM(LLMProvider):
    def chat(self, messages, json_mode=False):
        raise LLMProviderError("Ollama unreachable")


def test_stt_failure_is_recovered_and_reported_not_a_crash():
    """Phase 19: a dead recognizer no longer crashes the worker; JARVIS says so and returns to listening."""
    tts = FakeTTS()
    e = engine(stt=BrokenSTT(), tts=tts)
    assert e.run_once() is None
    assert e.state == VoiceState.WAITING
    assert "You can still use the dashboard" in tts.spoken[-1]
    assert e.status.snapshot()["stt"]["ready"] is False


def test_tts_failure_keeps_the_text_answer_and_reports_it():
    e = engine(tts=BrokenTTS())
    assert e.run_once() == "I don't have access to a calendar yet."  # the answer exists (dashboard), speech failed
    snap = e.status.snapshot()
    assert snap["tts_state"] == "TTS_ERROR" and snap["last_response"] == "I don't have access to a calendar yet."
    assert e.state == VoiceState.WAITING


def test_llm_down_is_reported_and_state_returns_to_waiting():
    e = engine(llm=DownLLM())
    with pytest.raises(LLMProviderError):
        e.run_once()
    assert e.state == VoiceState.WAITING


def test_manual_activation_replaces_the_wake_word_once():
    e = engine(wake=FakeWakeWord(trigger_on_call=10_000))  # the wake word never fires
    e.request_activation()
    assert e.run_once() == "I don't have access to a calendar yet."  # the tray's "Talk to JARVIS" started the conversation
    assert e._manual_wake.is_set() is False  # consumed: it does not trigger again
