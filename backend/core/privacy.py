"""Privacy modes and the truthful voice indicator.

    ACTIVE      normal: microphone and wake word on, monitoring on, all notifications
    BACKGROUND  screen locked / away: the wake word may stay on, monitoring on, only important notifications
    PAUSED      user paused JARVIS: microphone and wake word off, monitoring continues quietly (stores, does not speak)
    PRIVATE     nothing listens and nothing external is observed: microphone, wake word and external monitoring are off,
                only CRITICAL notifications are shown

The mode is persisted, so a PRIVATE choice survives a restart (JARVIS never silently re-enables the microphone after a reboot).
`VoiceIndicator` derives what the tray/dashboard shows from the actual runtime facts, never from a guess.
"""

import threading
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from backend.core.events import EventBus, SystemEvent
from backend.core.logging import get_logger
from backend.core.state_store import JsonFile

logger = get_logger(__name__)


class PrivacyMode(StrEnum):
    ACTIVE = "active"
    BACKGROUND = "background"
    PAUSED = "paused"
    PRIVATE = "private"


@dataclass(frozen=True)
class Capabilities:
    microphone: bool
    wake_word: bool
    external_monitoring: bool
    notifications: str  # "all" | "important" | "critical"


_CAPS = {
    PrivacyMode.ACTIVE: Capabilities(True, True, True, "all"),
    PrivacyMode.BACKGROUND: Capabilities(True, True, True, "important"),
    PrivacyMode.PAUSED: Capabilities(False, False, True, "important"),
    PrivacyMode.PRIVATE: Capabilities(False, False, False, "critical"),
}


def capabilities_for(mode: PrivacyMode) -> Capabilities:
    return _CAPS[mode]


Listener = Callable[[PrivacyMode, PrivacyMode], None]


class PrivacyController:
    def __init__(self, state_file: Path | None = None, bus: EventBus | None = None, default: PrivacyMode = PrivacyMode.ACTIVE):
        self._file = JsonFile(state_file, {}) if state_file else None
        self._bus = bus
        self._lock = threading.Lock()
        self._listeners: list[Listener] = []
        self._mode = self._load(default)

    def _load(self, default: PrivacyMode) -> PrivacyMode:
        if self._file is None:
            return default
        try:
            return PrivacyMode(self._file.read().get("mode", default.value))
        except ValueError:
            logger.warning("Stored privacy mode was not recognized; using %s", default.value)
            return default

    @property
    def mode(self) -> PrivacyMode:
        with self._lock:
            return self._mode

    @property
    def capabilities(self) -> Capabilities:
        return capabilities_for(self.mode)

    def add_listener(self, listener: Listener) -> None:
        self._listeners.append(listener)

    def set_mode(self, mode: PrivacyMode) -> bool:
        """Change the mode. Returns False if it was already `mode`. Persists before notifying listeners."""
        with self._lock:
            old = self._mode
            if old is mode:
                return False
            self._mode = mode
        if self._file is not None:
            try:
                self._file.write({"mode": mode.value})
            except OSError as exc:
                logger.error("Could not persist the privacy mode (%s)", type(exc).__name__)
        logger.info("Privacy mode changed: %s -> %s", old.value, mode.value)
        if self._bus:
            self._bus.publish(SystemEvent.PRIVACY_CHANGED, old=old.value, new=mode.value)
        for listener in list(self._listeners):
            try:
                listener(old, mode)
            except Exception as exc:  # noqa: BLE001 - one listener must not undo a privacy change
                logger.error("Privacy listener failed (%s)", type(exc).__name__)
        return True


class VoiceIndicator(StrEnum):
    LISTENING = "listening"
    PAUSED = "paused"
    MICROPHONE_DISABLED = "microphone_disabled"
    PROCESSING = "processing"
    SPEAKING = "speaking"
    UNAVAILABLE = "unavailable"


INDICATOR_TEXT = {
    VoiceIndicator.LISTENING: "🎙 Listening",
    VoiceIndicator.PAUSED: "⏸ Paused",
    VoiceIndicator.MICROPHONE_DISABLED: "🔇 Microphone disabled",
    VoiceIndicator.PROCESSING: "💬 Processing",
    VoiceIndicator.SPEAKING: "🔊 Speaking",
    VoiceIndicator.UNAVAILABLE: "⚠ Voice unavailable",
}


def voice_indicator(runtime_state: str, voice_state: str | None, microphone_active: bool, mode: PrivacyMode) -> VoiceIndicator:
    """What the user is told about the microphone, derived from facts:

    - PRIVATE mode, or a runtime that is paused, means the microphone is off and the user is told so;
    - "listening" is only ever reported when the microphone stream is actually open;
    - a runtime in error/stopped, or one still starting, is "unavailable", never "listening".
    """
    if mode is PrivacyMode.PRIVATE:
        return VoiceIndicator.MICROPHONE_DISABLED
    if runtime_state == "paused" or mode is PrivacyMode.PAUSED:
        return VoiceIndicator.PAUSED
    if runtime_state != "running":
        return VoiceIndicator.UNAVAILABLE
    if voice_state == "speaking":
        return VoiceIndicator.SPEAKING
    if voice_state in ("transcribing", "thinking"):
        return VoiceIndicator.PROCESSING
    if microphone_active:
        return VoiceIndicator.LISTENING
    return VoiceIndicator.UNAVAILABLE
