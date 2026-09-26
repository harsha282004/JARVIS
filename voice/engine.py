"""VoiceEngine: the single voice pipeline.

    Microphone -> (wake word) -> speech capture by VAD -> STT -> normalisation / control words
        -> ConversationEngine (agent, tool router, permission manager, confirmations) -> response policy -> TTS -> speakers

One `run_once()` call is one activation: wait for the wake word (or the tray's "Talk to JARVIS"), then hold a conversation for as
long as the user keeps talking (active conversation mode: follow-ups need no wake word until the inactivity timeout). Hardware
(microphone, speaker), wake word, STT and TTS live here; conversation state (session, history, pending confirmations, context)
lives in `ConversationEngine` and the intelligence layer, which this class calls with plain text and never the reverse.

What the voice layer adds on top of the providers:
  * end of speech is detected (VAD), not a fixed window; the microphone is reacquired if the device disappears;
  * "Stop" / "Cancel" / "Wait" (with or without "JARVIS") are control words: they stop speech or drop a pending question and are
    never sent to the agent as tasks; speech can be interrupted while JARVIS is talking (wake word, optional VAD, tray/dashboard);
  * a low-confidence transcript can never answer a pending confirmation;
  * long answers are summarised for the ear (the full text stays in the status/dashboard); TTS is sentence-queued and cancellable;
  * Do Not Disturb, mute and priorities decide whether an announcement is spoken; critical alerts may interrupt a conversation;
  * every failure (mic, wake word, STT, TTS, LLM) is recovered locally and reported truthfully, never as fabricated output.

Voice states stay WAITING -> LISTENING -> TRANSCRIBING -> THINKING -> SPEAKING (see `voice.status` for the dashboard vocabulary).
Audio exists only in memory for one utterance; it is never written to disk or logged.
"""

import re
import threading
import time
from collections.abc import Callable
from contextlib import ExitStack

import numpy as np

from agent.tasks.notifications import AnnouncementQueue
from backend.core.conversation.engine import ConversationEngine
from backend.core.llm.base import LLMProviderError
from backend.core.logging import get_logger
from backend.core.metrics import metrics
from voice.audio import AudioInput, AudioOutput
from voice.exceptions import AudioDeviceError, VoiceProviderError
from voice.normalize import Control, control_of, normalize
from voice.policy import VoicePolicy, clean_for_speech, spoken_version, split_sentences
from voice.settings import VoiceSettings, VoiceSettingsStore
from voice.status import TTS, Mic, VoiceLog, VoiceStatus, new_session_id
from voice.stt.base import STTProvider, Transcription
from voice.tts.base import TTSProvider
from voice.vad import EnergyVAD, UtteranceDetector, UtteranceStatus, frame_level
from voice.wake import WakeConfig, WakeEvent, WakeGate, confirm_phrase, is_sleep_command
from voice.wakeword.base import WakeWordProvider

logger = get_logger(__name__)

STT_UNAVAILABLE = "I can't process speech right now. You can still use the dashboard."
LLM_UNAVAILABLE = "I can't reach my language model right now. I can still help with reminders, your calendar and your email."
MIC_LOST = "I lost the microphone. I'll keep trying to reconnect."
DIDNT_CATCH = "Sorry, I didn't catch that."
LOW_CONFIDENCE_CONFIRM = "I wasn't sure I heard that. Please say yes or no again."
ACK_STOPPED = "Okay."
ACK_CANCELLED = "Okay, cancelled."
ACK_WAIT = "Sure, take your time."
ACK_TASK_STOPPED = "Okay, I stopped the task."
ACK_SLEEP = "Going to sleep."
_REPEAT = re.compile(r"^(?:jarvis[, ]+)?(?:please )?(?:repeat that|say that again|what did you say|say it again|come again|repeat)(?: please)?[.?!]*$", re.I)
_YES_NO_ONLY = re.compile(r"^(?:yes|yeah|yep|yup|sure|ok|okay|no|nope|nah|confirm|do it|go ahead)[.!]*$", re.I)


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
        announcements: AnnouncementQueue | None = None,
        *,
        settings: VoiceSettingsStore | None = None,
        use_vad: bool = False,
        policy: VoicePolicy | None = None,
        status: VoiceStatus | None = None,
        log: VoiceLog | None = None,
        mic_retry_seconds: float = 2.0,
        barge_in_grace_seconds: float = 0.3,
        sleep: Callable[[float], None] = time.sleep,
        wake: WakeConfig | None = None,
        clock: Callable[[], float] = time.monotonic,
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
        self._announcements = announcements
        self._store = settings
        self._use_vad = use_vad
        self._policy = policy
        self.status = status or VoiceStatus()
        self._log = log or VoiceLog(None)
        self._mic_retry = mic_retry_seconds
        self._grace = barge_in_grace_seconds
        self._sleep = sleep
        self.state = VoiceState.WAITING
        self._manual_wake = threading.Event()
        self._interrupt = threading.Event()
        self._session_id = new_session_id()
        self._last_response: str | None = None
        self._manual_activation = False
        self._clock = clock
        self._wake_cfg = wake or WakeConfig()
        self._gate = WakeGate(self._wake_cfg, clock)
        self._ring: list[np.ndarray] = []
        self._last_wake: WakeEvent | None = None
        self._manual_wake_at = 0.0
        self._idle_audio = 0.0
        self._last_capture_seconds = 0.0
        self._vad = EnergyVAD(self._settings().speech_threshold)
        self.status.update(wake_ready=self._safe_ready(wakeword), stt_ready=self._safe_ready(stt), tts_ready=self._safe_ready(tts),
                           wake_health="ready" if self._safe_ready(wakeword) else "not_ready")
        if settings is not None:
            settings.add_listener(self.apply_settings)
        self.apply_settings(self._settings())

    # ---- settings ----------------------------------------------------------------------------------------------------------

    def _settings(self) -> VoiceSettings:
        if self._store is not None:
            return self._store.current
        return VoiceSettings(max_utterance_seconds=max(self._listen_seconds, 2.0))

    def apply_settings(self, s: VoiceSettings) -> None:
        """Live-apply what can change while running (sensitivity, speed, volume, VAD threshold). Never raises."""
        try:
            if hasattr(self._wakeword, "set_threshold"):
                self._wakeword.set_threshold(s.wake_sensitivity)
            if hasattr(self._tts, "set_speed"):
                self._tts.set_speed(s.tts_speed)
            if hasattr(self._audio_output, "volume"):
                self._audio_output.volume = s.tts_volume
            self._vad.threshold = s.speech_threshold
        except Exception:  # noqa: BLE001 - a bad setting must not stop the voice loop
            logger.exception("Applying voice settings failed")

    @staticmethod
    def _safe_ready(provider) -> bool:
        try:
            return bool(provider.is_ready())
        except Exception:  # noqa: BLE001
            return False

    @property
    def _wake_block_until(self) -> float:
        """End of the current refractory window (debounce, post-speech, post-rejection). One mechanism for every wake source."""
        return self._gate._blocked_until

    @_wake_block_until.setter
    def _wake_block_until(self, value: float) -> None:
        self._gate._blocked_until = value

    # ---- external control (tray / dashboard / API; any thread) ---------------------------------------------------------------

    def request_activation(self) -> None:
        """"Talk to JARVIS" from the tray: behaves like hearing the wake word, on the next audio frame. It does nothing while
        the runtime is paused or private (the engine is not listening then), so it can never open a closed microphone. The request expires
        (WakeConfig.manual_ttl_seconds): one that nobody consumed is dropped, never replayed later as a surprise "Yes?"."""
        self._manual_wake_at = self._clock()
        self._manual_wake.set()

    def interrupt(self) -> None:
        """Stop speaking now (tray "Stop speaking", dashboard). Harmless when nothing is being said or about to be said: it is
        remembered only while JARVIS is speaking or working on an answer, so a stray click while idle cannot silence the next reminder."""
        if self.state in (VoiceState.SPEAKING, VoiceState.THINKING, VoiceState.TRANSCRIBING):
            self._interrupt.set()
        stop = getattr(self._audio_output, "stop", None)
        if callable(stop):
            stop()

    @property
    def microphone_active(self) -> bool:
        """True while the microphone stream is open."""
        return self._audio_input.is_open

    def _set_state(self, state: str) -> None:
        self.state = state
        self.status.update(voice_state=state)

    # ---- microphone --------------------------------------------------------------------------------------------------------

    def _acquire_mic(self, stack: ExitStack, should_stop: Callable[[], bool] | None) -> bool:
        """Open the microphone, retrying while it is missing (unplugged, in use, permission). False if asked to stop meanwhile."""
        announced = False
        while True:
            try:
                stack.enter_context(self._audio_input)
            except AudioDeviceError as exc:
                denied = "permission" in str(exc).lower() or "access" in str(exc).lower() or "denied" in str(exc).lower()
                self.status.update(mic=Mic.PERMISSION_DENIED if denied else Mic.DISCONNECTED,
                                   last_error="Microphone permission denied" if denied else "Microphone unavailable")
                if not announced:
                    logger.error("MICROPHONE_%s (%s)", "PERMISSION_DENIED" if denied else "DISCONNECTED", type(exc).__name__)
                    self._log.event("microphone_unavailable", session_id=self._session_id, state="waiting", result="permission denied" if denied else "unavailable")
                    announced = True
                if should_stop is not None and should_stop():
                    return False
                self._sleep(self._mic_retry)
                continue
            if announced:
                logger.info("MICROPHONE_RECONNECTED")
                self._log.event("microphone_reconnected", session_id=self._session_id, state="waiting")
            self.status.update(mic=Mic.CONNECTED, last_error=None)
            return True

    def _release_mic(self, stack: ExitStack, lost: bool) -> None:
        try:
            stack.close()
        except Exception:  # noqa: BLE001 - closing a vanished device can raise; it is closed either way
            logger.warning("Microphone close raised; treating it as closed")
        self.status.update(mic=Mic.DISCONNECTED if lost else Mic.CLOSED)

    # ---- speech ------------------------------------------------------------------------------------------------------------

    def _speak(self, text: str, *, kind: str = "response", monitor: bool = True) -> bool:
        """Say `text` (sentence by sentence). Returns True if it was interrupted. A failure to synthesize or play is recorded
        and reported on the status (the text is still on the dashboard); it never crashes the voice loop."""
        s = self._settings()
        spoken, shortened = spoken_version(text, s.spoken_max_chars) if kind == "response" else (clean_for_speech(text), False)
        if not spoken:
            return False
        if s.voice_muted:
            logger.info("TTS_SKIPPED muted")
            self.status.update(tts=TTS.IDLE)
            return False
        self._set_state(VoiceState.SPEAKING)
        logger.info("TTS_STARTED length=%d kind=%s shortened=%s", len(spoken), kind, shortened)
        interrupted = False
        started = time.perf_counter()
        first = True
        try:
            for sentence in split_sentences(spoken) or [spoken]:
                if self._interrupt.is_set():
                    interrupted = True
                    break
                self.status.update(tts=TTS.GENERATING)
                with metrics.timer("tts_synthesis_ms"):
                    samples, rate = self._tts.synthesize(sentence)
                self.status.update(tts=TTS.SPEAKING)
                if first:
                    first = False
                    self.status.set_latency("time_to_first_audio_ms", (time.perf_counter() - started) * 1000)
                if self._play(samples, rate, monitor):
                    interrupted = True
                    break
        except (VoiceProviderError, AudioDeviceError, OSError, RuntimeError) as exc:
            logger.error("TTS_FAILED (%s)", type(exc).__name__)
            self.status.update(tts=TTS.ERROR, last_error=f"Speech output failed ({type(exc).__name__}); the answer is on the dashboard")
            self._log.event("tts_failed", session_id=self._session_id, state="speaking", result=type(exc).__name__)
            return False
        finally:
            self._interrupt.clear() if interrupted else None
            self._gate.block(self._wake_cfg.post_tts_block_seconds)     # JARVIS's own voice in the microphone must never wake JARVIS
        self.status.update(tts=TTS.INTERRUPTED if interrupted else TTS.IDLE)
        if interrupted:
            self.status.update(interruptions=self.status.interruptions + 1)
            metrics.incr("voice.interruptions")
            self._log.event("interrupted", session_id=self._session_id, state="speaking")
        logger.info("TTS_%s", "INTERRUPTED" if interrupted else "COMPLETED")
        return interrupted

    def _play(self, samples: np.ndarray, rate: int, monitor: bool) -> bool:
        """Play one clip. While it plays, listen for a barge-in and stop at once. True if interrupted."""
        out = self._audio_output
        mode = self._settings().barge_in
        if not monitor or mode == "off" or not hasattr(out, "start") or not self._audio_input.is_open:
            out.play(samples, rate)
            return self._interrupt.is_set()
        out.start(samples, rate)
        began = time.monotonic()
        loud = 0
        try:
            while out.is_playing:
                if self._interrupt.is_set():
                    out.stop()
                    return True
                try:
                    frame = self._audio_input.read_frame()
                except AudioDeviceError:
                    self._sleep(0.02)  # the microphone glitched; keep speaking, the main loop will reacquire it
                    continue
                if time.monotonic() - began < self._grace:  # the first instant of playback is our own voice in the microphone
                    continue
                heard = self._strong_wake(frame)
                if mode == "vad":
                    loud = loud + 1 if frame_level(frame) >= self._settings().barge_in_threshold else 0
                    heard = heard or loud >= 3
                if heard:
                    out.stop()
                    self._log.event("barge_in", session_id=self._session_id, state="speaking", result=mode)
                    return True
        finally:
            if self._interrupt.is_set():
                out.stop()
        return False

    def _score(self) -> float | None:
        score = getattr(self._wakeword, "last_score", None)
        return float(score) if isinstance(score, (int, float)) and not isinstance(score, bool) else None

    def _strong_wake(self, frame: np.ndarray) -> bool:
        """The wake word while JARVIS is talking (barge-in): only a strong, sustained score interrupts, so the speaker's own echo cannot."""
        heard = self._wakeword.process(frame)
        score = self._score()
        if score is None:
            return bool(heard)
        if score >= self._wake_cfg.direct_threshold:
            self._bargein_run = getattr(self, "_bargein_run", 0) + 1
        else:
            self._bargein_run = 0
        return self._bargein_run >= max(1, self._wake_cfg.min_frames)

    # ---- listening ---------------------------------------------------------------------------------------------------------

    def _capture(self, no_speech_seconds: float, should_stop: Callable[[], bool] | None = None) -> tuple[np.ndarray, str, float | None]:
        """Record one utterance. Returns (audio, status, seconds until speech began).
        With VAD the utterance ends after `silence_seconds` of quiet; without it, a fixed window (the original behavior)."""
        self._last_capture_seconds = 0.0
        if not self._use_vad:
            frames = list(self._audio_input.frames(self._listen_seconds))
            audio = np.concatenate(frames) if frames else np.array([], dtype=np.int16)
            self._last_capture_seconds = max(len(audio) / self._sample_rate, self._listen_seconds)
            return audio, UtteranceStatus.COMPLETE.value, None
        s = self._settings()
        detector = UtteranceDetector(self._vad, self._sample_rate, s.silence_seconds, s.max_utterance_seconds, s.min_utterance_seconds, no_speech_seconds)
        while not detector.finished:
            if should_stop is not None and should_stop():
                return np.array([], dtype=np.int16), "stopped", None  # shutting down: what was heard so far is dropped, not acted on
            detector.push(self._audio_input.read_frame())
        result = detector.result()
        self._last_capture_seconds = result.total_seconds
        if result.speech_started_after is not None:
            self.status.set_latency("speech_detection_ms", result.speech_started_after * 1000)
        return result.audio, result.status.value, result.speech_started_after

    def _transcribe(self, utterance: np.ndarray) -> Transcription | None:
        """Speech to text. None if recognition itself failed (already reported)."""
        self._set_state(VoiceState.TRANSCRIBING)
        logger.info("STT_STARTED")
        started = time.perf_counter()
        try:
            with metrics.timer("stt_ms"):
                result = self._stt.transcribe_detailed(utterance, self._sample_rate) if hasattr(self._stt, "transcribe_detailed") else Transcription(
                    self._stt.transcribe(utterance, self._sample_rate))
        except Exception as exc:  # noqa: BLE001 - any recognizer failure is a degraded mode, not a crash
            logger.error("STT_FAILED (%s)", type(exc).__name__)
            self.status.update(stt_ready=False, last_error=f"Speech recognition failed ({type(exc).__name__})")
            self._log.event("stt_failed", session_id=self._session_id, state="transcribing", result=type(exc).__name__)
            return None
        elapsed = (time.perf_counter() - started) * 1000
        self.status.update(stt_ready=True, last_confidence=result.confidence)
        self.status.set_latency("stt_ms", elapsed)
        logger.info("STT_COMPLETED length=%d", len(result.text))
        # Transcription record: text, confidence, duration, failure. Never the audio.
        self._log.event("transcription", session_id=self._session_id, state="transcribing", transcription=result.text,
                        latency_ms=elapsed, result="empty" if not result.text else "ok",
                        confidence=result.confidence, audio_seconds=round(result.audio_seconds, 2))
        return result

    def _think(self, text: str) -> str:
        self._set_state(VoiceState.THINKING)
        logger.info("LLM_REQUEST_STARTED")
        started = time.perf_counter()
        try:
            with metrics.timer("conversation_ms"):  # the agent, its tools and any LLM calls for this turn
                response = self._conversation.respond(text)
        except LLMProviderError as exc:
            logger.error("LLM request failed: %s", exc)
            self.status.update(last_error="Language model unavailable")
            self._log.event("llm_failed", session_id=self._session_id, state="thinking", transcription=text, result="LLMProviderError")
            self._set_state(VoiceState.WAITING)
            raise
        elapsed = (time.perf_counter() - started) * 1000
        self.status.set_latency("agent_ms", elapsed)
        logger.info("LLM_RESPONSE_RECEIVED length=%d", len(response))
        self._log.event("turn", session_id=self._session_id, state="thinking", transcription=text, latency_ms=elapsed,
                        intent=self._intent(), tool=self._tool(), result=response)
        return response

    def _intent(self) -> str:
        decision = getattr(self._conversation, "last_decision", None)
        intent = getattr(decision, "intent", None)
        return str(getattr(intent, "value", intent)) if intent is not None else "deterministic"

    def _tool(self) -> str | None:
        return getattr(self._conversation, "last_action_name", None)

    # ---- announcements -----------------------------------------------------------------------------------------------------

    def _speak_announcements(self, *, minimum: str | None = None) -> None:
        """Speak queued announcements (reminders, alerts). Called only on this (the voice) thread while waiting for the
        wake word, so nothing else touches the audio devices and no conversation is interrupted. Do Not Disturb, mute and the
        notification switch hold non-critical ones back (kept on the status, never discarded silently); a critical alert can pass
        Do Not Disturb if the user allowed it. With `minimum` (mid-conversation) only announcements at that priority or above are taken."""
        if self._announcements is None:
            return
        spoke = False
        while True:
            if minimum is not None and not self._announcements.has_at_least(minimum):
                break
            item = self._announcements.get_item_nowait()
            if item is None:
                break
            allowed, reason = self._policy.may_speak(item.priority) if self._policy is not None else (True, "no policy")
            if not allowed:
                self.status.hold(item.text, item.priority, reason)
                self._log.event("announcement_held", session_id=self._session_id, state="waiting", result=f"{item.priority}: {reason}")
                continue
            spoke = True
            try:
                self._speak(item.text, kind="announcement")
                self._log.event("announcement", session_id=self._session_id, state="speaking", result=item.priority)
            except Exception as exc:  # noqa: BLE001 - a failed announcement must not stop listening
                logger.error("Announcement could not be spoken (%s)", type(exc).__name__)
        if spoke:
            self._set_state(VoiceState.WAITING)

    # ---- one activation ----------------------------------------------------------------------------------------------------

    def run_once(self, should_stop: Callable[[], bool] | None = None) -> str | None:
        """Wait for the wake word, then converse until the user stops talking,
        the conversation times out, or `should_stop` is set. Returns the last
        assistant reply, or None if nothing intelligible was heard.

        `should_stop` is a lifecycle hook (used by the Windows runtime): it is
        polled while waiting for the wake word and between turns. If it fires
        while waiting, the microphone is released and this returns None.
        """
        if self._announcements is not None:
            self._announcements.set_accepting(True)
        try:
            return self._run_once(should_stop)
        finally:
            if self._announcements is not None:
                self._announcements.set_accepting(False)

    def _wait_for_wake(self, stack: ExitStack, should_stop: Callable[[], bool] | None) -> bool:
        """True when a VALIDATED wake happened (recorded in self._last_wake), False when asked to stop. Only three things activate JARVIS: the wake-word model with a
        sustained strong score, a weaker candidate whose short STT check is exactly "hey jarvis"/"jarvis", or a fresh tray/API request. Nothing else (noise, other
        speech, the microphone opening or reconnecting, empty transcripts, scheduler ticks) can lead to the acknowledgement."""
        self._last_wake = None
        self._ring = []
        ring_max = max(3, int(self._wake_cfg.confirm_window_seconds * self._sample_rate / 1280))
        while True:
            if should_stop is not None and should_stop():
                return False
            self._speak_announcements()
            try:
                frame = self._audio_input.read_frame()
            except AudioDeviceError as exc:
                logger.error("MICROPHONE_DISCONNECTED (%s)", type(exc).__name__)
                self._release_mic(stack, lost=True)
                self.status.update(last_error="Microphone disconnected")
                if not self._acquire_mic(stack, should_stop):
                    return False
                self._ring = []                                      # a reconnect is not a wake: start clean (only a validated wake ever speaks)
                continue
            self._ring.append(frame)
            del self._ring[:-ring_max]
            if self._manual_wake.is_set():
                self._manual_wake.clear()
                age = self._clock() - self._manual_wake_at
                if age > self._wake_cfg.manual_ttl_seconds:
                    logger.info("STALE_MANUAL_ACTIVATION_DROPPED age_s=%.1f", age)
                    self._log.event("wake_rejected", session_id=self._session_id, state="waiting", result="stale manual activation dropped", age_s=round(age, 1))
                    continue
                self._manual_activation = True
                logger.info("MANUAL_ACTIVATION")
                self._last_wake = WakeEvent("manual", None, self._wakeword_threshold(), "manual", "waiting", False, 0.0)
                return True
            if self._gate.blocked():
                continue  # refractory: debounce, the tail of the last utterance, JARVIS's own voice
            heard = self._wakeword.process(frame)
            score = self._score()
            if score is None:                                        # a provider without scores (tests/other engines): its own decision, still debounced above
                if heard:
                    self._manual_activation = False
                    logger.info("WAKE_WORD_DETECTED")
                    self._last_wake = WakeEvent("legacy_model", None, self._wakeword_threshold(), "hey jarvis", "waiting", False, 0.0)
                    return True
                continue
            decision = self._gate.observe(score, self._wakeword_threshold())
            if decision.action == "accept":
                self._manual_activation = False
                logger.info("WAKE_WORD_DETECTED score=%.2f", score)
                self._last_wake = WakeEvent(decision.source, score, self._wakeword_threshold(), "hey jarvis", "waiting", False, 0.0)
                return True
            if decision.action == "candidate":
                event = self._confirm_wake(decision.score, decision.strong)
                if event is not None:
                    self._manual_activation = False
                    self._last_wake = event
                    return True

    def _wakeword_threshold(self) -> float:
        value = getattr(self._wakeword, "threshold", None)
        return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else float(self._settings().wake_sensitivity)

    def _confirm_wake(self, score: float, strong: bool = False) -> WakeEvent | None:
        """Second stage for a candidate: a short local STT check of the last ~2 s (plus a short tail) that must be EXACTLY "hey jarvis" or "jarvis". The audio stays in memory
        for this call only; nothing is stored or sent, and a rejected transcript is logged by length only."""
        tail = max(0, int((self._wake_cfg.strong_tail_seconds if strong else self._wake_cfg.confirm_tail_seconds) * self._sample_rate / 1280))
        for _ in range(tail):
            try:
                self._ring.append(self._audio_input.read_frame())
            except AudioDeviceError:
                break
        audio = np.concatenate(self._ring) if self._ring else np.array([], dtype=np.int16)
        self._ring = []
        reset = getattr(self._wakeword, "reset", None)
        if callable(reset):
            reset()
        started = time.perf_counter()
        ok, phrase, why = False, "", "speech recognition unavailable"
        if audio.size and self._safe_ready(self._stt):
            try:
                result = self._stt.transcribe_detailed(audio, self._sample_rate) if hasattr(self._stt, "transcribe_detailed") else Transcription(
                    self._stt.transcribe(audio, self._sample_rate))
                ok, phrase, why = confirm_phrase(result.text)
                length = len(result.text)
            except Exception as exc:  # noqa: BLE001 - a failed check is a rejection, never an activation
                why, length = f"speech recognition failed ({type(exc).__name__})", 0
        else:
            length = 0
        elapsed = (time.perf_counter() - started) * 1000
        if ok:
            self._log.event("wake_confirmed", session_id=self._session_id, state="waiting", result=phrase, latency_ms=elapsed, score=round(score, 2), stt_confirmation=True)
            return WakeEvent("stt_confirmed", score, self._wakeword_threshold(), phrase, "waiting", True, 0.0)
        self._gate.note_rejection()
        self.status.update(wake_rejections=self.status.wake_rejections + 1)
        logger.info("WAKE_CANDIDATE_REJECTED score=%.2f reason=%s", score, why)
        self._log.event("wake_rejected", session_id=self._session_id, state="waiting", result=why, latency_ms=elapsed, score=round(score, 2), transcript_chars=length, stt_confirmation=True)
        return None

    def _run_once(self, should_stop: Callable[[], bool] | None) -> str | None:
        logger.info("VOICE_ENGINE_STARTED state=%s", self.state)
        self._set_state(VoiceState.WAITING)
        self._session_id = new_session_id()
        stack = ExitStack()
        response: str | None = None
        try:
            if not self._acquire_mic(stack, should_stop):
                return None
            if not self._wait_for_wake(stack, should_stop):
                self._set_state(VoiceState.WAITING)
                return None

            reset = getattr(self._wakeword, "reset", None)
            if callable(reset):
                reset()
            wake = self._last_wake
            if wake is None:                                          # cannot happen; if it ever did, nothing may be spoken
                logger.error("ACTIVATION_WITHOUT_VALIDATED_WAKE ignored")
                return None
            self._gate.note_activation()
            self._idle_audio = 0.0
            self.status.update(activations=self.status.activations + 1, last_activation_at=_utc_iso(), session_state="active", sleep_reason=None,
                               last_wake={"source": wake.source, "score": None if wake.score is None else round(wake.score, 2), "threshold": round(wake.threshold, 2), "phrase": wake.phrase,
                                          "stt_confirmed": wake.stt_confirmed, "at": _utc_iso()})
            self._log.event("wake", session_id=self._session_id, state="listening", result=wake.phrase, source=wake.source, score=None if wake.score is None else round(wake.score, 2),
                            threshold=round(wake.threshold, 2), state_before=wake.state_before, stt_confirmation=wake.stt_confirmed, debounce_s=self._wake_cfg.debounce_seconds)
            self._set_state(VoiceState.LISTENING)
            logger.info("LISTENING_STARTED duration_s=%s", self._listen_seconds)
            self._interrupt.clear()
            with metrics.timer("wake_to_prompt_ms"):  # wake word heard -> "Yes?" has been synthesized and played
                self._speak(self._activation_reply, kind="prompt", monitor=False)
            try:
                response = self._converse(stack, should_stop)
            except AudioDeviceError as exc:
                logger.error("MICROPHONE_LOST_DURING_CONVERSATION (%s)", type(exc).__name__)
                self._release_mic(stack, lost=True)
                self.status.update(last_error="Microphone disconnected")
                self._log.event("microphone_lost", session_id=self._session_id, state="listening")
                self._speak(MIC_LOST, kind="prompt", monitor=False)
        finally:
            if self.status.mic in (Mic.DISCONNECTED, Mic.PERMISSION_DENIED):
                stack.close()  # already reported as unavailable; keep that status until the next activation reacquires it
            else:
                self._release_mic(stack, lost=False)
            self._wake_block_until = max(self._wake_block_until, self._clock() + max(1.0, self._wake_cfg.debounce_seconds))
            if self.status.session_state == "active":
                self.status.update(session_state="asleep")
            self.status.update(conversation_active=False, pending_action=None)
            self._set_state(VoiceState.WAITING)
        logger.info("VOICE_ENGINE_STOPPED state=%s", self.state)
        return response

    def _sleep_session(self, reason: str) -> None:
        """End the active conversation: no further follow-up listening until the next wake phrase. Nothing is sent to the language model or any tool."""
        self.status.update(session_state="asleep", sleep_reason=reason, conversation_active=False, pending_action=None)
        self._log.event("session_sleep", session_id=self._session_id, state="listening", result=reason)
        logger.info("VOICE_SESSION_SLEEP reason=%s", reason)
        if reason == "command":                                      # an explicit "sleep" also closes the conversation context; a timeout leaves it to expire on its own clock
            try:
                self._conversation.reset()
            except Exception:  # noqa: BLE001 - closing the conversation context is best effort
                pass

    def _converse(self, stack: ExitStack, should_stop: Callable[[], bool] | None) -> str | None:
        """One wake-to-sleep conversation. The follow-up window is the session timeout (default 120 s): it is measured in AUDIO time since the last meaningful interaction,
        and only a validated utterance (speech that transcribes to real text with acceptable confidence) resets it: ambient noise, empty transcripts and low-confidence
        speech do not. When it runs out the session ends silently (nothing is spoken because of a timeout)."""
        s = self._settings()
        timeout = s.conversation_timeout_seconds                     # the session timeout (VOICE_SESSION_TIMEOUT_SECONDS, default 120 s; adjustable live)
        response: str | None = None
        first = True
        misses = 0
        self._idle_audio = 0.0
        utterance, status, _ = self._capture(8.0, should_stop)
        while True:
            if status == "stopped":
                break
            self._idle_audio += max(self._last_capture_seconds, 0.5)      # time always advances: a capture that reports nothing can never loop forever
            silent = status in (UtteranceStatus.NO_SPEECH.value, UtteranceStatus.TOO_SHORT.value) or utterance.size == 0
            if silent:
                if first:
                    self._note_false_activation()
                    logger.info("No speech captured (%s); ending this activation", status)
                    break
                if status == UtteranceStatus.NO_SPEECH.value or self._idle_audio >= timeout:
                    self._sleep_session("timeout")
                    break
                utterance, status, _ = self._capture(max(1.0, timeout - self._idle_audio), should_stop)   # a noise blip: the timer keeps running
                continue
            heard = self._transcribe(utterance)
            if heard is None:  # recognizer failed: tell the user, keep the dashboard path
                self._speak(STT_UNAVAILABLE, kind="prompt", monitor=False)
                break
            awaiting = self._conversation.awaiting_answer() if hasattr(self._conversation, "awaiting_answer") else None
            low_confidence = (not first and heard.confidence is not None and heard.confidence < s.stt_min_confidence and control_of(heard.text) is Control.NONE
                              and not is_sleep_command(heard.text) and awaiting != "confirmation")   # a doubtful yes/no is handled by the confirmation guard, not dropped
            if not heard.text or low_confidence:
                if first:
                    self._note_false_activation()
                    logger.info("No speech recognized; ending this activation")
                    break
                self._log.event("ambient_ignored", session_id=self._session_id, state="listening", result="empty or low-confidence speech did not reset the session timer")
                if self._idle_audio >= timeout:
                    self._sleep_session("timeout")
                    break
                utterance, status, _ = self._capture(max(1.0, timeout - self._idle_audio), should_stop)
                continue
            first = False
            self._idle_audio = 0.0                                    # a validated interaction restarts the inactivity timer
            self.status.update(last_transcription=heard.text, conversation_active=True)
            text = heard.text
            if self._wake_cfg.sleep_command_enabled and is_sleep_command(text):
                self._interrupt.clear()
                self.interrupt()
                cancel = getattr(self._conversation, "cancel_pending", None)
                if callable(cancel):
                    cancel()
                self._interrupt.clear()
                self._speak(ACK_SLEEP, kind="prompt", monitor=False)
                self._sleep_session("command")
                break
            verdict = self._handle_control(text)
            if verdict == "end":
                break
            if verdict in ("listen", "wait"):
                self._interrupt.clear()
                utterance, status, _ = self._capture(15.0 if verdict == "wait" else timeout, should_stop)
                continue
            clean = normalize(text)
            if not clean.text:
                utterance, status, _ = self._capture(timeout, should_stop)
                misses += 1
                if misses > 2:
                    break
                continue
            if _REPEAT.match(text.strip()) and self._last_response:
                self._speak(self._last_response)
                response = self._last_response
            else:
                guard = self._confirmation_guard(clean.text, heard)
                if guard is not None:
                    self._speak(guard, kind="prompt", monitor=False)
                    self._interrupt.clear()
                    utterance, status, _ = self._capture(timeout, should_stop)
                    continue
                turn_started = time.perf_counter()
                try:
                    response = self._think(clean.text)
                except LLMProviderError:
                    self._speak(LLM_UNAVAILABLE, kind="prompt", monitor=False)
                    raise
                self._last_response = response
                self.status.update(last_response=response, pending_action=self._conversation.awaiting_answer()
                                   if hasattr(self._conversation, "awaiting_answer") else None)
                interrupted = self._speak(response)
                self.status.set_latency("end_to_end_ms", (time.perf_counter() - turn_started) * 1000)
                self._interrupt.clear()
                if interrupted:
                    # Barge-in: the user spoke over JARVIS. What they said is the next input ("Stop", or a new request).
                    utterance, status, _ = self._capture(6.0, should_stop)
                    continue
            self._speak_announcements(minimum="critical")
            if (should_stop is not None and should_stop()) or not self._conversation.is_active:
                break
            # Active conversation: listen for a follow-up without the wake word until the inactivity timeout.
            self._set_state(VoiceState.LISTENING)
            self._idle_audio = 0.0                                    # the timer starts when JARVIS has finished answering
            logger.info("LISTENING_STARTED follow_up=true timeout_s=%s", timeout)
            utterance, status, _ = self._capture(timeout, should_stop)
        return response

    def _handle_control(self, text: str) -> str | None:
        """Stop / Cancel / Wait are for the voice layer, never tasks. Returns None (not a control), "end" (finish the activation),
        "listen" (keep listening) or "wait" (the user asked for a moment)."""
        control = control_of(text)
        if control is Control.NONE:
            return None
        self._log.event("control", session_id=self._session_id, state="listening", intent=control.value, transcription=text)
        stopped = bool(hasattr(self._conversation, "cancel_task") and self._conversation.cancel_task())  # an autonomous task in progress is cancelled first
        if control is Control.STOP:
            self.interrupt()
            self._interrupt.clear()
            if stopped:
                self._speak(ACK_TASK_STOPPED, kind="prompt", monitor=False)
            return "listen"  # nothing is speaking now; JARVIS goes quiet and stays available for the follow-up window
        if control is Control.CANCEL:
            dropped = self._conversation.cancel_pending() if hasattr(self._conversation, "cancel_pending") else False
            self.status.update(pending_action=None)
            self._speak(ACK_TASK_STOPPED if stopped else ACK_CANCELLED if dropped else ACK_STOPPED, kind="prompt", monitor=False)
            return "listen"
        self._speak(ACK_WAIT, kind="prompt", monitor=False)
        return "wait"

    def _confirmation_guard(self, text: str, heard: Transcription) -> str | None:
        """A yes/no that the recognizer was unsure of must not authorize (or refuse) a pending action."""
        awaiting = self._conversation.awaiting_answer() if hasattr(self._conversation, "awaiting_answer") else None
        if awaiting != "confirmation" or not _YES_NO_ONLY.match(text.strip()):
            return None
        if heard.confidence is not None and heard.confidence < self._settings().stt_min_confidence:
            self._log.event("low_confidence_confirmation", session_id=self._session_id, state="listening", result=f"{heard.confidence:.2f}")
            return LOW_CONFIDENCE_CONFIRM
        return None

    def _note_false_activation(self) -> None:
        if self._manual_activation:
            return  # the user asked from the tray/dashboard and then said nothing: that is not the wake word misfiring
        self.status.update(false_activations=self.status.false_activations + 1)
        acts, false = self.status.activations, self.status.false_activations
        health = "ready"
        if acts >= 5 and false / acts > 0.5:
            health = "noisy: many activations without speech; lower the wake-word sensitivity"
        self.status.update(wake_health=health)
        self._log.event("false_activation", session_id=self._session_id, state="listening", result=f"{false}/{acts}")

    def run_forever(self) -> None:
        while True:
            try:
                self.run_once()
            except (VoiceProviderError, LLMProviderError) as exc:
                logger.error("Voice cycle failed, returning to WAITING: %s", exc)
                self._set_state(VoiceState.WAITING)


def _utc_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat(timespec="seconds")
