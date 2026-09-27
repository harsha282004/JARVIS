"""STT confidence/duration, wake-word sensitivity/health, TTS speed, playback control, health refinement, announcement priority queue,
composition wiring, and an optional real-model pipeline check (skipped when the local models are absent)."""

import math
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from agent.tasks.notifications import AnnouncementQueue, NotificationError, VoiceNotifier, normalize_priority
from backend.core.health import Health, ServiceState
from desktop.runtime.health_checks import voice_pipeline_check
from voice.status import Mic, VoiceStatus
from voice.stt.base import STTProvider, Transcription
from voice.stt.faster_whisper_provider import DEFAULT_PROMPT, FasterWhisperProvider

ROOT = Path(__file__).resolve().parents[2]


# ---- STT ------------------------------------------------------------------------------------------------------------------------------

class FakeModel:
    def __init__(self, segments):
        self.segments, self.kwargs = segments, {}

    def transcribe(self, audio, **kwargs):
        self.kwargs = kwargs
        return iter(self.segments), SimpleNamespace(language="en")


def provider(segments):
    p = FasterWhisperProvider.__new__(FasterWhisperProvider)
    p._model, p._language, p._prompt = FakeModel(segments), "en", DEFAULT_PROMPT
    return p


def seg(text, avg_logprob=-0.1, no_speech_prob=0.02):
    return SimpleNamespace(text=text, avg_logprob=avg_logprob, no_speech_prob=no_speech_prob)


def test_transcription_carries_confidence_duration_and_language():
    p = provider([seg(" what's my schedule "), seg("today")])
    r = p.transcribe_detailed(np.zeros(32000, dtype=np.int16), 16000)
    assert r.text == "what's my schedule today" and r.audio_seconds == pytest.approx(2.0) and r.language == "en"
    assert 0.85 < r.confidence <= 1.0
    assert p.transcribe(np.zeros(16000, dtype=np.int16), 16000) == "what's my schedule today"  # the original text-only API is unchanged


def test_uncertain_speech_has_low_confidence():
    r = provider([seg("yes", avg_logprob=-2.0, no_speech_prob=0.6)]).transcribe_detailed(np.zeros(16000, dtype=np.int16), 16000)
    assert r.confidence < 0.2 and r.confidence == pytest.approx(round((1 - 0.6) * math.exp(-2.0), 3), abs=0.001)


def test_no_segments_means_empty_text_and_no_confidence_never_a_guess():
    r = provider([]).transcribe_detailed(np.zeros(16000, dtype=np.int16), 16000)
    assert r.text == "" and r.confidence is None


def test_the_recognizer_gets_the_command_vocabulary_hint_and_vad_filter():
    p = provider([seg("stop")])
    p.transcribe_detailed(np.zeros(16000, dtype=np.int16), 16000)
    assert p._model.kwargs["initial_prompt"] == DEFAULT_PROMPT and p._model.kwargs["vad_filter"] is True


def test_the_default_stt_wrapper_reports_no_confidence():
    class Plain(STTProvider):
        def is_ready(self):
            return True

        def transcribe(self, audio, sample_rate):
            return "hello"

    r = Plain().transcribe_detailed(np.zeros(8000, dtype=np.int16), 16000)
    assert r == Transcription("hello", None, 0.5)


# ---- wake word / TTS / playback ---------------------------------------------------------------------------------------------------------

def test_wake_word_sensitivity_is_adjustable_and_bounded():
    from voice.wakeword.openwakeword_provider import OpenWakeWordProvider

    p = OpenWakeWordProvider.__new__(OpenWakeWordProvider)
    p._threshold, p._model_name = 0.5, "hey_jarvis_v0.1"
    p._model = SimpleNamespace(predict=lambda frame: {"hey_jarvis_v0.1": 0.6}, reset=lambda: None)
    frame = np.zeros(1280, dtype=np.int16)
    assert p.process(frame) is True and p.last_score == 0.6
    p.set_threshold(0.8)
    assert p.process(frame) is False and p.threshold == 0.8
    p.set_threshold(5)
    assert p.threshold == 0.99
    p.reset()
    assert p.last_score == 0.0


def test_piper_speed_maps_to_length_scale_and_is_bounded(monkeypatch):
    from voice.tts.piper_provider import PiperProvider

    seen = {}

    class Voice:
        def synthesize(self, text, cfg):
            seen["length_scale"] = cfg.length_scale
            return [SimpleNamespace(audio_float_array=np.zeros(10, dtype=np.float32), sample_rate=22050)]

    p = PiperProvider.__new__(PiperProvider)
    p._voice = Voice()
    p.set_speed(1.25)
    p.synthesize("hello")
    assert seen["length_scale"] == pytest.approx(0.8)
    p.set_speed(99)
    assert p.speed == 2.0


def test_audio_output_volume_scales_samples_and_stop_is_safe(monkeypatch):
    import voice.audio as audio

    played = {}
    monkeypatch.setattr(audio.sd, "play", lambda data, samplerate, device: played.update(data=data, rate=samplerate))
    monkeypatch.setattr(audio.sd, "stop", lambda: played.update(stopped=True))
    monkeypatch.setattr(audio.sd, "wait", lambda: None)
    out = audio.AudioOutput(volume=0.5)
    out.play(np.ones(4, dtype=np.float32), 22050)
    assert played["data"].tolist() == [0.5, 0.5, 0.5, 0.5]
    out.stop()
    assert played["stopped"] is True


def test_audio_output_volume_above_one_amplifies_a_quiet_clip():
    """Root cause of the reported "Yes?" volume complaint being investigated: `volume` used to be attenuation-only
    (>= 1.0 was a no-op), so a config value above 1.0 could never make anything louder. A quiet (non-full-scale) clip
    at volume=1.5 must come out louder, not unchanged."""
    import voice.audio as audio

    out = audio.AudioOutput.__new__(audio.AudioOutput)
    out.volume = 1.5
    quiet = np.full(8, 0.2, dtype=np.float32)
    scaled = out._scaled(quiet)
    assert np.all(scaled > quiet)                              # louder than the original...
    assert np.max(np.abs(scaled)) < 1.0                         # ...but nowhere near clipping for a clip this quiet


def test_audio_output_gain_soft_clips_instead_of_distorting_or_being_capped_silently():
    """A gain that would drive an already-loud (near full-scale) clip over 0 dBFS must be rounded off (soft clip),
    never a hard, harsh clip and never silently ignored (the old `>= 0.999: return samples` bug)."""
    import voice.audio as audio

    out = audio.AudioOutput.__new__(audio.AudioOutput)
    out.volume = audio._MAX_GAIN
    loud = np.array([1.0, -1.0, 0.95, -0.95], dtype=np.float32)
    scaled = out._scaled(loud)
    assert np.all(np.abs(scaled) <= 1.0 + 1e-6)                 # never exceeds full scale
    assert np.max(np.abs(scaled)) > 0.9                         # still clearly amplified, not squashed to nothing
    assert not np.array_equal(scaled, loud)                     # the gain was actually applied (not a silent no-op)


def test_audio_output_gain_is_capped_at_max_gain_even_if_a_larger_volume_is_set():
    import voice.audio as audio

    out = audio.AudioOutput.__new__(audio.AudioOutput)
    out.volume = 99.0  # a caller bypassing the settings validator must still be safe
    within_cap = out._scaled(np.full(4, 0.1, dtype=np.float32))
    out.volume = audio._MAX_GAIN
    at_cap = out._scaled(np.full(4, 0.1, dtype=np.float32))
    assert np.allclose(within_cap, at_cap)                      # anything above _MAX_GAIN behaves exactly like the cap


def test_audio_output_int16_gain_path_also_amplifies_and_soft_clips():
    import voice.audio as audio

    out = audio.AudioOutput.__new__(audio.AudioOutput)
    out.volume = 1.8
    quiet = np.full(4, 5000, dtype=np.int16)
    scaled = out._scaled(quiet)
    assert scaled.dtype == np.int16
    assert np.all(scaled > quiet)
    loud = np.full(4, 32000, dtype=np.int16)
    scaled_loud = out._scaled(loud)
    assert np.all(np.abs(scaled_loud.astype(np.int32)) <= 32767)


def test_warm_up_plays_a_brief_near_silent_clip_once_and_is_idempotent():
    """Root cause of the reported "Yes?" volume complaint: Piper already peaks its output near 0 dBFS (measured), so
    the fix is not a gain -- it is that `sd.play()` opens a fresh output stream on first use, and the OS's cold-start
    latency on that first call can swallow much of a 150 ms utterance while a multi-second reply barely notices. This
    proves warm_up() pays that cost once, silently, and never touches the device again after it succeeds."""
    import voice.audio as audio

    class FakeBackend:
        def __init__(self):
            self.play_calls = []

        def play(self, data, samplerate, device):
            self.play_calls.append((len(data), samplerate, device))

        def wait(self):
            pass

    backend = FakeBackend()
    out = audio.AudioOutput(backend=backend)
    out.warm_up()
    out.warm_up()
    out.warm_up()
    assert len(backend.play_calls) == 1                          # idempotent: only the first call actually plays anything
    length, rate, _device = backend.play_calls[0]
    assert 0 < length / rate <= 0.3                               # brief (a few hundred ms at most), never a real delay


def test_warm_up_is_near_silent_and_never_the_configured_volume():
    """The warm-up clip must not be audible on its own and must not go through the configured (possibly amplified)
    gain -- it exists to prime the device, not to be heard."""
    import voice.audio as audio

    played = {}

    class FakeBackend:
        def play(self, data, samplerate, device):
            played["data"] = data

        def wait(self):
            pass

    out = audio.AudioOutput(volume=audio._MAX_GAIN, backend=FakeBackend())
    out.warm_up()
    assert np.max(np.abs(played["data"])) < 0.01                 # near-silent regardless of the configured gain


def test_warm_up_failure_is_swallowed_and_retried_on_a_later_call():
    import voice.audio as audio

    class FlakyBackend:
        def __init__(self):
            self.calls = 0

        def play(self, data, samplerate, device):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("device busy")

        def wait(self):
            pass

    backend = FlakyBackend()
    out = audio.AudioOutput(backend=backend)
    out.warm_up()  # fails, swallowed
    out.warm_up()  # succeeds
    assert backend.calls == 2


def test_pad_utterance_adds_true_silence_before_and_after():
    """Root cause of the "Yes?" clipping complaint being addressed: Piper's own output already peaks at 0 dBFS for
    every utterance measured, including "Yes?" -- this is a playback-path artifact (the first/last few dozen ms of a
    freshly opened or torn-down output stream is the least reliable part of it), not a gain problem. A short run of
    real digital silence around the utterance protects it directly in the buffer handed to the driver."""
    import voice.audio as audio

    samples = np.full(100, 0.5, dtype=np.float32)
    padded = audio.pad_utterance(samples, sample_rate=1000, lead_seconds=0.05, tail_seconds=0.02)
    assert len(padded) == 50 + 100 + 20                              # 50 lead zeros, 100 real samples, 20 tail zeros
    assert np.all(padded[:50] == 0.0) and np.all(padded[-20:] == 0.0)
    assert np.array_equal(padded[50:150], samples)                   # the real audio itself is untouched


def test_pad_utterance_preserves_dtype_and_handles_empty_input():
    import voice.audio as audio

    int16_samples = np.full(10, 1000, dtype=np.int16)
    padded = audio.pad_utterance(int16_samples, sample_rate=1000)
    assert padded.dtype == np.int16
    empty = np.array([], dtype=np.float32)
    assert audio.pad_utterance(empty, sample_rate=1000).size == 0    # nothing to protect; never invents audio


def test_pad_utterance_is_a_no_op_when_both_durations_are_zero():
    import voice.audio as audio

    samples = np.full(5, 0.3, dtype=np.float32)
    assert audio.pad_utterance(samples, sample_rate=1000, lead_seconds=0, tail_seconds=0) is samples


# ---- health ---------------------------------------------------------------------------------------------------------------------------

def test_health_checks_are_refined_by_what_the_engine_observed():
    status = VoiceStatus()
    healthy = lambda: Health(ServiceState.HEALTHY, "ok")  # noqa: E731
    mic = voice_pipeline_check(healthy, status, "microphone")
    assert mic().state is ServiceState.HEALTHY
    status.update(mic=Mic.DISCONNECTED)
    assert mic().state is ServiceState.DEGRADED and "disconnected" in mic().detail
    status.update(mic=Mic.PERMISSION_DENIED)
    assert "permission" in mic().detail
    stt, tts = voice_pipeline_check(healthy, status, "stt"), voice_pipeline_check(healthy, status, "tts")
    assert stt().state is ServiceState.HEALTHY
    status.update(stt_ready=False, tts=("TTS_ERROR"))
    assert stt().state is ServiceState.DEGRADED and tts().state is ServiceState.DEGRADED


def test_a_disabled_check_stays_disabled_whatever_the_engine_says():
    status = VoiceStatus()
    status.update(mic=Mic.DISCONNECTED)
    disabled = voice_pipeline_check(lambda: Health(ServiceState.DISABLED, "private mode"), status, "microphone")
    assert disabled().state is ServiceState.DISABLED


# ---- announcement queue with priorities -------------------------------------------------------------------------------------------------

def test_queue_takes_critical_first_then_oldest_within_a_priority():
    q = AnnouncementQueue()
    q.set_accepting(True)
    for text, pr in (("n1", "normal"), ("c1", "critical"), ("n2", "normal"), ("h1", "high"), ("l1", "low"), ("c2", "critical")):
        q.put(text, pr)
    assert [q.get_nowait() for _ in range(6)] == ["c1", "c2", "h1", "n1", "n2", "l1"]
    assert q.get_nowait() is None


def test_full_queue_makes_room_for_a_more_important_item_only():
    q = AnnouncementQueue(max_size=2)
    q.set_accepting(True)
    assert q.put("l1", "low") and q.put("n1", "normal")
    assert q.put("l2", "low") is False        # not more important than anything queued
    assert q.put("c1", "critical") is True    # displaces the lowest
    assert sorted([q.get_nowait(), q.get_nowait()]) == ["c1", "n1"]


def test_queue_refuses_when_not_accepting_and_reports_priority_floor():
    q = AnnouncementQueue()
    assert q.put("x") is False
    q.set_accepting(True)
    q.put("hello", "high")
    assert q.has_at_least("high") and not q.has_at_least("critical")


def test_priority_mapping_and_notifier_passes_it_through():
    assert [normalize_priority(x) for x in ("IMPORTANT", "CRITICAL", "low", "weird", 2)] == ["high", "critical", "low", "normal", "normal"]
    q = AnnouncementQueue()
    notifier = VoiceNotifier(q)
    with pytest.raises(NotificationError):
        notifier.notify("not running yet", {"priority": "high"})
    q.set_accepting(True)
    notifier.notify("reminder", {"priority": "high"})
    assert q.get_item_nowait().priority == "high"


def test_notification_center_levels_reach_the_voice_queue_with_their_priority(tmp_path):
    """The real composition's `deliver`: NotificationCenter level -> queue priority (Level.CRITICAL is spoken as critical)."""
    from backend.core.notifications import Level

    mapping = {1: "low", 2: "normal", 3: "high", 4: "critical"}
    assert [mapping[int(level)] for level in (Level.LOW, Level.NORMAL, Level.IMPORTANT, Level.CRITICAL)] == ["low", "normal", "high", "critical"]
    source = (ROOT / "desktop" / "runtime" / "composition.py").read_text(encoding="utf-8")
    assert "announcements.put(text, normalize_priority(" in source


def test_reminders_and_proactive_messages_are_queued_with_a_priority():
    assert '"priority": "high"' in (ROOT / "agent" / "tasks" / "scheduler.py").read_text(encoding="utf-8")
    assert '"priority":' in (ROOT / "agent" / "proactive" / "engine.py").read_text(encoding="utf-8")


# ---- composition / config ------------------------------------------------------------------------------------------------------------------

def test_the_launcher_hands_the_voice_control_to_the_engine_the_api_and_the_tray():
    cli = (ROOT / "desktop" / "launcher" / "cli.py").read_text(encoding="utf-8")
    assert "build_voice_engine(settings, task_system, router, bus, services.voice)" in cli and "voice=services.voice" in cli
    comp = (ROOT / "desktop" / "runtime" / "composition.py").read_text(encoding="utf-8")
    assert "build_voice_control(" in comp and "toggle_dnd=" in comp and "stop_speaking=manager.interrupt_speech" in comp


def test_new_voice_settings_have_safe_defaults_and_validation():
    from pydantic import ValidationError

    from backend.core.config import Settings

    s = Settings()
    assert s.VOICE_USE_VAD is True and s.VOICE_SILENCE_SECONDS == 1.0 and s.VOICE_DND_ALLOW_CRITICAL is True
    for bad in ({"VOICE_TTS_SPEED": 9}, {"VOICE_SILENCE_SECONDS": 0}, {"VOICE_TTS_VOLUME": 3}, {"VOICE_CONVERSATION_TIMEOUT_SECONDS": 1}):
        with pytest.raises(ValidationError):
            Settings(**bad)


def test_env_example_documents_every_new_setting():
    text = (ROOT / ".env.example").read_text(encoding="utf-8")
    for name in ("VOICE_USE_VAD", "VOICE_SILENCE_SECONDS", "VOICE_MAX_UTTERANCE_SECONDS", "VOICE_SPEECH_THRESHOLD", "VOICE_CONVERSATION_TIMEOUT_SECONDS",
                 "VOICE_TTS_SPEED", "VOICE_TTS_VOLUME", "VOICE_SPOKEN_MAX_CHARS", "VOICE_DND_ALLOW_CRITICAL"):
        assert name in text, name


def test_runtime_manager_can_interrupt_the_engine():
    from desktop.runtime.manager import RuntimeManager

    class Engine:
        interrupted = 0

        def interrupt(self):
            self.interrupted += 1

    manager = RuntimeManager(lambda: Engine())
    assert manager.interrupt_speech() is False  # no engine yet: honest
    manager._engine = Engine()
    assert manager.interrupt_speech() is True and manager._engine.interrupted == 1


# ---- real local models (skipped where the models are not installed) ---------------------------------------------------------------------

_MODELS = ROOT / "models"
real = pytest.mark.skipif(not (_MODELS / "tts" / "en_US-lessac-medium.onnx").is_file() or not (_MODELS / "wakeword" / "hey_jarvis_v0.1.onnx").is_file(),
                          reason="local Piper/openWakeWord models are not installed")


@real
def test_real_piper_to_vad_to_whisper_pipeline_recognizes_a_command_and_ignores_silence():
    """Real Piper speech -> real VAD -> real Faster-Whisper (the base model; downloaded once). Synthetic speech, not a human speaker."""
    from scripts.voice_real_check import FRAME, RATE, frames_of, resample, to_int16
    from voice.tts.piper_provider import PiperProvider
    from voice.vad import EnergyVAD, UtteranceDetector, UtteranceStatus

    try:
        stt = FasterWhisperProvider("base", "en", "cpu")
    except Exception as exc:  # noqa: BLE001 - offline and the model was never downloaded
        pytest.skip(f"Whisper model unavailable: {exc}")
    tts = PiperProvider(str(_MODELS / "tts" / "en_US-lessac-medium.onnx"))
    samples, rate = tts.synthesize("What's my schedule today?")
    stream = np.concatenate([np.zeros(FRAME * 8, dtype=np.int16), to_int16(resample(samples, rate)), np.zeros(FRAME * 30, dtype=np.int16)])
    det = UtteranceDetector(EnergyVAD(0.015), RATE, 1.0, 15.0, 0.15, 6.0)
    for f in frames_of(stream):
        if det.push(f) not in (UtteranceStatus.WAITING, UtteranceStatus.SPEAKING):
            break
    assert det.status is UtteranceStatus.COMPLETE
    heard = stt.transcribe_detailed(det.result().audio, RATE)
    assert "schedule" in heard.text.lower() and heard.confidence and heard.confidence > 0.3
    assert stt.transcribe_detailed(np.zeros(FRAME * 30, dtype=np.int16), RATE).text == ""  # silence never becomes words


@real
def test_real_wake_word_model_fires_on_hey_jarvis_and_not_on_silence():
    from scripts.voice_real_check import FRAME, frames_of, resample, to_int16
    from voice.tts.piper_provider import PiperProvider
    from voice.wakeword.openwakeword_provider import OpenWakeWordProvider

    try:
        wake = OpenWakeWordProvider(str(_MODELS / "wakeword" / "hey_jarvis_v0.1.onnx"), 0.5)
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"wake-word feature models unavailable: {exc}")
    samples, rate = PiperProvider(str(_MODELS / "tts" / "en_US-lessac-medium.onnx")).synthesize("Hey Jarvis")
    stream = np.concatenate([np.zeros(FRAME * 10, dtype=np.int16), to_int16(resample(samples, rate)), np.zeros(FRAME * 20, dtype=np.int16)])
    assert any(wake.process(f) for f in frames_of(stream))
    wake.reset()
    assert not any(wake.process(f) for f in frames_of(np.zeros(FRAME * 60, dtype=np.int16)))
