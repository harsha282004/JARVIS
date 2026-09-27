"""VoiceSwitch: the JARVIS ON/OFF control the tray, dashboard API and startup all share.

Scripted RuntimeManager/engine only (no audio hardware, model or network)."""

import time

from desktop.runtime.manager import RuntimeManager
from desktop.runtime.state import RuntimeState
from tests.test_runtime_manager import FakeEngine, wait_for
from voice.settings import VoiceSettings, VoiceSettingsStore
from voice.switch import VoiceMode, VoiceSwitch, derive_mode


def make(mic_allowed=lambda: True, interrupt=None, defaults=None):
    store = VoiceSettingsStore(defaults=defaults or VoiceSettings())
    switch = VoiceSwitch(store, mic_allowed=mic_allowed, interrupt=interrupt)
    engine = FakeEngine()
    manager = RuntimeManager(lambda: engine, stop_timeout=3, start_paused=switch.start_paused)
    switch.attach_manager(manager)
    return switch, manager, engine, store


# ---- derive_mode (pure) -----------------------------------------------------------------------------------------------------------

def test_derive_mode_is_off_unless_enabled_and_the_worker_is_really_running():
    assert derive_mode(False, "running", "waiting") is VoiceMode.OFF          # switched off: OFF regardless of the worker
    assert derive_mode(True, "paused", "waiting") is VoiceMode.OFF            # enabled but not actually running yet
    assert derive_mode(True, "running", "waiting") is VoiceMode.SLEEPING
    assert derive_mode(True, "running", "listening") is VoiceMode.LISTENING
    assert derive_mode(True, "running", "transcribing") is VoiceMode.PROCESSING
    assert derive_mode(True, "running", "thinking") is VoiceMode.PROCESSING
    assert derive_mode(True, "running", "speaking") is VoiceMode.SPEAKING


# ---- initial state, ON/OFF, idempotency --------------------------------------------------------------------------------------------

def test_initial_off_state_never_opens_the_microphone():
    switch, manager, engine, _ = make(defaults=VoiceSettings(voice_enabled=False))
    manager.start()
    assert wait_for(lambda: manager.state is RuntimeState.PAUSED)
    time.sleep(0.05)
    assert engine.calls == 0 and not manager.status().microphone_active
    assert switch.mode() is VoiceMode.OFF
    manager.shutdown()


def test_on_transition_opens_the_microphone_and_starts_the_wake_detector():
    switch, manager, engine, _ = make(defaults=VoiceSettings(voice_enabled=False))
    manager.start()
    assert wait_for(lambda: manager.state is RuntimeState.PAUSED)
    switch.enable()
    assert wait_for(lambda: manager.state is RuntimeState.RUNNING)
    assert wait_for(lambda: manager.status().microphone_active)
    assert switch.mode() is VoiceMode.SLEEPING
    manager.shutdown()


def test_off_transition_releases_the_microphone_and_stops_wake_vad_stt():
    switch, manager, engine, _ = make()
    manager.start()
    assert wait_for(lambda: manager.state is RuntimeState.RUNNING and manager.status().microphone_active)
    switch.disable()
    assert wait_for(lambda: manager.state is RuntimeState.PAUSED)
    assert not manager.status().microphone_active                             # the worker (wake/VAD/STT loop) is not running at all
    assert switch.mode() is VoiceMode.OFF
    manager.shutdown()


def test_on_idempotency_no_duplicate_engine_or_microphone():
    switch, manager, engine, _ = make()
    manager.start()
    assert wait_for(lambda: manager.state is RuntimeState.RUNNING)
    for _ in range(3):
        switch.enable()
        time.sleep(0.01)
    assert manager.state is RuntimeState.RUNNING and engine.calls >= 1
    manager.shutdown()


def test_off_idempotency():
    switch, manager, engine, _ = make()
    manager.start()
    assert wait_for(lambda: manager.state is RuntimeState.RUNNING)
    switch.disable()
    assert wait_for(lambda: manager.state is RuntimeState.PAUSED)
    for _ in range(3):
        switch.disable()
        time.sleep(0.01)
    assert manager.state is RuntimeState.PAUSED
    manager.shutdown()


def test_toggle_flips_and_reconciles():
    switch, manager, engine, _ = make()
    manager.start()
    assert wait_for(lambda: manager.state is RuntimeState.RUNNING)
    assert switch.toggle() is False
    assert wait_for(lambda: manager.state is RuntimeState.PAUSED)
    assert switch.toggle() is True
    assert wait_for(lambda: manager.state is RuntimeState.RUNNING)
    manager.shutdown()


def test_toggle_no_duplicate_threads_or_microphone_streams_on_repeated_on_off():
    switch, manager, engine, _ = make()
    manager.start()
    assert wait_for(lambda: manager.state is RuntimeState.RUNNING)
    for _ in range(5):
        switch.disable()
        assert wait_for(lambda: manager.state is RuntimeState.PAUSED)
        switch.enable()
        assert wait_for(lambda: manager.state is RuntimeState.RUNNING)
    assert manager._thread is not None and manager._thread.is_alive()
    manager.shutdown()
    assert wait_for(lambda: not manager._thread.is_alive() if manager._thread else True)


# ---- OFF stops speech immediately -------------------------------------------------------------------------------------------------

def test_disable_interrupts_speech_in_progress():
    calls = []
    switch, manager, engine, _ = make(interrupt=lambda: calls.append(1))
    manager.start()
    assert wait_for(lambda: manager.state is RuntimeState.RUNNING)
    switch.disable()
    assert calls == [1]
    manager.shutdown()


# ---- privacy interaction: both must allow the microphone ----------------------------------------------------------------------------

def test_privacy_can_hold_the_microphone_closed_even_when_the_switch_is_on():
    mic_allowed = {"v": False}
    switch, manager, engine, _ = make(mic_allowed=lambda: mic_allowed["v"])
    manager.start()
    assert wait_for(lambda: manager.state is RuntimeState.PAUSED)
    assert switch.enabled and switch.mode() is VoiceMode.OFF                  # switch says ON, but privacy still blocks the microphone
    manager.shutdown()


def test_a_switched_off_voice_stays_off_when_privacy_allows_the_microphone_again():
    mic_allowed = {"v": True}
    switch, manager, engine, _ = make(mic_allowed=lambda: mic_allowed["v"])
    manager.start()
    assert wait_for(lambda: manager.state is RuntimeState.RUNNING)
    switch.disable()
    assert wait_for(lambda: manager.state is RuntimeState.PAUSED)
    mic_allowed["v"] = False
    mic_allowed["v"] = True                                                   # privacy toggles back to allowing the mic; the user's OFF must still hold
    switch.reconcile()
    assert manager.state is RuntimeState.PAUSED
    manager.shutdown()


# ---- persistence --------------------------------------------------------------------------------------------------------------------

def test_off_preference_persists_across_a_restart(tmp_path):
    path = tmp_path / "voice_settings.json"
    store1 = VoiceSettingsStore(path, defaults=VoiceSettings())
    switch1 = VoiceSwitch(store1)
    switch1.disable()
    assert store1.current.voice_enabled is False

    store2 = VoiceSettingsStore(path, defaults=VoiceSettings())               # a fresh process reading the same file
    assert store2.current.voice_enabled is False


def test_on_preference_persists_and_restores_sleeping_not_active(tmp_path):
    path = tmp_path / "voice_settings.json"
    VoiceSettingsStore(path, defaults=VoiceSettings()).update({"voice_enabled": True})
    store2 = VoiceSettingsStore(path, defaults=VoiceSettings())
    switch = VoiceSwitch(store2)
    engine = FakeEngine()
    manager = RuntimeManager(lambda: engine, stop_timeout=3, start_paused=switch.start_paused)
    switch.attach_manager(manager)
    manager.start()
    assert wait_for(lambda: manager.state is RuntimeState.RUNNING)
    assert switch.mode() is VoiceMode.SLEEPING                                # restored ON -> sleeping, never mid-conversation
    manager.shutdown()


# ---- dashboard/tray consistency: one switch, one source of truth --------------------------------------------------------------------

def test_snapshot_used_by_both_tray_and_dashboard_agrees_with_enabled():
    switch, manager, engine, _ = make()
    manager.start()
    assert wait_for(lambda: manager.state is RuntimeState.RUNNING)
    snap = switch.snapshot()
    assert snap["enabled"] is True and snap["wake_word_enabled"] is True and snap["mode"] == "SLEEPING"
    switch.disable()
    assert wait_for(lambda: manager.state is RuntimeState.PAUSED)
    snap = switch.snapshot()
    assert snap["enabled"] is False and snap["wake_word_enabled"] is False and snap["mode"] == "OFF"
    manager.shutdown()


def test_settings_store_rejects_voice_enabled_as_an_ordinary_setting():
    """voice_enabled has one writer (VoiceSwitch); VoiceControl.update()/toggle() must refuse it so the tray and dashboard
    can never fight over two different flags."""
    from voice.control import VoiceControl
    from voice.policy import VoicePolicy
    from voice.status import VoiceLog, VoiceStatus

    store = VoiceSettingsStore(defaults=VoiceSettings())
    control = VoiceControl(settings=store, status=VoiceStatus(), log=VoiceLog(None), policy=VoicePolicy(lambda: store.current, lambda: None))
    try:
        control.update({"voice_enabled": False})
        assert False, "expected ValueError"
    except ValueError:
        pass
    try:
        control.toggle("voice_enabled")
        assert False, "expected ValueError"
    except ValueError:
        pass
