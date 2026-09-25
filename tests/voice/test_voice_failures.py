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


def test_stt_failure_raises_a_typed_error_and_run_forever_survives_it():
    e = engine(stt=BrokenSTT())
    with pytest.raises(VoiceProviderError):
        e.run_once()
    calls = {"n": 0}
    real = e.run_once

    def once(*a, **k):
        calls["n"] += 1
        if calls["n"] >= 3:
            raise KeyboardInterrupt  # stop the loop after proving it kept going
        return real(*a, **k)

    e.run_once = once
    with pytest.raises(KeyboardInterrupt):
        e.run_forever()
    assert calls["n"] == 3 and e.state == VoiceState.WAITING  # two failed cycles did not end the loop


def test_tts_failure_is_typed_not_silent():
    with pytest.raises(VoiceProviderError):
        engine(tts=BrokenTTS()).run_once()


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
