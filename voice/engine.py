"""VoiceEngine: wires wake word -> STT -> conversation -> TTS.

One `run_once()` call is one activation: wait for the wake word, then hold a
conversation for as long as the user keeps speaking. Hardware (microphone,
speaker), wake word, STT and TTS live here; conversation state (session,
history, context, timeout) lives in `ConversationEngine`, which this class
calls with plain text and never the reverse.

Voice states are unchanged: WAITING -> LISTENING -> TRANSCRIBING -> THINKING
-> SPEAKING, with LISTENING/TRANSCRIBING/THINKING/SPEAKING repeating for
follow-up turns before returning to WAITING.
"""

from collections.abc import Callable

import numpy as np

from backend.core.conversation.engine import ConversationEngine
from backend.core.llm.base import LLMProviderError
from backend.core.logging import get_logger
from voice.audio import AudioInput, AudioOutput
from voice.exceptions import VoiceProviderError
from voice.stt.base import STTProvider
from voice.tts.base import TTSProvider
from voice.wakeword.base import WakeWordProvider

logger = get_logger(__name__)


class VoiceState:
    WAITING = "waiting"
    LISTENING = "listening"
    TRANSCRIBING = "transcribing"
    THINKING = "thinking"
    SPEAKING = "speaking"


class VoiceEngine:
    """Drives wake-word activations through a multi-turn spoken conversation."""

    def __init__(
        self,
        wakeword: WakeWordProvider,
        stt: STTProvider,
        conversation: ConversationEngine,
        tts: TTSProvider,
        audio_input: AudioInput,
        audio_output: AudioOutput,
        sample_rate: int,
        listen_seconds: float,
        activation_reply: str = "Yes?",
    ):
        self._wakeword = wakeword
        self._stt = stt
        self._conversation = conversation
        self._tts = tts
        self._audio_input = audio_input
        self._audio_output = audio_output
        self._sample_rate = sample_rate
        self._listen_seconds = listen_seconds
        self._activation_reply = activation_reply
        self.state = VoiceState.WAITING

    @property
    def microphone_active(self) -> bool:
        """True while the microphone stream is open."""
        return self._audio_input.is_open

    def _speak(self, text: str) -> None:
        self.state = VoiceState.SPEAKING
        logger.info("TTS_STARTED length=%d", len(text))
        samples, sample_rate = self._tts.synthesize(text)
        self._audio_output.play(samples, sample_rate)
        logger.info("TTS_COMPLETED")

    def _transcribe(self, utterance: np.ndarray) -> str:
        self.state = VoiceState.TRANSCRIBING
        logger.info("STT_STARTED")
        text = self._stt.transcribe(utterance, self._sample_rate)
        logger.info("STT_COMPLETED length=%d", len(text))
        return text

    def _capture(self) -> np.ndarray:
        """Record one utterance window from the (already open) microphone."""
        frames = list(self._audio_input.frames(self._listen_seconds))
        return np.concatenate(frames) if frames else np.array([], dtype=np.int16)

    def _think(self, text: str) -> str:
        self.state = VoiceState.THINKING
        logger.info("LLM_REQUEST_STARTED")
        try:
            response = self._conversation.respond(text)
        except LLMProviderError as exc:
            logger.error("LLM request failed: %s", exc)
            self.state = VoiceState.WAITING
            raise
        logger.info("LLM_RESPONSE_RECEIVED length=%d", len(response))
        return response

    def run_once(self, should_stop: Callable[[], bool] | None = None) -> str | None:
        """Wait for the wake word, then converse until the user stops talking,
        the conversation times out, or `should_stop` is set. Returns the last
        assistant reply, or None if nothing intelligible was heard.

        `should_stop` is a lifecycle hook (used by the Windows runtime): it is
        polled while waiting for the wake word and between turns. If it fires
        while waiting, the microphone is released and this returns None.
        """
        logger.info("VOICE_ENGINE_STARTED state=%s", self.state)
        self.state = VoiceState.WAITING

        with self._audio_input:
            while True:
                if should_stop is not None and should_stop():
                    self.state = VoiceState.WAITING
                    return None
                frame = self._audio_input.read_frame()
                if self._wakeword.process(frame):
                    logger.info("WAKE_WORD_DETECTED")
                    break

            self.state = VoiceState.LISTENING
            logger.info("LISTENING_STARTED duration_s=%s", self._listen_seconds)
            self._speak(self._activation_reply)
            utterance = self._capture()

        response: str | None = None
        while True:
            text = self._transcribe(utterance)
            if not text:
                logger.info("No speech recognized; ending this activation")
                break

            response = self._think(text)
            self._speak(response)

            if (should_stop is not None and should_stop()) or not self._conversation.is_active:
                break

            # Follow-up turn: listen again without requiring the wake word.
            self.state = VoiceState.LISTENING
            logger.info("LISTENING_STARTED follow_up=true duration_s=%s", self._listen_seconds)
            with self._audio_input:
                utterance = self._capture()

        self.state = VoiceState.WAITING
        logger.info("VOICE_ENGINE_STOPPED state=%s", self.state)
        return response

    def run_forever(self) -> None:
        while True:
            try:
                self.run_once()
            except (VoiceProviderError, LLMProviderError) as exc:
                logger.error("Voice cycle failed, returning to WAITING: %s", exc)
                self.state = VoiceState.WAITING
