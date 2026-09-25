"""VoiceControl: the one place the tray, the dashboard API and the engine share voice state.

Owns the persisted settings, the live status, the structured voice log and the announcement policy. It is built once by the
composition root and handed to the VoiceEngine, so the engine can be rebuilt (restart) without losing the user's settings and the
dashboard can still say something truthful while the engine is starting or stopped.
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from backend.core.metrics import metrics
from voice.policy import VoicePolicy, local_now_factory
from voice.settings import VoiceSettings, VoiceSettingsStore, defaults_from_config
from voice.status import Mic, VoiceLog, VoiceStatus


@dataclass
class VoiceControl:
    settings: VoiceSettingsStore
    status: VoiceStatus
    log: VoiceLog
    policy: VoicePolicy
    engine_interrupt: Any = None     # () -> None, set by the runtime once an engine exists
    announcements: Any = None        # AnnouncementQueue (for the queue length)

    # ---- commands (all validated by the settings store) ---------------------------------------------------------------

    def update(self, changes: dict[str, Any]) -> VoiceSettings:
        return self.settings.update(changes)

    def toggle(self, name: str) -> bool:
        """Flip a boolean setting and return the new value."""
        value = not getattr(self.settings.current, name)
        self.settings.update({name: value})
        return value

    def interrupt(self) -> None:
        if self.engine_interrupt is not None:
            self.engine_interrupt()

    # ---- observation --------------------------------------------------------------------------------------------------

    def snapshot(self) -> dict[str, Any]:
        s = self.settings.current
        snap = self.status.snapshot()
        timers = metrics.snapshot()["timers"]
        snap["settings"] = s.to_dict()
        snap["tts"].update({"voice": s.tts_voice, "speed": s.tts_speed, "volume": s.tts_volume, "muted": s.voice_muted})
        snap["stt"].update({"model": s.stt_model, "language": s.stt_language})
        snap["wake_word"].update({"phrase": s.wake_word, "sensitivity": s.wake_sensitivity})
        snap["notifications"] = {
            "voice_enabled": s.voice_notifications, "do_not_disturb": self.policy.dnd_active(), "dnd_manual": s.dnd_enabled,
            "dnd_scheduled": s.dnd_schedule_enabled, "queued": len(self.announcements) if self.announcements is not None else 0,
            "held": len(snap["held_notifications"]),
        }
        snap["metrics_ms"] = {name: timers[name] for name in ("wake_to_prompt_ms", "stt_ms", "conversation_ms", "tts_synthesis_ms")
                              if name in timers}
        snap["metrics_ms"].update({name: t for name, t in timers.items() if name.startswith("tool.")})
        return snap

    def microphone_state(self) -> str:
        return self.status.mic if self.status.mic != Mic.UNKNOWN else Mic.UNKNOWN


def build_voice_control(config: Any, state_dir: Path, zone, announcements=None) -> VoiceControl:
    settings = VoiceSettingsStore(state_dir / "voice_settings.json", defaults_from_config(config))
    return VoiceControl(
        settings=settings, status=VoiceStatus(), log=VoiceLog(state_dir / "voice_log.jsonl"),
        policy=VoicePolicy(lambda: settings.current, local_now_factory(zone)), announcements=announcements,
    )
