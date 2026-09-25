"""What the voice system is doing right now, for the dashboard and tray, and the structured voice log.

`VoiceStatus` is a thread-safe snapshot holder written by the voice thread and read by the API/tray thread.
It contains state names, short text the user just said or heard, timings and error *kinds*: never audio, never secrets.
`VoiceLog` appends one JSON line per voice event (session_id, state, transcription, intent, tool, latency_ms, result),
redacted, size-rotated, and also without audio.
"""

import threading
import time
import uuid
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from backend.core.redaction import redact
from backend.core.state_store import JsonLines


class Mic:
    CONNECTED = "MICROPHONE_CONNECTED"
    DISCONNECTED = "MICROPHONE_DISCONNECTED"
    UNKNOWN = "MICROPHONE_UNKNOWN"   # not opened yet
    PERMISSION_DENIED = "MICROPHONE_PERMISSION_DENIED"
    CLOSED = "MICROPHONE_CLOSED"     # deliberately released (paused / private mode)


class TTS:
    IDLE = "TTS_IDLE"
    GENERATING = "TTS_GENERATING"
    SPEAKING = "TTS_SPEAKING"
    INTERRUPTED = "TTS_INTERRUPTED"
    ERROR = "TTS_ERROR"


class Overall:
    IDLE = "IDLE"
    LISTENING = "LISTENING"
    PROCESSING = "PROCESSING"
    SPEAKING = "SPEAKING"
    ERROR = "ERROR"


_OVERALL = {"waiting": Overall.IDLE, "listening": Overall.LISTENING, "transcribing": Overall.PROCESSING,
            "thinking": Overall.PROCESSING, "speaking": Overall.SPEAKING}


def new_session_id() -> str:
    return uuid.uuid4().hex[:12]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _short(text: str | None, limit: int) -> str | None:
    if text is None:
        return None
    text = redact(" ".join(text.split()))
    return text if len(text) <= limit else text[: limit - 1] + "…"


class VoiceLog:
    """Structured, redacted, audio-free voice event log."""

    def __init__(self, path: Path | None):
        self._file = JsonLines(path) if path is not None else None

    def event(self, event: str, *, session_id: str | None = None, state: str | None = None, transcription: str | None = None,
              intent: str | None = None, tool: str | None = None, latency_ms: float | None = None, result: str | None = None,
              **extra: Any) -> None:
        if self._file is None:
            return
        record = {"ts": _now(), "event": event, "session_id": session_id, "state": state,
                  "transcription": _short(transcription, 300), "intent": intent, "tool": tool,
                  "latency_ms": None if latency_ms is None else round(latency_ms, 1), "result": _short(result, 200)}
        record.update({k: (_short(v, 200) if isinstance(v, str) else v) for k, v in extra.items()})
        try:
            self._file.append({k: v for k, v in record.items() if v is not None})
        except OSError:
            pass  # logging must never break the voice loop

    def recent(self, limit: int = 50) -> list[dict]:
        return self._file.read(limit) if self._file is not None else []


class VoiceStatus:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.mic = Mic.UNKNOWN
        self.tts = TTS.IDLE
        self.voice_state = "waiting"
        self.wake_ready: bool | None = None   # None = the engine has not been built yet (unknown), never a claim of health
        self.wake_health = "unknown"
        self.stt_ready: bool | None = None
        self.tts_ready: bool | None = None
        self.conversation_active = False
        self.pending_action: str | None = None   # a question JARVIS asked and is waiting for the user to answer
        self.last_transcription: str | None = None
        self.last_confidence: float | None = None
        self.last_response: str | None = None
        self.last_error: str | None = None
        self.last_activation_at: str | None = None
        self.activations = 0
        self.false_activations = 0
        self.interruptions = 0
        self.suppressed: deque[dict] = deque(maxlen=20)   # announcements held back by DND/mute (kept, not lost)
        self.latency: dict[str, float] = {}
        self.degraded: list[str] = []

    def update(self, **fields: Any) -> None:
        with self._lock:
            for name, value in fields.items():
                if not hasattr(self, name):
                    raise AttributeError(name)
                if name in ("last_transcription", "last_response", "last_error") and isinstance(value, str):
                    value = redact(value)  # what is shown on the dashboard never carries a credential the user happened to say
                setattr(self, name, value)

    def set_latency(self, name: str, ms: float) -> None:
        with self._lock:
            self.latency[name] = round(ms, 1)

    def hold(self, text: str, priority: str, reason: str) -> None:
        with self._lock:
            self.suppressed.append({"at": _now(), "priority": priority, "reason": reason, "text": _short(text, 200)})

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            overall = Overall.ERROR if self.last_error and self.voice_state == "waiting" and self.mic in (
                Mic.DISCONNECTED, Mic.PERMISSION_DENIED) else _OVERALL.get(self.voice_state, Overall.IDLE)
            return {
                "state": overall, "voice_state": self.voice_state, "microphone": self.mic, "tts_state": self.tts,
                "wake_word": {"ready": self.wake_ready, "health": self.wake_health, "activations": self.activations,
                              "false_activations": self.false_activations, "last_activation_at": self.last_activation_at},
                "stt": {"ready": self.stt_ready, "last_confidence": self.last_confidence},
                "tts": {"ready": self.tts_ready},
                "conversation": {"active": self.conversation_active, "pending_action": self.pending_action},
                "last_transcription": self.last_transcription, "last_response": self.last_response,
                "last_error": self.last_error, "interruptions": self.interruptions,
                "held_notifications": list(self.suppressed), "latency_ms": dict(self.latency), "degraded": list(self.degraded),
            }


def timestamp() -> float:
    return time.monotonic()
