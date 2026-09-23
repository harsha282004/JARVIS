"""Unit tests for the VoiceEngine state machine, using fake providers.

No real audio hardware, wake-word model, Whisper model, Ollama server, or
Piper model is used here — these are pure state-machine/wiring tests.
Real-provider behavior is covered by manual end-to-end testing
(docs/voice-system.md).
"""

import numpy as np
import pytest

from backend.core.conversation.engine import ConversationEngine
from backend.core.conversation.prompts import SYSTEM_PROMPT
from backend.core.llm.base import LLMProvider, LLMProviderError
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
    """Returns each scripted utterance in turn, then "" (silence)."""

    def __init__(self, *texts: str):
        self._texts = list(texts)

    def is_ready(self) -> bool:
        return True

    def transcribe(self, audio: np.ndarray, sample_rate: int) -> str:
        return self._texts.pop(0) if self._texts else ""


class FakeLLM(LLMProvider):
    def __init__(self, response: str = "I don't have access to a calendar yet."):
        self._response = response
        self.requests = []

    def chat(self, messages):
        self.requests.append(list(messages))
        return self._response


class FailingLLM(LLMProvider):
    def chat(self, messages):
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


def _conversation(llm=None):
    return ConversationEngine(llm=llm or FakeLLM(), max_messages=20, timeout_seconds=120)


def _make_engine(llm=None, stt_texts=("what is today's date",)):
    return VoiceEngine(
        wakeword=FakeWakeWord(trigger_on_call=1),
        stt=FakeSTT(*stt_texts),
        conversation=_conversation(llm),
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
    system, user = llm.requests[0]
    assert user.content == "what is today's date"
    # The system prompt must disclaim missing personal-data access.
    assert system.content == SYSTEM_PROMPT
    assert "calendar" in system.content.lower()
    assert "memory" in system.content.lower()


def test_run_once_speaks_activation_reply_then_response():
    tts = FakeTTS()
    engine = VoiceEngine(
        wakeword=FakeWakeWord(trigger_on_call=1),
        stt=FakeSTT("a question"),
        conversation=_conversation(FakeLLM(response="final answer")),
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
    engine = _make_engine(stt_texts=())

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


def test_follow_up_turn_needs_no_wake_word_and_shares_context():
    llm = FakeLLM(response="answer")
    engine = _make_engine(llm=llm, stt_texts=("What is Python?", "Who created it?"))

    response = engine.run_once()

    assert response == "answer"
    assert engine.state == VoiceState.WAITING
    assert len(llm.requests) == 2
    second = [(m.role.value, m.content) for m in llm.requests[1]]
    assert second[1:] == [
        ("user", "What is Python?"),
        ("assistant", "answer"),
        ("user", "Who created it?"),
    ]
    assert engine._wakeword._calls == 1  # wake word heard once, not per turn


def test_activation_ends_on_silent_follow_up_but_context_survives_for_next_wake():
    llm = FakeLLM(response="answer")
    engine = _make_engine(llm=llm, stt_texts=("Tell me about Bengaluru.",))
    engine.run_once()  # turn, then a silent follow-up window ends the activation

    engine._wakeword = FakeWakeWord(trigger_on_call=1)
    engine._stt = FakeSTT("What is its population?")
    engine.run_once()

    second = [m.content for m in llm.requests[1]]
    assert "Tell me about Bengaluru." in second
    assert second[-1] == "What is its population?"


def test_follow_up_stops_when_conversation_is_no_longer_active():
    llm = FakeLLM()
    engine = _make_engine(llm=llm, stt_texts=("one", "two"))
    engine._conversation.reset()
    original = engine._conversation.respond

    def respond_then_reset(text):
        reply = original(text)
        engine._conversation.reset()  # e.g. session ended
        return reply

    engine._conversation.respond = respond_then_reset
    engine.run_once()

    assert len(llm.requests) == 1  # no follow-up window after the session ended


def test_stop_request_between_turns_ends_activation():
    llm = FakeLLM()
    engine = _make_engine(llm=llm, stt_texts=("one", "two"))
    calls = {"n": 0}

    def should_stop():
        calls["n"] += 1
        return calls["n"] > 1  # not while waiting for wake word, yes after the first turn

    engine.run_once(should_stop=should_stop)
    assert len(llm.requests) == 1
