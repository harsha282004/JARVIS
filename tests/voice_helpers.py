"""Fakes for the Phase 19 voice tests: a scripted microphone, recognizer, wake word, speaker and synthesizer.

No audio hardware, model or network is used. Frames are synthetic (a sine burst is "speech", zeros are "silence").
"""

import numpy as np

from voice.exceptions import AudioDeviceError, VoiceProviderError
from voice.stt.base import Transcription

RATE = 16000
FRAME = 1280


def speech(n: int = 1, level: float = 0.2) -> list[np.ndarray]:
    t = np.arange(FRAME) / RATE
    tone = (np.sin(2 * np.pi * 220 * t) * level * 32767).astype(np.int16)
    return [tone.copy() for _ in range(n)]


def silence(n: int = 1) -> list[np.ndarray]:
    return [np.zeros(FRAME, dtype=np.int16) for _ in range(n)]


UTTERANCE = speech(6) + silence(16)  # ~0.5 s of speech then > 1 s of quiet: one complete utterance


class ScriptedMic:
    """An AudioInput stand-in. `script` items are frames or exceptions to raise; when it runs out it returns silence forever."""

    def __init__(self, *script, open_failures: list[Exception] | None = None):
        self._script = list(script)
        self._open_failures = list(open_failures or [])
        self.is_open = False
        self.opens = 0
        self.closes = 0
        self.reads = 0

    def push(self, *items) -> None:
        self._script.extend(items)

    def __enter__(self):
        if self._open_failures:
            raise self._open_failures.pop(0)
        self.is_open = True
        self.opens += 1
        return self

    def __exit__(self, *exc_info):
        self.is_open = False
        self.closes += 1

    def read_frame(self):
        self.reads += 1
        if self._script:
            item = self._script.pop(0)
            if isinstance(item, Exception):
                raise item
            return item
        return silence(1)[0]

    def frames(self, seconds):
        for _ in range(3):
            yield silence(1)[0]


class ScriptedWake:
    """Fires on the n-th call (default the first); `fires_on` may list several call numbers."""

    def __init__(self, *fires_on: int):
        self.fires_on = set(fires_on or (1,))
        self.calls = 0
        self.resets = 0
        self.threshold = 0.5

    def is_ready(self):
        return True

    frame_samples = FRAME

    def process(self, frame):
        self.calls += 1
        return self.calls in self.fires_on

    def set_threshold(self, value):
        self.threshold = value

    def reset(self):
        self.resets += 1


class ScriptedSTT:
    """Returns each scripted result in turn ((text, confidence) or an Exception), then an empty transcript."""

    def __init__(self, *results):
        self._results = list(results)
        self.heard: list[np.ndarray] = []

    def is_ready(self):
        return True

    def transcribe(self, audio, rate):
        return self.transcribe_detailed(audio, rate).text

    def transcribe_detailed(self, audio, rate):
        self.heard.append(audio)
        if not self._results:
            return Transcription("", None, len(audio) / rate)
        item = self._results.pop(0)
        if isinstance(item, Exception):
            raise item
        text, confidence = item if isinstance(item, tuple) else (item, 0.95)
        return Transcription(text, confidence, len(audio) / rate)


class RecordingTTS:
    def __init__(self, fail: Exception | None = None, samples: int = 10):
        self.spoken: list[str] = []
        self.fail = fail
        self.samples = samples
        self.speed = 1.0

    def is_ready(self):
        return True

    def synthesize(self, text):
        if self.fail is not None:
            raise self.fail
        self.spoken.append(text)
        return np.zeros(self.samples, dtype=np.float32), 22050

    def set_speed(self, speed):
        self.speed = speed


class ScriptedOutput:
    """A speaker whose clip stays 'playing' for `polls` calls to is_playing (so barge-in has time to happen)."""

    def __init__(self, polls: int = 0, poll_sleep: float = 0.0):
        self.polls = polls
        self.poll_sleep = poll_sleep
        self._left = 0
        self.started: list[int] = []
        self.stopped = 0
        self.played: list[int] = []
        self.volume = 1.0

    def start(self, samples, rate):
        self.started.append(len(samples))
        self._left = self.polls

    @property
    def is_playing(self):
        if self.poll_sleep:
            import time

            time.sleep(self.poll_sleep)
        if self._left > 0:
            self._left -= 1
            return True
        return False

    def stop(self):
        self.stopped += 1
        self._left = 0

    def play(self, samples, rate):
        self.played.append(len(samples))


class FakeConversation:
    """A ConversationEngine stand-in that records what reached the agent."""

    def __init__(self, reply="Okay.", awaiting=None):
        self.received: list[str] = []
        self.reply = reply
        self.awaiting = awaiting
        self.cancelled = 0
        self.is_active = True
        self.last_decision = None
        self.last_action_name = None

    def respond(self, text):
        self.received.append(text)
        return self.reply(text) if callable(self.reply) else self.reply

    def awaiting_answer(self):
        return self.awaiting

    def cancel_pending(self):
        self.cancelled += 1
        was, self.awaiting = self.awaiting is not None, None
        return was


def mic_gone():
    return AudioDeviceError("device unplugged")


def stt_crash():
    return VoiceProviderError("model crashed")
