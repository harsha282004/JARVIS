"""VoiceEngine Phase 19 behavior with scripted hardware: VAD capture, active conversation, controls, barge-in, microphone loss,
low-confidence guard, response policy, notifications/DND, structured log, latency, failure injection."""

import json
import threading
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from agent.tasks.notifications import AnnouncementQueue
from backend.core.metrics import metrics
from tests.voice_helpers import (
    UTTERANCE,
    FakeConversation,
    RecordingTTS,
    ScriptedMic,
    ScriptedOutput,
    ScriptedSTT,
    ScriptedWake,
    mic_gone,
    silence,
    speech,
    stt_crash,
)
from voice.engine import (
    DIDNT_CATCH,
    LLM_UNAVAILABLE,
    LOW_CONFIDENCE_CONFIRM,
    STT_UNAVAILABLE,
    VoiceEngine,
    VoiceState,
)
from voice.exceptions import AudioDeviceError
from voice.policy import VoicePolicy
from voice.settings import VoiceSettings, VoiceSettingsStore
from voice.status import VoiceLog, VoiceStatus
from backend.core.llm.base import LLMProviderError

IST = ZoneInfo("Asia/Kolkata")


def build(*, mic=None, wake=None, stt=None, conv=None, tts=None, out=None, queue=None, settings=None, log=None, clock=None, vad=True, **kw):
    store = VoiceSettingsStore(defaults=settings or VoiceSettings(conversation_timeout_seconds=3.0))
    now = clock or (lambda: datetime(2026, 9, 25, 15, 0, tzinfo=IST))
    engine = VoiceEngine(
        wakeword=wake or ScriptedWake(1), stt=stt or ScriptedSTT(), conversation=conv or FakeConversation(), tts=tts or RecordingTTS(),
        audio_input=mic or ScriptedMic(*UTTERANCE), audio_output=out or ScriptedOutput(), sample_rate=16000, listen_seconds=1.0,
        announcements=queue, settings=store, use_vad=vad, policy=VoicePolicy(lambda: store.current, now), status=VoiceStatus(),
        log=log, sleep=lambda s: None, barge_in_grace_seconds=0.0, **kw)
    return engine, store


# ---- capture and conversation -----------------------------------------------------------------------------------------------

def test_full_turn_with_vad_capture_and_states():
    tts, conv = RecordingTTS(), FakeConversation("You have two meetings.")
    engine, _ = build(stt=ScriptedSTT("what meetings do I have"), conv=conv, tts=tts)
    assert engine.run_once() == "You have two meetings."
    assert conv.received == ["what meetings do I have"]
    assert tts.spoken == ["Yes?", "You have two meetings."]
    snap = engine.status.snapshot()
    assert snap["last_transcription"] == "what meetings do I have" and snap["last_response"] == "You have two meetings."
    assert snap["state"] == "IDLE" and snap["tts_state"] == "TTS_IDLE" and snap["wake_word"]["activations"] == 1


def test_the_recorded_utterance_is_the_speech_not_a_fixed_window():
    stt = ScriptedSTT("hello there")
    engine, _ = build(stt=stt)
    engine.run_once()
    audio = stt.heard[0]
    assert 0.5 < len(audio) / 16000 < 3.0  # about the utterance plus the silence that ended it, not a fixed 5 s or 15 s


def test_active_conversation_follow_up_needs_no_wake_word_then_times_out():
    wake = ScriptedWake(1)
    mic = ScriptedMic(*UTTERANCE, *UTTERANCE)  # two utterances, then silence
    conv = FakeConversation("ok")
    engine, _ = build(mic=mic, wake=wake, stt=ScriptedSTT("first question", "second question"), conv=conv)
    engine.run_once()
    assert conv.received == ["first question", "second question"]
    calls_during_conversation = wake.calls
    assert calls_during_conversation == 1  # the wake word was consulted once; follow-ups needed none


def test_conversation_timeout_is_configurable():
    mic = ScriptedMic(*UTTERANCE)
    engine, _ = build(mic=mic, stt=ScriptedSTT("hello"), settings=VoiceSettings(conversation_timeout_seconds=3.0))
    engine.run_once()
    short = mic.reads
    mic2 = ScriptedMic(*UTTERANCE)
    engine2, _ = build(mic=mic2, stt=ScriptedSTT("hello"), settings=VoiceSettings(conversation_timeout_seconds=10.0))
    engine2.run_once()
    assert mic2.reads > short + 50  # it kept listening for follow-ups about 7 s (~90 frames) longer


def test_no_speech_after_wake_is_a_false_activation_and_costs_no_agent_call():
    conv = FakeConversation()
    engine, _ = build(mic=ScriptedMic(), conv=conv, settings=VoiceSettings(conversation_timeout_seconds=3.0))
    assert engine.run_once() is None
    assert conv.received == [] and engine.status.false_activations == 1


def test_many_false_activations_are_reported_as_noisy_wake_word():
    engine, _ = build(mic=ScriptedMic())
    for _ in range(6):
        engine._session_id = "x"
        engine.status.update(activations=engine.status.activations + 1)
        engine._note_false_activation()
    assert "lower the wake-word sensitivity" in engine.status.snapshot()["wake_word"]["health"]


def test_shutdown_during_capture_drops_the_partial_utterance_instead_of_acting_on_it():
    conv = FakeConversation()
    engine, _ = build(mic=ScriptedMic(*speech(4)), stt=ScriptedSTT("delete everything"), conv=conv)
    calls = {"n": 0}

    def should_stop():
        calls["n"] += 1
        return calls["n"] > 3  # not while waiting for the wake word, yes a few frames into the utterance

    engine.run_once(should_stop=should_stop)
    assert conv.received == [] and engine.state == VoiceState.WAITING


def test_empty_transcript_ends_quietly():
    conv = FakeConversation()
    engine, _ = build(stt=ScriptedSTT(("", None)), conv=conv)
    assert engine.run_once() is None and conv.received == []


def test_legacy_fixed_window_still_works_without_vad():
    conv, tts = FakeConversation("legacy"), RecordingTTS()
    engine, _ = build(mic=ScriptedMic(), stt=ScriptedSTT("hi"), conv=conv, tts=tts, vad=False)
    assert engine.run_once() == "legacy"


# ---- control words --------------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("phrase", ["Stop", "JARVIS stop", "That's enough", "Wait", "Cancel", "never mind"])
def test_control_words_never_reach_the_agent(phrase):
    conv = FakeConversation("should not be asked")
    mic = ScriptedMic(*UTTERANCE)
    engine, _ = build(mic=mic, stt=ScriptedSTT(phrase), conv=conv)
    engine.run_once()
    assert conv.received == []  # not run as a task


def test_cancel_drops_the_pending_question_and_says_so():
    conv, tts = FakeConversation(awaiting="confirmation"), RecordingTTS()
    engine, _ = build(stt=ScriptedSTT("cancel"), conv=conv, tts=tts)
    engine.run_once()
    assert conv.cancelled == 1 and conv.awaiting is None and tts.spoken[-1] == "Okay, cancelled."


def test_wait_gives_the_user_more_time_then_processes_the_next_request():
    conv = FakeConversation("done")
    engine, _ = build(mic=ScriptedMic(*UTTERANCE, *silence(40), *UTTERANCE), stt=ScriptedSTT("wait", "what time is it"), conv=conv)
    engine.run_once()
    assert conv.received == ["what time is it"]


def test_a_normal_sentence_containing_cancel_is_a_real_request():
    conv = FakeConversation("I'll ask you to confirm.")
    engine, _ = build(stt=ScriptedSTT("cancel my dentist appointment"), conv=conv)
    engine.run_once()
    assert conv.received == ["cancel my dentist appointment"]


def test_normalization_is_applied_before_the_agent():
    conv = FakeConversation("ok")
    engine, _ = build(stt=ScriptedSTT("JARVIS comma uh check my mail"), conv=conv)
    engine.run_once()
    assert conv.received == ["check my mail"]


def test_repeat_that_replays_the_last_answer_without_asking_the_agent_again():
    conv, tts = FakeConversation("Your first meeting is at nine."), RecordingTTS()
    engine, _ = build(mic=ScriptedMic(*UTTERANCE, *UTTERANCE), stt=ScriptedSTT("what is first", "say that again"), conv=conv, tts=tts)
    engine.run_once()
    assert conv.received == ["what is first"] and tts.spoken.count("Your first meeting is at nine.") == 2


# ---- barge-in ------------------------------------------------------------------------------------------------------------------

def test_wake_word_during_speech_stops_it_immediately_and_stop_is_not_a_task():
    wake = ScriptedWake(1, 12)  # 1: activation; 12: the user says "Hey JARVIS" while it is talking
    conv, out, tts = FakeConversation("A very long answer. It goes on. And on."), ScriptedOutput(polls=100), RecordingTTS()
    mic = ScriptedMic(*UTTERANCE, *silence(30), *speech(6), *silence(16))
    engine, _ = build(mic=mic, wake=wake, stt=ScriptedSTT("tell me a story", "JARVIS stop"), conv=conv, out=out, tts=tts)
    engine.run_once()
    assert out.stopped >= 1
    assert conv.received == ["tell me a story"]  # "JARVIS stop" was a control word, not a request
    assert engine.status.interruptions == 1
    assert len(tts.spoken) < 4  # the remaining sentences were not spoken


def test_barge_in_with_a_new_request_processes_it():
    wake = ScriptedWake(1, 12)
    conv = FakeConversation(lambda t: "Long. Long. Long." if t == "read everything" else "It is nine.")
    mic = ScriptedMic(*UTTERANCE, *silence(30), *UTTERANCE)
    engine, _ = build(mic=mic, wake=wake, stt=ScriptedSTT("read everything", "what time is it"), conv=conv, out=ScriptedOutput(polls=100))
    engine.run_once()
    assert conv.received == ["read everything", "what time is it"]


def test_vad_barge_in_mode_stops_on_loud_speech_without_the_wake_word():
    conv, out = FakeConversation("Answer one. Answer two."), ScriptedOutput(polls=200)
    mic = ScriptedMic(*UTTERANCE, *silence(8), *speech(6, 0.4), *silence(16))
    engine, _ = build(mic=mic, stt=ScriptedSTT("talk", "stop"), conv=conv, out=out, settings=VoiceSettings(barge_in="vad", conversation_timeout_seconds=3.0))
    engine.run_once()
    assert out.stopped >= 1 and conv.received == ["talk"]


def test_barge_in_off_plays_to_the_end():
    out = ScriptedOutput(polls=5)
    engine, _ = build(stt=ScriptedSTT("hi"), out=out, settings=VoiceSettings(barge_in="off", conversation_timeout_seconds=3.0))
    engine.run_once()
    assert out.stopped == 0 and out.played  # blocking play(), nothing listens while speaking


def test_interrupt_from_the_tray_or_dashboard_stops_speech_from_another_thread():
    out = ScriptedOutput(polls=10_000, poll_sleep=0.001)
    engine, _ = build(stt=ScriptedSTT("talk"), conv=FakeConversation("One. Two. Three."), out=out)
    threading.Timer(0.05, engine.interrupt).start()
    engine.run_once()
    assert out.stopped >= 1 and engine.status.interruptions >= 1


def test_a_stop_click_while_idle_does_not_swallow_the_next_announcement():
    queue, tts = AnnouncementQueue(), RecordingTTS()
    engine, _ = build(queue=queue, tts=tts)
    queue.set_accepting(True)
    engine.interrupt()  # nothing is being said
    queue.put("Reminder: submit the assignment", "high")
    engine._speak_announcements()
    assert tts.spoken == ["Reminder: submit the assignment"]


def test_speaking_over_a_glitching_microphone_does_not_crash_playback():
    out = ScriptedOutput(polls=6)
    mic = ScriptedMic(*UTTERANCE, mic_gone(), mic_gone(), mic_gone())
    engine, _ = build(mic=mic, stt=ScriptedSTT("hi"), out=out)
    engine.run_once()  # must complete


# ---- microphone lifecycle -----------------------------------------------------------------------------------------------------

def test_microphone_unplugged_while_waiting_reconnects_and_carries_on():
    mic = ScriptedMic(mic_gone(), mic_gone(), *UTTERANCE)
    conv = FakeConversation("hi")
    engine, _ = build(mic=mic, wake=ScriptedWake(1), stt=ScriptedSTT("hello"), conv=conv)
    assert engine.run_once() == "hi"
    assert mic.opens >= 2  # the stream was reopened after the disconnect
    assert engine.status.mic in ("MICROPHONE_CLOSED", "MICROPHONE_CONNECTED")


def test_microphone_missing_at_start_retries_then_connects_and_reports_states():
    mic = ScriptedMic(*UTTERANCE, open_failures=[AudioDeviceError("no device"), AudioDeviceError("no device")])
    engine, _ = build(mic=mic, stt=ScriptedSTT("hi"))
    seen = []
    original = engine.status.update

    def spy(**fields):
        if "mic" in fields:
            seen.append(fields["mic"])
        original(**fields)

    engine.status.update = spy
    engine.run_once()
    assert seen[0] == "MICROPHONE_DISCONNECTED" and "MICROPHONE_CONNECTED" in seen


def test_permission_denied_is_reported_as_such():
    mic = ScriptedMic(open_failures=[AudioDeviceError("Access is denied: microphone permission")] * 3)
    engine, _ = build(mic=mic)
    stops = iter([False, False, True])
    assert engine.run_once(should_stop=lambda: next(stops, True)) is None
    snap = engine.status.snapshot()
    assert snap["microphone"] == "MICROPHONE_PERMISSION_DENIED" and "permission" in snap["last_error"].lower()


def test_stop_request_while_the_microphone_is_missing_returns_promptly():
    mic = ScriptedMic(open_failures=[AudioDeviceError("gone")] * 100)
    engine, _ = build(mic=mic)
    assert engine.run_once(should_stop=lambda: True) is None


def test_microphone_lost_in_the_middle_of_a_conversation_is_announced_and_the_loop_survives():
    mic = ScriptedMic(*speech(2), mic_gone())
    tts = RecordingTTS()
    engine, _ = build(mic=mic, tts=tts, stt=ScriptedSTT("x"))
    engine.run_once()
    assert any("lost the microphone" in s for s in tts.spoken) and engine.state == VoiceState.WAITING


def test_microphone_is_released_when_the_activation_ends():
    mic = ScriptedMic(*UTTERANCE)
    engine, _ = build(mic=mic, stt=ScriptedSTT("hi"))
    engine.run_once()
    assert mic.is_open is False and engine.status.mic == "MICROPHONE_CLOSED"


# ---- confirmations and low-confidence STT ------------------------------------------------------------------------------------------

def test_a_low_confidence_yes_cannot_confirm_a_pending_action():
    conv, tts = FakeConversation(awaiting="confirmation"), RecordingTTS()
    engine, _ = build(mic=ScriptedMic(*UTTERANCE, *UTTERANCE), stt=ScriptedSTT(("yes", 0.10), ("yes", 0.95)), conv=conv, tts=tts)
    engine.run_once()
    assert LOW_CONFIDENCE_CONFIRM in " ".join(tts.spoken)
    assert conv.received == ["yes"]  # only the clearly heard one reached the confirmation path


def test_a_confident_yes_reaches_the_confirmation_path_and_confidence_is_not_required_for_other_speech():
    conv = FakeConversation(awaiting=None)
    engine, _ = build(stt=ScriptedSTT(("what is the weather", 0.05)), conv=conv)
    engine.run_once()
    assert conv.received == ["what is the weather"]  # low confidence only matters for confirmations


# ---- response policy -----------------------------------------------------------------------------------------------------------

def test_long_answers_are_summarised_aloud_but_kept_in_full_for_the_dashboard():
    full = " ".join(f"Item {i} is on your list." for i in range(60))
    tts = RecordingTTS()
    engine, _ = build(stt=ScriptedSTT("read my list"), conv=FakeConversation(full), tts=tts, settings=VoiceSettings(spoken_max_chars=150, conversation_timeout_seconds=3.0))
    engine.run_once()
    said = " ".join(tts.spoken[1:])
    assert "full details are on your dashboard" in said and len(said) < 260
    assert engine.status.snapshot()["last_response"] == full


def test_muted_voice_speaks_nothing_but_the_text_answer_is_kept():
    tts = RecordingTTS()
    engine, _ = build(stt=ScriptedSTT("hi"), conv=FakeConversation("Hello."), tts=tts, settings=VoiceSettings(voice_muted=True, conversation_timeout_seconds=3.0))
    assert engine.run_once() == "Hello." and tts.spoken == []
    assert engine.status.snapshot()["last_response"] == "Hello."


def test_speed_and_volume_changes_apply_live():
    engine, store = build()
    tts, out, wake = engine._tts, engine._audio_output, engine._wakeword
    store.update({"tts_speed": 1.3, "tts_volume": 0.4, "wake_sensitivity": 0.8})
    assert (tts.speed, out.volume, wake.threshold) == (1.3, 0.4, 0.8)


def test_tts_states_go_generating_speaking_idle():
    engine, _ = build(stt=ScriptedSTT("hi"))
    seen = []
    original = engine.status.update

    def spy(**fields):
        if "tts" in fields:
            seen.append(fields["tts"])
        original(**fields)

    engine.status.update = spy
    engine.run_once()
    assert seen[:3] == ["TTS_GENERATING", "TTS_SPEAKING", "TTS_IDLE"]


# ---- notifications, DND ------------------------------------------------------------------------------------------------------------

def test_announcements_speak_by_priority_when_free():
    queue, tts = AnnouncementQueue(), RecordingTTS()
    engine, _ = build(queue=queue, tts=tts, mic=ScriptedMic(), wake=ScriptedWake(9999))
    queue.set_accepting(True)
    queue.put("low thing", "low")
    queue.put("critical thing", "critical")
    queue.put("normal thing", "normal")
    engine._speak_announcements()
    assert tts.spoken == ["critical thing", "normal thing", "low thing"]


def test_do_not_disturb_holds_non_critical_announcements_but_keeps_them_visible():
    queue, tts = AnnouncementQueue(), RecordingTTS()
    engine, _ = build(queue=queue, tts=tts, settings=VoiceSettings(dnd_enabled=True))
    queue.set_accepting(True)
    queue.put("a reminder", "high")
    queue.put("server on fire", "critical")
    engine._speak_announcements()
    assert tts.spoken == ["server on fire"]  # the optional critical override
    held = engine.status.snapshot()["held_notifications"]
    assert [h["text"] for h in held] == ["a reminder"] and held[0]["reason"] == "do not disturb"


def test_critical_override_can_be_turned_off():
    queue, tts = AnnouncementQueue(), RecordingTTS()
    engine, _ = build(queue=queue, tts=tts, settings=VoiceSettings(dnd_enabled=True, dnd_allow_critical=False))
    queue.set_accepting(True)
    queue.put("server on fire", "critical")
    engine._speak_announcements()
    assert tts.spoken == [] and len(engine.status.snapshot()["held_notifications"]) == 1


def test_a_critical_alert_interrupts_a_conversation_but_a_normal_one_waits():
    queue, tts = AnnouncementQueue(), RecordingTTS()
    conv = FakeConversation("ok")

    def respond(text):
        queue.put("normal reminder", "normal")
        queue.put("critical alert", "critical")
        return "ok"

    conv.reply = respond
    engine, _ = build(queue=queue, tts=tts, stt=ScriptedSTT("hello"), conv=conv)
    engine.run_once()
    assert "critical alert" in tts.spoken and "normal reminder" not in tts.spoken
    assert len(queue) == 1  # the normal one is still queued for after the conversation


def test_voice_notifications_switch_holds_normal_ones():
    queue, tts = AnnouncementQueue(), RecordingTTS()
    engine, _ = build(queue=queue, tts=tts, settings=VoiceSettings(voice_notifications=False))
    queue.set_accepting(True)
    queue.put("nice to know", "normal")
    engine._speak_announcements()
    assert tts.spoken == [] and engine.status.snapshot()["held_notifications"][0]["reason"] == "voice notifications off"


# ---- failure injection -------------------------------------------------------------------------------------------------------------

def test_stt_crash_is_spoken_honestly_and_the_loop_survives():
    tts = RecordingTTS()
    engine, _ = build(stt=ScriptedSTT(stt_crash()), tts=tts)
    assert engine.run_once() is None
    assert STT_UNAVAILABLE == "I can't process speech right now. You can still use the dashboard."
    assert " ".join(tts.spoken).endswith(STT_UNAVAILABLE)
    assert engine.status.snapshot()["stt"]["ready"] is False and engine.state == VoiceState.WAITING


def test_tts_unavailable_degrades_to_text_and_reports_the_error():
    engine, _ = build(stt=ScriptedSTT("hi"), conv=FakeConversation("The answer."), tts=RecordingTTS(fail=RuntimeError("no voice")))
    assert engine.run_once() == "The answer."
    snap = engine.status.snapshot()
    assert snap["tts_state"] == "TTS_ERROR" and snap["last_response"] == "The answer." and "dashboard" in snap["last_error"]


def test_audio_output_device_disconnected_does_not_crash():
    class DeadSpeaker(ScriptedOutput):
        def start(self, samples, rate):
            raise AudioDeviceError("speaker unplugged")

    engine, _ = build(stt=ScriptedSTT("hi"), conv=FakeConversation("Fine."), out=DeadSpeaker())
    assert engine.run_once() == "Fine."
    assert engine.status.snapshot()["tts_state"] == "TTS_ERROR"


def test_llm_unavailable_is_spoken_and_reported():
    class Down(FakeConversation):
        def respond(self, text):
            raise LLMProviderError("Ollama unreachable")

    tts = RecordingTTS()
    engine, _ = build(stt=ScriptedSTT("tell me a joke"), conv=Down(), tts=tts)
    with pytest.raises(LLMProviderError):
        engine.run_once()
    assert " ".join(tts.spoken).endswith(LLM_UNAVAILABLE) and "still help with reminders" in LLM_UNAVAILABLE
    assert engine.state == VoiceState.WAITING and engine.status.snapshot()["last_error"] == "Language model unavailable"


def test_speaking_while_jarvis_talks_then_silence_does_not_deadlock():
    engine, _ = build(stt=ScriptedSTT("talk"), conv=FakeConversation("One. Two."), out=ScriptedOutput(polls=50), wake=ScriptedWake(1, 12), mic=ScriptedMic(*UTTERANCE, *silence(20), *speech(2), *silence(30)))
    engine.run_once()  # ends because the barge-in produced nothing intelligible, not by hanging


def test_ambiguous_or_empty_commands_end_cleanly_without_calling_the_agent():
    conv = FakeConversation()
    engine, _ = build(stt=ScriptedSTT("uh", "um…"), mic=ScriptedMic(*UTTERANCE, *UTTERANCE), conv=conv)
    engine.run_once()
    assert conv.received == []


# ---- structured log, metrics, privacy --------------------------------------------------------------------------------------------------

def test_the_voice_log_is_structured_redacted_and_has_no_audio(tmp_path):
    log = VoiceLog(tmp_path / "voice_log.jsonl")
    secret = "sk-" + "a" * 40
    engine, _ = build(stt=ScriptedSTT(f"my api key is {secret}"), conv=FakeConversation("Noted. password=hunter2hunter2"), log=log)
    engine.run_once()
    raw = (tmp_path / "voice_log.jsonl").read_text(encoding="utf-8")
    assert secret not in raw and "hunter2hunter2" not in raw and "[REDACTED]" in raw
    records = [json.loads(line) for line in raw.splitlines()]
    turn = next(r for r in records if r["event"] == "turn")
    assert {"ts", "session_id", "state", "transcription", "intent", "latency_ms", "result"} <= set(turn)
    tr = next(r for r in records if r["event"] == "transcription")
    assert "confidence" in tr and "audio_seconds" in tr
    assert "audio" not in " ".join(k for r in records for k in r if k not in ("audio_seconds",))  # no audio field anywhere
    assert len({r["session_id"] for r in records}) == 1


def test_latency_metrics_are_recorded_per_stage():
    metrics.reset()
    engine, _ = build(stt=ScriptedSTT("hi"))
    engine.run_once()
    timers = metrics.snapshot()["timers"]
    for name in ("wake_to_prompt_ms", "stt_ms", "conversation_ms", "tts_synthesis_ms"):
        assert name in timers, name
    lat = engine.status.snapshot()["latency_ms"]
    for name in ("speech_detection_ms", "stt_ms", "agent_ms", "time_to_first_audio_ms", "end_to_end_ms"):
        assert name in lat, name


def test_a_manual_activation_followed_by_silence_is_not_counted_as_a_wake_word_false_alarm():
    engine, _ = build(mic=ScriptedMic(), wake=ScriptedWake(99999))
    engine.request_activation()
    engine.run_once()
    assert engine.status.activations == 1 and engine.status.false_activations == 0


def test_manual_activation_and_wake_reset_prevent_double_triggers():
    wake = ScriptedWake(99999)
    engine, _ = build(wake=wake, stt=ScriptedSTT("hi"))
    engine.request_activation()
    engine.run_once()
    assert wake.resets == 1  # buffered wake-word audio was cleared after activation
    assert engine._wake_block_until > 0  # and the refractory window is armed


def test_no_session_state_leaks_audio_to_disk(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    engine, _ = build(stt=ScriptedSTT("hi"))
    engine.run_once()
    assert list(tmp_path.rglob("*.wav")) == [] and list(tmp_path.rglob("*.npy")) == [] and list(tmp_path.iterdir()) == []
