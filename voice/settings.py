"""Persisted, validated voice settings (one file, atomic writes, corruption-safe).

Defaults come from the application `Settings` (.env); what the user changes at run time (tray, dashboard) is saved
in `.jarvis/voice_settings.json` and wins after a restart. Nothing here holds secrets or audio.
"""

import threading
from collections.abc import Callable
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import Any

from backend.core.logging import get_logger
from backend.core.state_store import JsonFile

logger = get_logger(__name__)

# name -> (min, max) for numeric settings; anything outside is clamped, never trusted.
RANGES: dict[str, tuple[float, float]] = {
    "wake_sensitivity": (0.05, 0.99),
    "tts_speed": (0.5, 2.0),
    "tts_volume": (0.0, 1.0),
    "silence_seconds": (0.3, 5.0),
    "max_utterance_seconds": (2.0, 60.0),
    "min_utterance_seconds": (0.1, 3.0),
    "speech_threshold": (0.001, 0.5),
    "conversation_timeout_seconds": (3.0, 600.0),
    "spoken_max_chars": (80, 2000),
    "stt_min_confidence": (0.0, 1.0),
    "barge_in_threshold": (0.02, 0.9),
}


@dataclass
class VoiceSettings:
    wake_word: str = "hey_jarvis"
    wake_sensitivity: float = 0.5          # openWakeWord score threshold; higher = fewer false activations
    microphone: str = ""                   # "" = system default
    stt_model: str = "base"
    stt_language: str = "en"
    stt_min_confidence: float = 0.35       # below this a spoken "yes" cannot confirm a pending action
    tts_voice: str = "en_US-lessac-medium"
    tts_speed: float = 1.0                 # 1.0 normal, 1.25 faster
    tts_volume: float = 1.0
    silence_seconds: float = 1.0           # end of utterance after this much silence
    max_utterance_seconds: float = 15.0
    min_utterance_seconds: float = 0.15    # a one-word "Stop" is short; shorter blips are noise
    speech_threshold: float = 0.015        # RMS (0..1 of full scale) above the adaptive noise floor
    conversation_timeout_seconds: float = 20.0  # follow-ups are heard without the wake word for this long
    spoken_max_chars: int = 320            # voice replies longer than this are summarised; full text stays on the dashboard
    voice_muted: bool = False              # JARVIS does not speak (tray); text still appears on the dashboard
    voice_notifications: bool = True
    dnd_enabled: bool = False              # manual do-not-disturb
    dnd_schedule_enabled: bool = False
    dnd_start: str = "22:00"
    dnd_end: str = "07:00"
    dnd_allow_critical: bool = True
    barge_in: str = "wake_word"            # how speech is interrupted: wake_word ("Hey JARVIS, stop"), vad (any loud speech; use a headset), off
    barge_in_threshold: float = 0.08       # RMS level for barge_in=vad

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _coerce(name: str, value: Any, default: Any) -> Any:
    """Convert `value` to the type of `default`; clamp numbers to their range; raise ValueError when it is nonsense."""
    if isinstance(default, bool):
        if isinstance(value, bool):
            return value
        if isinstance(value, str) and value.lower() in ("true", "false"):
            return value.lower() == "true"
        raise ValueError(f"{name} must be true or false")
    if isinstance(default, (int, float)):
        if isinstance(value, bool) or not isinstance(value, (int, float, str)):
            raise ValueError(f"{name} must be a number")
        try:
            number = float(value)
        except ValueError:
            raise ValueError(f"{name} must be a number") from None
        if number != number:  # NaN
            raise ValueError(f"{name} must be a number")
        low, high = RANGES.get(name, (float("-inf"), float("inf")))
        number = min(max(number, low), high)
        return int(number) if isinstance(default, int) else number
    if not isinstance(value, str):
        raise ValueError(f"{name} must be text")
    value = value.strip()
    if name in ("dnd_start", "dnd_end"):
        parts = value.split(":")
        if len(parts) != 2 or not all(p.isdigit() for p in parts) or not (0 <= int(parts[0]) < 24 and 0 <= int(parts[1]) < 60):
            raise ValueError(f"{name} must be HH:MM")
        return f"{int(parts[0]):02d}:{int(parts[1]):02d}"
    if name == "barge_in":
        if value not in ("wake_word", "vad", "off"):
            raise ValueError("barge_in must be wake_word, vad or off")
        return value
    if len(value) > 200 or any(ord(c) < 32 for c in value):
        raise ValueError(f"{name} is not valid text")
    return value


class VoiceSettingsStore:
    """Thread-safe holder. `update()` validates, persists, and tells listeners (the engine applies changes live)."""

    def __init__(self, path: Path | None = None, defaults: VoiceSettings | None = None):
        self._file = JsonFile(path, {}) if path is not None else None
        self._lock = threading.RLock()
        self._listeners: list[Callable[[VoiceSettings], None]] = []
        self._settings = defaults or VoiceSettings()
        self._load()

    def _load(self) -> None:
        if self._file is None:
            return
        saved = self._file.read()
        if not isinstance(saved, dict):
            return
        known = {f.name: f for f in fields(VoiceSettings)}
        for name, value in saved.items():
            if name not in known:
                continue
            try:
                setattr(self._settings, name, _coerce(name, value, getattr(self._settings, name)))
            except ValueError:
                logger.warning("Ignoring invalid saved voice setting %s", name)

    @property
    def current(self) -> VoiceSettings:
        with self._lock:
            return VoiceSettings(**asdict(self._settings))

    def add_listener(self, listener: Callable[[VoiceSettings], None]) -> None:
        self._listeners.append(listener)

    def update(self, changes: dict[str, Any]) -> VoiceSettings:
        """Apply `changes` atomically: if any value is invalid nothing changes (ValueError)."""
        known = {f.name for f in fields(VoiceSettings)}
        with self._lock:
            staged = VoiceSettings(**asdict(self._settings))
            for name, value in changes.items():
                if name not in known:
                    raise ValueError(f"unknown voice setting: {name}")
                setattr(staged, name, _coerce(name, value, getattr(staged, name)))
            if staged.min_utterance_seconds >= staged.max_utterance_seconds:
                raise ValueError("min_utterance_seconds must be below max_utterance_seconds")
            self._settings = staged
            if self._file is not None:
                self._file.write(staged.to_dict())
            snapshot = VoiceSettings(**asdict(staged))
        for listener in list(self._listeners):
            try:
                listener(snapshot)
            except Exception:  # noqa: BLE001 - a broken listener must not undo a saved setting
                logger.exception("Voice settings listener failed")
        return snapshot


def defaults_from_config(config: Any) -> VoiceSettings:
    """Initial values from the application settings (.env)."""
    s = VoiceSettings()
    s.wake_sensitivity = float(config.WAKE_WORD_THRESHOLD)
    s.microphone = config.MICROPHONE_DEVICE
    s.stt_model = config.STT_MODEL
    s.stt_language = config.STT_LANGUAGE
    s.tts_voice = config.TTS_VOICE
    s.tts_speed = float(getattr(config, "VOICE_TTS_SPEED", s.tts_speed))
    s.tts_volume = float(getattr(config, "VOICE_TTS_VOLUME", s.tts_volume))
    s.silence_seconds = float(getattr(config, "VOICE_SILENCE_SECONDS", s.silence_seconds))
    s.max_utterance_seconds = float(getattr(config, "VOICE_MAX_UTTERANCE_SECONDS", s.max_utterance_seconds))
    s.conversation_timeout_seconds = float(getattr(config, "VOICE_CONVERSATION_TIMEOUT_SECONDS", s.conversation_timeout_seconds))
    s.speech_threshold = float(getattr(config, "VOICE_SPEECH_THRESHOLD", s.speech_threshold))
    s.spoken_max_chars = int(getattr(config, "VOICE_SPOKEN_MAX_CHARS", s.spoken_max_chars))
    s.dnd_allow_critical = bool(getattr(config, "VOICE_DND_ALLOW_CRITICAL", True))
    return s
