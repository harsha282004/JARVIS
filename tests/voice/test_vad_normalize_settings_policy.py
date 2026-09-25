"""Unit tests: VAD/utterance detection, transcript normalisation and control words, persisted settings, DND/priority policy, spoken summaries."""

from datetime import datetime, time
from zoneinfo import ZoneInfo

import numpy as np
import pytest

from tests.voice_helpers import FRAME, RATE, silence, speech
from voice.normalize import Control, control_of, normalize
from voice.policy import VoicePolicy, clean_for_speech, in_schedule, spoken_version, split_sentences
from voice.settings import VoiceSettings, VoiceSettingsStore
from voice.vad import EnergyVAD, UtteranceDetector, UtteranceStatus, frame_level

IST = ZoneInfo("Asia/Kolkata")


def detector(**kw):
    return UtteranceDetector(EnergyVAD(0.015), RATE, **{"silence_seconds": 1.0, "max_utterance_seconds": 15.0, "min_utterance_seconds": 0.3,
                                                        "no_speech_seconds": 3.0, **kw})


def feed(d, frames):
    for f in frames:
        if d.push(f) not in (UtteranceStatus.WAITING, UtteranceStatus.SPEAKING):
            break
    return d.status


# ---- VAD ------------------------------------------------------------------------------------------------------------------------

def test_level_of_silence_and_speech():
    assert frame_level(silence(1)[0]) == 0.0
    assert 0.1 < frame_level(speech(1)[0]) < 0.2
    assert frame_level(np.array([], dtype=np.int16)) == 0.0


def test_utterance_ends_after_the_configured_silence_not_a_fixed_window():
    d = detector(silence_seconds=1.0)
    assert feed(d, silence(3) + speech(6) + silence(20)) is UtteranceStatus.COMPLETE
    r = d.result()
    assert r.speech_seconds == pytest.approx(6 * FRAME / RATE)
    assert r.speech_started_after == pytest.approx(3 * FRAME / RATE)
    # ~1 s of trailing silence was consumed (13 frames), not the 20 available and not a fixed 5 s window
    assert r.total_seconds < 3 * 0.08 + 6 * 0.08 + 1.2


def test_a_shorter_silence_setting_finishes_sooner():
    fast, slow = detector(silence_seconds=0.4), detector(silence_seconds=2.0)
    feed(fast, speech(5) + silence(40))
    feed(slow, speech(5) + silence(40))
    assert fast.result().total_seconds < slow.result().total_seconds


def test_a_pause_inside_a_sentence_does_not_end_it():
    d = detector(silence_seconds=1.0)
    assert feed(d, speech(4) + silence(6) + speech(4) + silence(20)) is UtteranceStatus.COMPLETE
    assert d.result().speech_seconds == pytest.approx(8 * 0.08)


def test_nobody_speaks_gives_no_speech():
    assert feed(detector(no_speech_seconds=1.0), silence(30)) is UtteranceStatus.NO_SPEECH


def test_a_click_shorter_than_the_minimum_is_dropped():
    assert feed(detector(min_utterance_seconds=0.5), silence(2) + speech(1) + silence(20)) is UtteranceStatus.TOO_SHORT


def test_maximum_utterance_cuts_off_a_monologue():
    d = detector(max_utterance_seconds=2.0)
    assert feed(d, speech(60)) is UtteranceStatus.TOO_LONG
    assert d.result().total_seconds <= 2.2


def test_pre_roll_keeps_the_start_of_the_first_word():
    d = detector()
    feed(d, silence(5) + speech(4) + silence(20))
    assert d.result().audio.size >= (4 + 3) * FRAME  # the speech plus the frames just before the trigger


def test_noise_floor_adapts_so_steady_background_is_not_speech():
    vad = EnergyVAD(0.015)
    hum = (np.random.default_rng(1).normal(0, 0.006, FRAME) * 32767).astype(np.int16)
    assert not any(vad.is_speech(hum) for _ in range(30))
    assert vad.is_speech(speech(1)[0])


def test_threshold_is_configurable():
    quiet = speech(1, level=0.03)[0]
    assert EnergyVAD(0.015).is_speech(quiet) and not EnergyVAD(0.2).is_speech(quiet)


# ---- normalisation and control words ------------------------------------------------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    ("JARVIS comma check my mail", "check my mail"),
    ("Hey Jarvis, what's my schedule", "what's my schedule"),
    ("uh… what's the time", "what's the time"),
    ("um remind me at 6 pm period", "remind me at 6 pm."),
    ("Check my mail", "Check my mail"),
])
def test_normalization_cleans_fillers_wake_word_and_spoken_punctuation(raw, expected):
    assert normalize(raw).text == expected


def test_a_spoken_correction_becomes_a_canonical_change_request():
    n = normalize("No, I meant tomorrow")
    assert n.text == "change that to tomorrow" and n.corrected
    assert normalize("sorry I meant 6 PM").text == "change that to 6 PM"


def test_retracting_the_request_is_a_cancel_not_a_task():
    assert normalize("actually don't do that").control is Control.CANCEL
    assert normalize("never mind").control is Control.CANCEL


@pytest.mark.parametrize("phrase", ["Stop", "stop.", "JARVIS stop", "Hey Jarvis, stop", "That's enough", "Enough", "be quiet", "stop talking"])
def test_stop_phrases_are_controls(phrase):
    assert control_of(phrase) is Control.STOP
    n = normalize(phrase)
    assert n.control is Control.STOP and n.text == ""  # nothing is left to send to the agent


@pytest.mark.parametrize("phrase", ["Wait", "hold on", "one moment", "just a second"])
def test_wait_phrases(phrase):
    assert control_of(phrase) is Control.WAIT


@pytest.mark.parametrize("phrase", ["Cancel", "cancel that", "never mind", "forget it", "scratch that"])
def test_cancel_phrases(phrase):
    assert control_of(phrase) is Control.CANCEL


@pytest.mark.parametrize("phrase", ["cancel my dentist appointment", "stop the timer at 5", "wait for the email from Rao", "what does stop mean", "Cancel the meeting tomorrow"])
def test_longer_sentences_are_real_requests_never_controls(phrase):
    assert control_of(phrase) is Control.NONE
    assert normalize(phrase).text  # goes to the agent and its confirmations


def test_empty_and_noise_transcripts():
    assert normalize("").text == "" and normalize("   ").text == "" and normalize("uh…").text == ""


# ---- settings -------------------------------------------------------------------------------------------------------------------

def test_settings_persist_and_survive_a_restart(tmp_path):
    store = VoiceSettingsStore(tmp_path / "v.json")
    store.update({"wake_sensitivity": 0.7, "tts_speed": 1.25, "dnd_enabled": True, "silence_seconds": 1.5, "microphone": "2"})
    again = VoiceSettingsStore(tmp_path / "v.json").current
    assert (again.wake_sensitivity, again.tts_speed, again.dnd_enabled, again.silence_seconds, again.microphone) == (0.7, 1.25, True, 1.5, "2")


def test_invalid_values_change_nothing():
    store = VoiceSettingsStore()
    before = store.current
    for bad in ({"tts_speed": "fast"}, {"nope": 1}, {"dnd_start": "25:00"}, {"voice_muted": "maybe"}, {"barge_in": "always"}, {"tts_volume": float("nan")},
                {"microphone": "x" * 500}, {"min_utterance_seconds": 3.0, "max_utterance_seconds": 2.0}):
        with pytest.raises(ValueError):
            store.update(bad)
    assert store.current == before


def test_numbers_are_clamped_to_safe_ranges():
    store = VoiceSettingsStore()
    store.update({"tts_speed": 99, "tts_volume": -3, "wake_sensitivity": 5})
    s = store.current
    assert (s.tts_speed, s.tts_volume, s.wake_sensitivity) == (2.0, 0.0, 0.99)


def test_a_corrupt_settings_file_falls_back_to_defaults(tmp_path):
    path = tmp_path / "v.json"
    path.write_text("{not json", encoding="utf-8")
    assert VoiceSettingsStore(path).current == VoiceSettings()
    assert (tmp_path / "v.json.corrupt").exists()


def test_listeners_are_told_and_a_failing_listener_does_not_block_the_change():
    store = VoiceSettingsStore()
    seen = []
    store.add_listener(lambda s: (_ for _ in ()).throw(RuntimeError("boom")))
    store.add_listener(lambda s: seen.append(s.tts_speed))
    store.update({"tts_speed": 1.5})
    assert seen == [1.5] and store.current.tts_speed == 1.5


def test_settings_file_holds_no_secrets_or_audio(tmp_path):
    store = VoiceSettingsStore(tmp_path / "v.json")
    store.update({"microphone": "Headset"})
    text = (tmp_path / "v.json").read_text(encoding="utf-8").lower()
    assert "token" not in text and "password" not in text and "audio" not in text


# ---- policy: DND, mute, priorities ---------------------------------------------------------------------------------------------

def policy(**settings):
    store = VoiceSettingsStore(defaults=VoiceSettings(**settings))
    clock = {"now": datetime(2026, 9, 25, 15, 0, tzinfo=IST)}
    p = VoicePolicy(lambda: store.current, lambda: clock["now"])
    return p, store, clock


def test_schedule_crossing_midnight():
    assert in_schedule(time(23, 0), time(22, 0), time(7, 0)) and in_schedule(time(6, 59), time(22, 0), time(7, 0))
    assert not in_schedule(time(7, 0), time(22, 0), time(7, 0)) and not in_schedule(time(15, 0), time(22, 0), time(7, 0))
    assert in_schedule(time(13, 0), time(12, 0), time(14, 0)) and not in_schedule(time(1, 0), time(1, 0), time(1, 0))


def test_normal_hours_everything_is_spoken():
    p, _, _ = policy()
    assert all(p.may_speak(x)[0] for x in ("low", "normal", "high", "critical"))


def test_manual_dnd_holds_everything_but_allowed_critical():
    p, store, _ = policy(dnd_enabled=True)
    assert [p.may_speak(x)[0] for x in ("low", "normal", "high", "critical")] == [False, False, False, True]
    store.update({"dnd_allow_critical": False})
    assert not p.may_speak("critical")[0]


def test_scheduled_dnd_follows_the_clock():
    p, _, clock = policy(dnd_schedule_enabled=True, dnd_start="22:00", dnd_end="07:00")
    assert p.may_speak("normal")[0]
    clock["now"] = datetime(2026, 9, 25, 23, 30, tzinfo=IST)
    assert not p.may_speak("normal")[0] and p.dnd_active()
    clock["now"] = datetime(2026, 9, 26, 7, 30, tzinfo=IST)
    assert p.may_speak("normal")[0]


def test_mute_silences_even_critical_and_notifications_off_keeps_only_critical():
    p, _, _ = policy(voice_muted=True)
    assert not p.may_speak("critical")[0]
    p, _, _ = policy(voice_notifications=False)
    assert [p.may_speak(x)[0] for x in ("normal", "high", "critical")] == [False, False, True]


def test_only_critical_may_interrupt_a_conversation():
    p, _, _ = policy()
    assert p.may_interrupt_conversation("critical") and not p.may_interrupt_conversation("high") and not p.may_interrupt_conversation("normal")
    p, _, _ = policy(dnd_enabled=True, dnd_allow_critical=False)
    assert not p.may_interrupt_conversation("critical")


# ---- spoken response policy ----------------------------------------------------------------------------------------------------

def test_short_answers_are_spoken_whole_and_markup_is_removed():
    text, cut = spoken_version("**Two** meetings: [Standup](http://x) and review.", 320)
    assert text == "Two meetings: Standup and review." and not cut


def test_long_answers_are_cut_at_a_sentence_and_point_to_the_dashboard():
    long = " ".join(f"Sentence number {i} is here." for i in range(60))
    text, cut = spoken_version(long, 200)
    assert cut and text.endswith("The full details are on your dashboard.") and len(text) < 260
    assert text.startswith("Sentence number 0 is here.")


def test_one_enormous_sentence_is_still_bounded():
    text, cut = spoken_version("word " * 500, 100)
    assert cut and len(text) < 160


def test_sentence_splitting_and_cleaning():
    assert split_sentences("First one. Second one! Third?") == ["First one.", "Second one!", "Third?"]
    assert clean_for_speech("- item one\n- item two") == "item one item two"
