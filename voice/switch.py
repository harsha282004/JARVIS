"""VoiceSwitch: the one JARVIS voice ON/OFF control, shared by the tray, the dashboard API and startup.

    ON  -> the RuntimeManager runs the voice worker: microphone open, wake detector + VAD + STT available, state SLEEPING until a wake phrase.
    OFF -> the RuntimeManager pauses the worker: the microphone is released and no wake/VAD/STT code runs at all (the application, dashboard and
           integrations keep running). "OFF" is a real shutdown of the voice pipeline, not a flag the audio loop ignores.

The preference is persisted (`voice_enabled` in the voice settings file), so a saved OFF survives a restart: JARVIS starts, the tray and dashboard
come up, and the microphone stays closed. Privacy modes (PAUSED/PRIVATE) also close the microphone; the pipeline runs only when BOTH the switch is on
and privacy allows it, and `reconcile()` is the only place that decision is applied, so tray, dashboard and privacy can never disagree.

Public vocabulary (`VoiceMode`): VOICE_OFF, VOICE_SLEEPING (enabled, waiting for a wake phrase), VOICE_LISTENING (a conversation is open),
VOICE_PROCESSING (speech recognition / agent), VOICE_SPEAKING (text to speech).
"""

import threading
from collections.abc import Callable
from enum import StrEnum
from typing import Any

from backend.core.logging import get_logger
from voice.settings import VoiceSettingsStore

logger = get_logger(__name__)


class VoiceMode(StrEnum):
    OFF = "VOICE_OFF"
    SLEEPING = "VOICE_SLEEPING"
    LISTENING = "VOICE_LISTENING"
    PROCESSING = "VOICE_PROCESSING"
    SPEAKING = "VOICE_SPEAKING"

    @property
    def short(self) -> str:
        return self.value.removeprefix("VOICE_")


_ENGINE_TO_MODE = {"waiting": VoiceMode.SLEEPING, "listening": VoiceMode.LISTENING, "transcribing": VoiceMode.PROCESSING,
                   "thinking": VoiceMode.PROCESSING, "speaking": VoiceMode.SPEAKING}


def derive_mode(enabled: bool, runtime_state: str | None, engine_state: str | None) -> VoiceMode:
    """OFF unless the switch is on AND the worker is really running; otherwise the engine's own state, in the public vocabulary."""
    if not enabled or runtime_state != "running":
        return VoiceMode.OFF
    return _ENGINE_TO_MODE.get(engine_state or "waiting", VoiceMode.SLEEPING)


class VoiceSwitch:
    """Thread-safe, idempotent ON/OFF over a RuntimeManager. `manager` is attached after construction (the manager needs the switch for start-up)."""

    def __init__(self, settings: VoiceSettingsStore, mic_allowed: Callable[[], bool] = lambda: True, interrupt: Callable[[], None] | None = None):
        self._settings = settings
        self._mic_allowed = mic_allowed          # privacy: False while PAUSED / PRIVATE
        self._interrupt = interrupt              # stop speaking now, so a disable does not wait for a sentence to finish
        self._manager: Any = None
        self._lock = threading.RLock()
        self.deferred_resume = False             # switched ON while privacy held the microphone closed: start when privacy allows it

    def attach_manager(self, manager: Any) -> None:
        self._manager = manager
        manager.add_listener(self._on_runtime_change)

    # ---- state ---------------------------------------------------------------------------------------------------------------------

    @property
    def enabled(self) -> bool:
        return bool(self._settings.current.voice_enabled)

    def wanted_running(self) -> bool:
        """Should the voice worker be running now? Both the user's switch and privacy must allow it."""
        return self.enabled and bool(self._mic_allowed())

    def mode(self) -> VoiceMode:
        manager = self._manager
        if manager is None:
            return VoiceMode.OFF
        status = manager.status()
        return derive_mode(self.enabled, status.state.value, status.voice_state)

    def snapshot(self) -> dict[str, Any]:
        mode = self.mode()
        return {"enabled": self.enabled, "mode": mode.short, "voice_mode": mode.value, "privacy_allows_microphone": bool(self._mic_allowed()),
                "wake_word_enabled": mode is not VoiceMode.OFF}

    # ---- commands ------------------------------------------------------------------------------------------------------------------

    def enable(self) -> bool:
        """Turn the voice on. True if this changed the preference. Idempotent: a second call opens nothing."""
        return self._set(True)

    def disable(self) -> bool:
        """Turn the voice off: worker stopped, microphone released. True if this changed the preference. Idempotent."""
        return self._set(False)

    def toggle(self) -> bool:
        with self._lock:
            self._set(not self.enabled)
            return self.enabled

    def _set(self, value: bool) -> bool:
        with self._lock:
            changed = self.enabled != value
            if changed:
                self._settings.update({"voice_enabled": value})
                self.deferred_resume = value and not self._mic_allowed()
                logger.info("VOICE_ENABLED" if value else "VOICE_DISABLED")
            self.reconcile()
            return changed

    def reconcile(self) -> None:
        """Make the runtime match the wanted state (never blocks on the worker thread itself; safe to call repeatedly)."""
        manager = self._manager
        if manager is None:
            return
        with self._lock:
            state = manager.state.value
            if self.wanted_running():
                if state == "paused":
                    manager.resume()
                # starting / running: nothing to do (starting consults wanted_running when the engine is built); stopped / error belong to the app lifecycle and the supervisor
            elif state in ("running", "starting"):
                if self._interrupt is not None:
                    try:
                        self._interrupt()
                    except Exception:  # noqa: BLE001 - failing to cut speech must not block the shutdown
                        pass
                if state == "running":
                    manager.pause()
                # starting: the worker checks `start_paused` once the engine exists; _on_runtime_change re-checks when it reaches RUNNING

    def start_paused(self) -> bool:
        """Consulted by the RuntimeManager once the engine is built: True = do not open the microphone."""
        return not self.wanted_running()

    def _on_runtime_change(self, status: Any) -> None:
        """A STARTING -> RUNNING transition can race with a disable; re-check off the worker thread (pause() joins that thread)."""
        if status.state.value == "running" and not self.wanted_running():
            threading.Thread(target=self.reconcile, name="jarvis-voice-reconcile", daemon=True).start()
