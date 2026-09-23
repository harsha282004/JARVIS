"""VoiceEngine: wires wake word -> STT -> LLM -> TTS into one wake-word cycle.

Intentionally minimal — a single-turn state machine, not a conversation
engine. There is no persisted conversation history or session state:
Phase 3 will add that. JARVIS has no memory, Gmail, Calendar, messaging,
or personal RAG yet, so the system prompt tells the LLM not to claim it
does.
"""

from collections.abc import Callable

import numpy as np

from backend.core.llm.base import LLMProvider, LLMProviderError
from backend.core.logging import get_logger
from voice.audio import AudioInput, AudioOutput
from voice.exceptions import VoiceProviderError
from voice.stt.base import STTProvider
from voice.tts.base import TTSProvider
from voice.wakeword.base import WakeWordProvider

logger = get_logger(__name__)

SYSTEM_PROMPT = (
    "You are JARVIS, a local voice assistant running entirely on the "
    "user's own machine. You do NOT have access to the user's email, "
    "calendar, messages, files, tasks, reminders, or any personal memory "
    "— those integrations do not exist yet. If asked about any of them, "
    "say plainly that you don't have that capability yet. Keep answers "
    "short (1-2 sentences) since they will be read aloud."
)


class VoiceState:
    WAITING = "waiting"
    LISTENING = "listening"
    TRANSCRIBING = "transcribing"
    THINKING = "thinking"
    SPEAKING = "speaking"


class VoiceEngine:
    """Drives one wake-word -> response cycle at a time."""

    def __init__(
        self,
        wakeword: WakeWordProvider,
        stt: STTProvider,
        llm: LLMProvider,
        tts: TTSProvider,
        audio_input: AudioInput,
        audio_output: AudioOutput,
        sample_rate: int,
        listen_seconds: float,
        activation_reply: str = "Yes?",
    ):
        self._wakeword = wakeword
        self._stt = stt
        self._llm = llm
        self._tts = tts
        self._audio_input = audio_input
        self._audio_output = audio_output
        self._sample_rate = sample_rate
        self._listen_seconds = listen_seconds
        self._activation_reply = activation_reply
        self.state = VoiceState.WAITING

    def _speak(self, text: str) -> None:
        self.state = VoiceState.SPEAKING
        logger.info("TTS_STARTED text=%r", text)
        samples, sample_rate = self._tts.synthesize(text)
        self._audio_output.play(samples, sample_rate)
        logger.info("TTS_COMPLETED")

    @property
    def microphone_active(self) -> bool:
        """True while the microphone stream is open."""
        return self._audio_input.is_open

    def run_once(self, should_stop: Callable[[], bool] | None = None) -> str | None:
        """Wait for the wake word, handle exactly one utterance, then return
        to WAITING. Returns the LLM's response text, or None if nothing
        intelligible was transcribed.

        `should_stop` is a lifecycle hook (used by the Windows runtime): it is
        polled while waiting for the wake word, and if it returns True the
        microphone is released and this returns None without a cycle."""
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

            frames = list(self._audio_input.frames(self._listen_seconds))

        utterance = np.concatenate(frames) if frames else np.array([], dtype=np.int16)

        self.state = VoiceState.TRANSCRIBING
        logger.info("STT_STARTED")
        text = self._stt.transcribe(utterance, self._sample_rate)
        logger.info("STT_COMPLETED length=%d", len(text))

        if not text:
            logger.info("STT produced no text; skipping LLM/TTS this cycle")
            self.state = VoiceState.WAITING
            return None

        self.state = VoiceState.THINKING
        logger.info("LLM_REQUEST_STARTED")
        try:
            response = self._llm.generate(text, system=SYSTEM_PROMPT)
        except LLMProviderError as exc:
            logger.error("LLM request failed: %s", exc)
            self.state = VoiceState.WAITING
            raise
        logger.info("LLM_RESPONSE_RECEIVED length=%d", len(response))

        self._speak(response)

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
