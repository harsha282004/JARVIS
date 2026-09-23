"""Unit tests for the VoiceEngine state machine, using fake providers.

No real audio hardware, wake-word model, Whisper model, Ollama server, or
Piper model is used here — these are pure state-machine/wiring tests.
Real-provider behavior is covered by manual end-to-end testing
(docs/voice-system.md).
"""

import numpy as np
import pytest

from backend.core.llm.base import LLMProviderError
from voice.engine import VoiceEngine, VoiceState


class FakeAudioInput:
    is_open = False

    def __init__(self, wake_after: int = 1):
        self.sample_rate = 16000
        self._reads = 0
        self._wake_after = wake_after
        self.opened = False
        self.closed = False

    def __enter__(self):
        self.opened = True
        return self

    def __exit__(self, *exc_info):
        self.closed = True

    def read_frame(self) -> np.ndarray:
        self._reads += 1
        return np.zeros(1280, dtype=np.int16)

    def frames(self, duration_seconds: float):
        for _ in range(3):
            yield np.zeros(1280, dtype=np.int16)


class FakeWakeWord:
    def __init__(self, trigger_on_call: int = 1):
        self._calls = 0
        self._trigger_on_call = trigger_on_call

    def is_ready(self) -> bool:
        return True

    @property
    def frame_samples(self) -> int:
        return 1280

    def process(self, frame: np.ndarray) -> bool:
        self._calls += 1
        return self._calls >= self._trigger_on_call


class FakeSTT:
    def __init__(self, text: str = "what is today's date"):
        self._text = text

    def is_ready(self) -> bool:
        return True

    def transcribe(self, audio: np.ndarray, sample_rate: int) -> str:
        return self._text


class FakeLLM:
    def __init__(self, response: str = "I don't have access to a calendar yet."):
        self._response = response
        self.last_prompt = None
        self.last_system = None

    def generate(self, prompt: str, system: str | None = None, **kwargs) -> str:
        self.last_prompt = prompt
        self.last_system = system
        return self._response


class FailingLLM:
    def generate(self, prompt: str, system: str | None = None, **kwargs) -> str:
        raise LLMProviderError("Ollama unreachable")


class FakeTTS:
    def __init__(self):
        self.spoken = []

    def is_ready(self) -> bool:
        return True

    def synthesize(self, text: str):
        self.spoken.append(text)
        return np.zeros(10, dtype=np.float32), 22050


class FakeAudioOutput:
    def __init__(self):
        self.played = []

    def play(self, samples, sample_rate):
        self.played.append((samples, sample_rate))


def _make_engine(llm=None, stt_text="what is today's date"):
    return VoiceEngine(
        wakeword=FakeWakeWord(trigger_on_call=1),
        stt=FakeSTT(text=stt_text),
        llm=llm or FakeLLM(),
        tts=FakeTTS(),
        audio_input=FakeAudioInput(),
        audio_output=FakeAudioOutput(),
        sample_rate=16000,
        listen_seconds=1.0,
    )


def test_engine_starts_in_waiting_state():
    engine = _make_engine()
    assert engine.state == VoiceState.WAITING


def test_run_once_full_cycle_returns_to_waiting():
    llm = FakeLLM(response="I don't have calendar access yet.")
    engine = _make_engine(llm=llm)

    response = engine.run_once()

    assert response == "I don't have calendar access yet."
    assert engine.state == VoiceState.WAITING
    assert llm.last_prompt == "what is today's date"
    # The system prompt must disclaim missing personal-data access.
    assert "calendar" in llm.last_system.lower()
    assert "memory" in llm.last_system.lower()


def test_run_once_speaks_activation_reply_then_response():
    tts = FakeTTS()
    engine = VoiceEngine(
        wakeword=FakeWakeWord(trigger_on_call=1),
        stt=FakeSTT(),
        llm=FakeLLM(response="final answer"),
        tts=tts,
        audio_input=FakeAudioInput(),
        audio_output=FakeAudioOutput(),
        sample_rate=16000,
        listen_seconds=1.0,
        activation_reply="Yes?",
    )

    engine.run_once()

    assert tts.spoken == ["Yes?", "final answer"]


def test_run_once_skips_llm_when_transcript_empty():
    engine = _make_engine(stt_text="")

    result = engine.run_once()

    assert result is None
    assert engine.state == VoiceState.WAITING


def test_run_once_propagates_llm_failure_and_resets_state():
    engine = _make_engine(llm=FailingLLM())

    with pytest.raises(LLMProviderError):
        engine.run_once()

    assert engine.state == VoiceState.WAITING


def test_run_forever_recovers_from_provider_error(monkeypatch):
    engine = _make_engine(llm=FailingLLM())
    calls = {"n": 0}

    def fake_run_once():
        calls["n"] += 1
        if calls["n"] >= 2:
            raise KeyboardInterrupt
        raise LLMProviderError("boom")

    monkeypatch.setattr(engine, "run_once", fake_run_once)

    with pytest.raises(KeyboardInterrupt):
        engine.run_forever()

    assert calls["n"] == 2


def test_run_once_returns_without_cycle_when_stop_requested():
    engine = _make_engine()
    engine._wakeword = FakeWakeWord(trigger_on_call=10_000)  # never wakes

    assert engine.run_once(should_stop=lambda: True) is None
    assert engine.state == VoiceState.WAITING
    assert engine._audio_input.closed  # microphone released on stop


def test_microphone_active_reflects_audio_input():
    engine = _make_engine()
    engine._audio_input.is_open = True
    assert engine.microphone_active is True
