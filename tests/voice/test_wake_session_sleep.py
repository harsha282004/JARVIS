"""Strict wake policy, no spontaneous "Yes?", debounce, echo protection, the 120-second voice session, the "JARVIS sleep" command and the male Piper voice.
Scripted hardware only (no microphone, model or network); the real-hardware counterpart is scripts/voice_real_check.py and the launcher check."""

from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pytest

from agent.tasks.notifications import AnnouncementQueue
from backend.core.config import Settings
from tests.voice_helpers import FakeConversation, RecordingTTS, ScriptedMic, ScriptedOutput, ScriptedSTT, mic_gone, silence, speech
from voice.engine import ACK_SLEEP, VoiceEngine
from voice.policy import VoicePolicy
from voice.settings import VoiceSettings, VoiceSettingsStore, defaults_from_config
from voice.status import VoiceStatus
from voice.wake import WakeConfig, WakeGate, confirm_phrase, is_sleep_command, normalize_wake_phrase, wake_plus_sleep

IST = ZoneInfo("Asia/Kolkata")
ROOT = Path(__file__).resolve().parents[2]


class ScoredWake:
    """openWakeWord stand-in with per-frame scores (0.0 once the script runs out)."""

    frame_samples = 1280

    def __init__(self, *scores, threshold=0.5):
        self.scores, self.threshold, self.last_score, self.calls, self.resets = list(scores), threshold, 0.0, 0, 0

    def is_ready(self):
        return True

    def process(self, frame):
        self.calls += 1
        self.last_score = self.scores.pop(0) if self.scores else 0.0
        return self.last_score >= self.threshold

    def set_threshold(self, value):
        self.threshold = value

    def reset(self):
        self.resets += 1


class Clock:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t


def build(*, mic=None, wake=None, stt=None, conv=None, tts=None, out=None, cfg=None, timeout=120.0, clock=None, queue=None, settings=None):
    store = VoiceSettingsStore(defaults=settings or VoiceSettings(conversation_timeout_seconds=timeout))
    cfg = cfg or WakeConfig(session_timeout_seconds=timeout, direct_accept=True)         # session/sleep tests exercise the session, not the phrase check
    engine = VoiceEngine(wakeword=wake or ScoredWake(), stt=stt or ScriptedSTT(), conversation=conv or FakeConversation(), tts=tts or RecordingTTS(), audio_input=mic or ScriptedMic(),
                         audio_output=out or ScriptedOutput(), sample_rate=16000, listen_seconds=1.0, announcements=queue, settings=store, use_vad=True,
                         policy=VoicePolicy(lambda: store.current, lambda: datetime(2026, 9, 25, 15, 0, tzinfo=IST)), status=VoiceStatus(), sleep=lambda s: None,
                         barge_in_grace_seconds=0.0, wake=cfg, clock=clock or (lambda: 1e9))
    return engine


def waiting_for(mic, frames):
    """should_stop that fires once the scripted microphone has been read `frames` times (the wake-only loop never ends by itself)."""
    return lambda: mic.reads >= frames


UTT = speech(6) + silence(16)


# ============================================ wake phrase policy (pure) ================================================================

@pytest.mark.parametrize("text", ["Hey JARVIS", "hey jarvis", "Hey, Jarvis!", "  JARVIS  ", "jarvis.", "HEY   JARVIS?"])
def test_supported_phrases_are_accepted(text):
    assert normalize_wake_phrase(text) in ("hey jarvis", "jarvis")


@pytest.mark.parametrize("text", ["", "hello", "service", "jarvice", "hey jarvis play music", "jarvis what time is it", "the jarvis project", "hi jarvis", "okay jarvis", "hey travis",
                                  "jar vis", "hey jarvis jarvis", "sleep"])
def test_everything_else_is_rejected_without_fuzzy_matching(text):
    assert normalize_wake_phrase(text) is None


def test_confirm_phrase_and_wake_plus_sleep():
    assert confirm_phrase("Hey Jarvis.") == (True, "hey jarvis", "phrase matched")
    assert confirm_phrase("hey jarvis sleep")[0] is False and wake_plus_sleep("Hey JARVIS, sleep") and not wake_plus_sleep("hey jarvis play")
    assert confirm_phrase("what is the weather")[0] is False


@pytest.mark.parametrize("text,expected", [("sleep", True), ("Sleep.", True), ("JARVIS sleep", True), ("Hey JARVIS, sleep", True), ("go to sleep", True), ("Go to sleep, Jarvis", True),
                                           ("jarvis go to sleep", True), ("sleep well tonight", False), ("how much sleep do I need", False), ("remind me to sleep at 10", False), ("", False)])
def test_sleep_command_recognition(text, expected):
    assert is_sleep_command(text) is expected


# ============================================ the gate ====================================================================================

def test_gate_needs_sustained_strong_scores_or_a_candidate_check():
    gate = WakeGate(WakeConfig(), Clock())
    assert gate.observe(0.95, 0.5).action == "none"               # a single strong frame is not decided while the score may still be rising...
    assert gate.observe(0.0, 0.5).action == "candidate"           # ...and once it ends it is only a candidate for the phrase check, never an activation
    gate = WakeGate(WakeConfig(direct_accept=True), Clock())
    gate.observe(0.9, 0.5)
    assert gate.observe(0.92, 0.5).action == "accept"             # two consecutive strong frames (direct mode)
    gate = WakeGate(WakeConfig(), Clock())
    gate.observe(0.9, 0.5)
    strict = gate.observe(0.92, 0.5)
    assert strict.action == "candidate" and strict.strong          # the strict default: even a strong score needs the exact phrase
    gate = WakeGate(WakeConfig(), Clock())
    for score in (0.2, 0.29, 0.0):
        assert gate.observe(score, 0.5).action == "none"          # below the candidate floor: nothing
    assert gate.observe(0.4, 0.5).action == "none" and gate.observe(0.0, 0.5).action == "none"    # one raised frame below the threshold is noise
    assert gate.observe(0.4, 0.5).action == "none" and gate.observe(0.4, 0.5).action == "none"
    assert gate.observe(0.0, 0.5).action == "candidate"           # two raised frames


def test_gate_debounce_blocks_and_releases():
    clock = Clock()
    gate = WakeGate(WakeConfig(debounce_seconds=1.5, direct_accept=True), clock)
    gate.observe(0.9, 0.5)
    assert gate.observe(0.9, 0.5).action == "accept"
    gate.note_activation()
    assert gate.observe(0.99, 0.5).action == "none" and gate.blocked()                  # repeated wake audio right after: ignored
    clock.t += 1.4
    assert gate.observe(0.99, 0.5).action == "none"
    clock.t += 0.2
    assert not gate.blocked() and gate.observe(0.9, 0.5).action == "none" and gate.observe(0.0, 0.5).action == "candidate"


def test_candidate_cooldown_after_a_rejection():
    clock = Clock()
    gate = WakeGate(WakeConfig(candidate_cooldown_seconds=2.0), clock)
    gate.observe(0.7, 0.5)
    assert gate.observe(0.0, 0.5).action == "candidate"
    gate.note_rejection()
    gate.observe(0.7, 0.5)
    assert gate.observe(0.0, 0.5).action == "none"
    clock.t += 2.1
    gate.observe(0.7, 0.5)
    assert gate.observe(0.0, 0.5).action == "candidate"


def test_without_stt_confirmation_only_persistent_scores_count():
    gate = WakeGate(WakeConfig(stt_confirm=False), Clock())
    gate.observe(0.6, 0.5)
    assert gate.observe(0.0, 0.5).action == "none"                          # one frame: not persistent
    gate.observe(0.6, 0.5)
    gate.observe(0.6, 0.5)
    assert gate.observe(0.0, 0.5).action == "accept"


# ============================================ engine: wake / no spontaneous "Yes?" ========================================================

def test_hey_jarvis_activates_exactly_once_and_says_yes_after_validation():
    tts, conv = RecordingTTS(), FakeConversation("It's 3 PM.")
    mic = ScriptedMic(*silence(3), *UTT)
    engine = build(mic=mic, wake=ScoredWake(0.0, 0.92, 0.95), stt=ScriptedSTT("what time is it"), conv=conv, tts=tts, timeout=3.0)
    assert engine.run_once() == "It's 3 PM."
    assert tts.spoken[0] == "Yes?" and tts.spoken.count("Yes?") == 1 and conv.received == ["what time is it"]
    assert engine.status.activations == 1 and engine.status.last_wake["source"] == "model" and engine.status.last_wake["phrase"] == "hey jarvis"


@pytest.mark.parametrize("phrase,canonical", [("Hey JARVIS.", "hey jarvis"), ("Jarvis", "jarvis")])
def test_a_weaker_candidate_is_confirmed_by_the_exact_phrase(phrase, canonical):
    tts = RecordingTTS()
    stt = ScriptedSTT(phrase, "what can you do")
    engine = build(mic=ScriptedMic(*silence(3), *UTT), wake=ScoredWake(0.0, 0.62, 0.0), stt=stt, tts=tts, timeout=3.0)
    engine.run_once()
    assert tts.spoken[0] == "Yes?" and engine.status.last_wake["source"] == "stt_confirmed" and engine.status.last_wake["phrase"] == canonical
    assert engine.status.last_wake["stt_confirmed"] is True and len(stt.heard) >= 1


@pytest.mark.parametrize("heard", ["the weather is nice today", "jarvis play some music", "service", "", "hey travis"])
def test_unrelated_or_nonexact_speech_never_activates(heard):
    tts, conv = RecordingTTS(), FakeConversation()
    mic = ScriptedMic(*silence(200))
    engine = build(mic=mic, wake=ScoredWake(0.0, 0.7, 0.0), stt=ScriptedSTT(heard), conv=conv, tts=tts)
    assert engine.run_once(waiting_for(mic, 150)) is None
    assert tts.spoken == [] and conv.received == [] and engine.status.activations == 0 and engine.status.wake_rejections == 1


def test_hey_jarvis_sleep_heard_while_waiting_is_ignored_silently():
    tts = RecordingTTS()
    mic = ScriptedMic(*silence(200))
    engine = build(mic=mic, wake=ScoredWake(0.7, 0.0), stt=ScriptedSTT("Hey JARVIS, sleep"), tts=tts)
    engine.run_once(waiting_for(mic, 150))
    assert tts.spoken == [] and engine.status.activations == 0


def test_silence_and_noise_do_not_activate():
    tts = RecordingTTS()
    mic = ScriptedMic(*(speech(6) + silence(10)) * 8)                      # loud non-wake noise: the model scores it ~0
    engine = build(mic=mic, wake=ScoredWake(), tts=tts, stt=ScriptedSTT())
    engine.run_once(waiting_for(mic, 400))
    assert tts.spoken == [] and engine.status.activations == 0


def test_below_threshold_scores_do_not_trigger_even_a_speech_check():
    stt, tts = ScriptedSTT("hey jarvis"), RecordingTTS()
    mic = ScriptedMic(*silence(200))
    engine = build(mic=mic, wake=ScoredWake(0.0, 0.4, 0.0, 0.29, 0.0, 0.1), stt=stt, tts=tts)
    engine.run_once(waiting_for(mic, 100))
    assert tts.spoken == [] and stt.heard == []                             # not even a candidate: no transcription happened


def test_startup_produces_no_speech():
    tts = RecordingTTS()
    mic = ScriptedMic()
    engine = build(mic=mic, tts=tts)
    assert engine.run_once(lambda: True) is None and tts.spoken == []
    assert mic.opens <= 1


def test_microphone_reconnect_produces_no_speech():
    tts = RecordingTTS()
    mic = ScriptedMic(*silence(5), mic_gone(), *silence(300))
    engine = build(mic=mic, tts=tts)
    engine.run_once(waiting_for(mic, 200))
    assert tts.spoken == [] and mic.opens == 2                              # it reconnected, and said nothing


def test_periodic_announcement_polling_and_empty_queue_say_nothing():
    tts = RecordingTTS()
    engine = build(queue=AnnouncementQueue(), tts=tts)
    for _ in range(5):
        engine._speak_announcements()
    assert tts.spoken == []


def test_empty_or_unintelligible_transcript_after_a_wake_adds_no_second_yes():
    tts = RecordingTTS()
    engine = build(mic=ScriptedMic(*silence(3), *UTT), wake=ScoredWake(0.0, 0.9, 0.95), stt=ScriptedSTT(""), tts=tts, timeout=3.0)
    engine.run_once()
    assert tts.spoken == ["Yes?"] and engine.status.false_activations == 1


def test_a_stale_manual_request_is_dropped_never_replayed():
    clock, tts = Clock(), RecordingTTS()
    mic = ScriptedMic(*silence(100))
    engine = build(mic=mic, tts=tts, clock=clock)
    engine.request_activation()                                              # e.g. clicked while paused; nobody was listening
    clock.t += 30
    engine.run_once(waiting_for(mic, 60))
    assert tts.spoken == [] and engine.status.activations == 0


def test_a_fresh_manual_request_activates_once():
    clock, tts = Clock(), RecordingTTS()
    engine = build(mic=ScriptedMic(*silence(3), *UTT), tts=tts, clock=clock, stt=ScriptedSTT("hello"), timeout=3.0)
    engine.request_activation()
    engine.run_once()
    assert tts.spoken.count("Yes?") == 1 and engine.status.last_wake["source"] == "manual"


def test_the_acknowledgement_can_only_follow_a_validated_wake_event():
    src = (ROOT / "voice" / "engine.py").read_text(encoding="utf-8")
    assert src.count("self._activation_reply") == 2                          # stored once, spoken once
    spoken = src.index("self._speak(self._activation_reply")
    assert src.rindex("wake = self._last_wake", 0, spoken) > src.rindex("def _run_once", 0, spoken)
    assert "ACTIVATION_WITHOUT_VALIDATED_WAKE" in src


# ============================================ debounce, echo, barge-in =====================================================================

def test_repeated_wake_audio_after_an_activation_does_not_activate_again():
    clock, tts = Clock(), RecordingTTS()
    mic = ScriptedMic(*silence(3), *UTT, *silence(200))
    wake = ScoredWake(0.0, 0.92, 0.95, *([0.95] * 100))
    engine = build(mic=mic, wake=wake, stt=ScriptedSTT("hi"), tts=tts, clock=clock, timeout=3.0)
    engine.run_once()
    first = engine.status.activations
    engine.run_once(waiting_for(mic, mic.reads + 60))                        # the wake audio keeps arriving inside the debounce/echo window
    assert first == 1 and engine.status.activations == 1 and tts.spoken.count("Yes?") == 1


def test_jarvis_own_voice_cannot_wake_or_interrupt_it_with_a_weak_score():
    out = ScriptedOutput(polls=40)
    mic = ScriptedMic(*silence(3), *UTT, *silence(200))
    wake = ScoredWake(0.0, 0.92, 0.95)
    tts = RecordingTTS()
    engine = build(mic=mic, wake=wake, stt=ScriptedSTT("tell me a story"), conv=FakeConversation("Once upon a time."), tts=tts, out=out, timeout=3.0)
    wake.scores += [0.0] * 4 + [0.6] * 20                                     # echo-like, weak-but-above-threshold scores while JARVIS speaks
    engine.run_once()
    assert out.stopped == 0 and engine.status.interruptions == 0


def test_a_strong_wake_while_speaking_still_barges_in():
    out = ScriptedOutput(polls=200)
    mic = ScriptedMic(*silence(3), *UTT, *silence(20), *speech(6), *silence(16))
    wake = ScoredWake(0.0, 0.92, 0.95)
    engine = build(mic=mic, wake=wake, stt=ScriptedSTT("tell me a story", "stop"), conv=FakeConversation("Once upon a time."), out=out, timeout=3.0)
    wake.scores += [0.0] * 20 + [0.95] * 10
    engine.run_once()
    assert out.stopped >= 1 and engine.status.interruptions == 1


def test_speech_arms_a_post_tts_block():
    clock = Clock()
    engine = build(mic=ScriptedMic(*silence(3), *UTT), wake=ScoredWake(0.0, 0.9, 0.95), stt=ScriptedSTT("hi"), clock=clock, timeout=3.0)
    engine.run_once()
    assert engine._gate.blocked() and engine._wake_block_until >= clock.t + 1.0


# ============================================ session: 120 s inactivity =====================================================================

def audio_seconds(mic):
    return mic.reads * 1280 / 16000


def test_wake_starts_a_session_and_follow_ups_need_no_wake_word():
    conv, tts = FakeConversation("Answer."), RecordingTTS()
    mic = ScriptedMic(*silence(3), *UTT, *silence(20), *UTT)
    wake = ScoredWake(0.0, 0.92, 0.95)
    engine = build(mic=mic, wake=wake, stt=ScriptedSTT("first question", "second question"), conv=conv, tts=tts, timeout=5.0)
    engine.run_once()
    assert conv.received == ["first question", "second question"] and tts.spoken.count("Yes?") == 1
    assert wake.calls <= 3                                                   # the wake model was not consulted again during the conversation


def test_the_session_times_out_after_120_seconds_of_inactivity_and_says_nothing():
    conv, tts = FakeConversation("Here you go."), RecordingTTS()
    mic = ScriptedMic(*silence(3), *UTT)                                     # then silence forever
    engine = build(mic=mic, wake=ScoredWake(0.0, 0.92, 0.95), stt=ScriptedSTT("what can you do"), conv=conv, tts=tts, timeout=120.0)
    engine.run_once()
    assert tts.spoken == ["Yes?", "Here you go."]                            # no unsolicited speech because of the timeout
    assert 115 <= audio_seconds(mic) - 1.9 <= 126                            # ~120 s of audio after the answer (the first ~2 s are the wake and the question)
    snap = engine.status.snapshot()["conversation"]
    assert snap["session"] == "asleep" and snap["sleep_reason"] == "timeout" and not snap["active"]


def test_ambient_noise_and_empty_transcripts_do_not_reset_the_timer():
    tts = RecordingTTS()
    blips = (speech(4) + silence(16)) * 40                                   # a noisy room: 40 blips, each ~1.6 s, spread over ~64 s
    mic = ScriptedMic(*silence(3), *UTT, *blips)
    engine = build(mic=mic, wake=ScoredWake(0.0, 0.92, 0.95), stt=ScriptedSTT("hello"), tts=tts, timeout=120.0)
    engine.run_once()                                                        # every later transcript is empty
    assert audio_seconds(mic) < 122 + 2                                      # if each blip had reset the timer this would be > 180 s
    assert engine.status.snapshot()["conversation"]["sleep_reason"] == "timeout" and tts.spoken == ["Yes?", "Okay."]


def test_low_confidence_speech_does_not_reset_the_timer():
    conv = FakeConversation("Fine.")
    blips = (speech(6) + silence(16)) * 30
    mic = ScriptedMic(*silence(3), *UTT, *blips)
    stt = ScriptedSTT(("hello", 0.95), *[("blah blah", 0.1)] * 30)
    engine = build(mic=mic, wake=ScoredWake(0.0, 0.92, 0.95), stt=stt, conv=conv, timeout=60.0)
    engine.run_once()
    assert conv.received == ["hello"] and audio_seconds(mic) < 66            # the doubtful speech never reached the agent and never extended the session


def test_a_real_utterance_restarts_the_timer():
    conv = FakeConversation("Ok.")
    gap = silence(500)                                                       # ~40 s of quiet
    mic = ScriptedMic(*silence(3), *UTT, *gap, *UTT)
    engine = build(mic=mic, wake=ScoredWake(0.0, 0.92, 0.95), stt=ScriptedSTT("one", "two"), conv=conv, timeout=60.0)
    engine.run_once()
    assert conv.received == ["one", "two"]
    assert audio_seconds(mic) > 40 + 60 - 4                                  # ...and then a further full 60 s of listening after "two"


def test_timeout_is_configurable_and_bounded():
    s = Settings(VOICE_SESSION_TIMEOUT_SECONDS=45)
    assert defaults_from_config(s).conversation_timeout_seconds == 45 and Settings().VOICE_SESSION_TIMEOUT_SECONDS == 120
    assert defaults_from_config(Settings(VOICE_CONVERSATION_TIMEOUT_SECONDS=30)).conversation_timeout_seconds == 30       # the old name still works
    for bad in ({"VOICE_SESSION_TIMEOUT_SECONDS": 1}, {"VOICE_SESSION_TIMEOUT_SECONDS": 99999}, {"WAKE_DEBOUNCE_SECONDS": -1}, {"WAKE_MIN_FRAMES": 0}, {"WAKE_DIRECT_THRESHOLD": 2}):
        with pytest.raises(Exception):
            Settings(**bad)


# ============================================ sleep command ==================================================================================

@pytest.mark.parametrize("phrase", ["JARVIS sleep", "jarvis sleep.", "Hey JARVIS, sleep", "go to sleep", "Sleep"])
def test_sleep_command_ends_the_session_without_the_model_or_tools(phrase):
    conv, tts = FakeConversation("never used"), RecordingTTS()
    conv.awaiting = "confirmation"
    mic = ScriptedMic(*silence(3), *UTT, *silence(20), *UTT, *silence(2000))
    engine = build(mic=mic, wake=ScoredWake(0.0, 0.92, 0.95), stt=ScriptedSTT("what's up", phrase), conv=conv, tts=tts, timeout=120.0)
    engine.run_once()
    assert conv.received == ["what's up"] and phrase not in conv.received          # the sleep phrase never reached the agent (no Groq, no tools)
    assert tts.spoken[-1] == ACK_SLEEP == "Going to sleep." and conv.cancelled == 1   # a pending question was dropped
    snap = engine.status.snapshot()["conversation"]
    assert snap["session"] == "asleep" and snap["sleep_reason"] == "command"
    assert audio_seconds(mic) < 12                                                # it stopped listening at once, it did not wait for the timeout


def test_sleep_is_not_triggered_by_longer_sentences_and_can_be_disabled():
    conv = FakeConversation("ok")
    mic = ScriptedMic(*silence(3), *UTT, *silence(20), *UTT)
    engine = build(mic=mic, wake=ScoredWake(0.0, 0.92, 0.95), stt=ScriptedSTT("hi", "sleep well tonight"), conv=conv, timeout=4.0)
    engine.run_once()
    assert conv.received == ["hi", "sleep well tonight"]
    conv2 = FakeConversation("ok")
    mic2 = ScriptedMic(*silence(3), *UTT, *silence(20), *UTT)
    engine2 = build(mic=mic2, wake=ScoredWake(0.0, 0.92, 0.95), stt=ScriptedSTT("hi", "sleep"), conv=conv2, cfg=WakeConfig(sleep_command_enabled=False, session_timeout_seconds=4.0, direct_accept=True), timeout=4.0)
    engine2.run_once()
    assert conv2.received == ["hi", "sleep"]


def test_wake_works_again_after_sleep_and_after_timeout():
    tts = RecordingTTS()
    mic = ScriptedMic(*silence(3), *UTT, *silence(20), *UTT, *silence(100))
    wake = ScoredWake(0.0, 0.92, 0.95)
    engine = build(mic=mic, wake=wake, stt=ScriptedSTT("hi", "JARVIS sleep", "hello again"), tts=tts, timeout=30.0)
    engine.run_once()
    assert engine.status.snapshot()["conversation"]["sleep_reason"] == "command"
    mic.push(*silence(100), *UTT, *silence(2000))
    wake.scores += [0.0] * 3 + [0.9, 0.95]
    engine._gate._blocked_until = 0.0                                          # (wall-clock debounce is covered separately)
    engine.run_once()
    assert tts.spoken.count("Yes?") == 2 and engine.status.activations == 2


# ============================================ male voice ====================================================================================

def test_male_voice_is_the_configured_default():
    s = Settings()
    assert s.TTS_VOICE == "en_US-ryan-medium" and VoiceSettings().tts_voice == "en_US-ryan-medium"
    text = (ROOT / ".env.example").read_text(encoding="utf-8")
    assert "TTS_MODEL_PATH=models/tts/en_US-ryan-medium.onnx" in text and "TTS_VOICE=en_US-ryan-medium" in text and "download_voice('en_US-ryan-medium'" in text


def test_tts_health_reports_a_missing_voice_honestly_and_names_the_installed_one(tmp_path):
    from backend.core.health import ServiceState
    from desktop.runtime.health_checks import tts_voice_check

    assert tts_voice_check("", "x")().state is ServiceState.FAILED
    missing = tts_voice_check(str(tmp_path / "en_US-ryan-medium.onnx"), "en_US-ryan-medium")()
    assert missing.state is ServiceState.FAILED and "not found" in missing.detail and "en_US-ryan-medium" in missing.detail
    (tmp_path / "en_US-ryan-medium.onnx").write_bytes(b"x")
    (tmp_path / "en_US-ryan-medium.onnx.json").write_text('{"dataset": "ryan"}', encoding="utf-8")
    ok = tts_voice_check(str(tmp_path / "en_US-ryan-medium.onnx"), "en_US-ryan-medium")()
    assert ok.state is ServiceState.HEALTHY and "en_US-ryan-medium (male)" in ok.detail
    (tmp_path / "en_US-lessac-medium.onnx").write_bytes(b"x")
    (tmp_path / "en_US-lessac-medium.onnx.json").write_text('{"dataset": "lessac"}', encoding="utf-8")
    assert "(male)" not in tts_voice_check(str(tmp_path / "en_US-lessac-medium.onnx"), "x")().detail       # the female voice is never reported as male


def test_the_male_piper_model_is_really_installed_and_speaks():
    model = ROOT / "models" / "tts" / "en_US-ryan-medium.onnx"
    if not model.is_file():
        pytest.skip("en_US-ryan-medium is not installed here (see docs/voice-system.md)")
    import json

    assert json.loads(Path(str(model) + ".json").read_text(encoding="utf-8"))["dataset"] == "ryan"
    from voice.tts.piper_provider import PiperProvider

    audio, rate = PiperProvider(str(model)).synthesize("Yes?")
    assert rate == 22050 and audio.size > 1000 and float(np.abs(audio).max()) > 0.05


# ============================================ strict default: a strong score is still phrase-checked ===========================================

STRICT = WakeConfig(session_timeout_seconds=3.0)


@pytest.mark.parametrize("heard", ["Okay Jarvis", "Yes, Jarvis", "Hey Travis", "hey jarvis play music", "Jarvis sleep"])
def test_strict_mode_rejects_a_strong_model_score_for_anything_but_the_exact_phrase(heard):
    tts, conv = RecordingTTS(), FakeConversation()
    mic = ScriptedMic(*silence(300))
    engine = build(mic=mic, wake=ScoredWake(0.0, 0.95, 0.99, 1.0, 0.98), stt=ScriptedSTT(heard), conv=conv, tts=tts, cfg=STRICT)
    engine.run_once(waiting_for(mic, 200))
    assert tts.spoken == [] and conv.received == [] and engine.status.activations == 0 and engine.status.wake_rejections == 1


@pytest.mark.parametrize("heard", ["Hey Jarvis.", "JARVIS!", "hey, jarvis"])
def test_strict_mode_accepts_the_exact_phrase_after_the_check(heard):
    tts = RecordingTTS()
    engine = build(mic=ScriptedMic(*silence(3), *UTT), wake=ScoredWake(0.0, 0.95, 0.99, 1.0), stt=ScriptedSTT(heard, "what time is it"), tts=tts, cfg=STRICT, timeout=3.0)
    engine.run_once()
    assert tts.spoken[0] == "Yes?" and tts.spoken.count("Yes?") == 1 and engine.status.last_wake["stt_confirmed"] is True


def test_strict_mode_without_working_speech_recognition_never_activates():
    class Down(ScriptedSTT):
        def is_ready(self):
            return False

    tts = RecordingTTS()
    mic = ScriptedMic(*silence(300))
    engine = build(mic=mic, wake=ScoredWake(0.0, 0.95, 0.99, 1.0), stt=Down(), tts=tts, cfg=STRICT)
    engine.run_once(waiting_for(mic, 100))
    assert tts.spoken == [] and engine.status.activations == 0                # fail safe: no phrase check, no wake


def test_the_wake_confirmation_audio_is_kept_only_in_memory(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    mic = ScriptedMic(*silence(300))
    engine = build(mic=mic, wake=ScoredWake(0.0, 0.95, 0.99, 1.0), stt=ScriptedSTT("Okay Jarvis"), cfg=STRICT)
    engine.run_once(waiting_for(mic, 100))
    assert len(engine._ring) <= 30 and not list(tmp_path.iterdir())          # a bounded in-memory ring (~2.4 s), nothing on disk
    src = (ROOT / "voice" / "wake.py").read_text(encoding="utf-8")
    assert "open(" not in src and ".write(" not in src
