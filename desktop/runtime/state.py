"""Runtime lifecycle states and the status snapshot exposed to the tray/CLI.

`RuntimeState` describes the *application* lifecycle. It is deliberately
separate from `voice.engine.VoiceState`, which describes what the voice
pipeline is doing within a single wake-word cycle.
"""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


class RuntimeState(StrEnum):
    STARTING = "starting"
    RUNNING = "running"
    PAUSED = "paused"
    STOPPING = "stopping"
    STOPPED = "stopped"
    ERROR = "error"


@dataclass(frozen=True)
class RuntimeStatus:
    state: RuntimeState
    voice_state: str | None
    started_at: datetime
    last_error: str | None
    microphone_active: bool
