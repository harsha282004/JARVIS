"""What the tray shows, derived from facts (runtime state, service health, privacy mode). No fake status.

    🟢 Online    the voice runtime is running and no critical service is bad
    🟡 Starting  the runtime (or a critical service) is still starting
    🔴 Offline   the runtime is stopped or in error, or a critical service (database) is down
    ⏸ Paused    the user paused JARVIS or switched it to private mode (nothing is listening)
    ⚠ Degraded  running, but a service (Gmail, Calendar, LLM, ...) is not working
"""

from dataclasses import dataclass
from enum import StrEnum

from backend.core.health import OverallStatus
from backend.core.privacy import INDICATOR_TEXT, PrivacyMode, VoiceIndicator, voice_indicator
from desktop.runtime.state import RuntimeState, RuntimeStatus


class TrayState(StrEnum):
    ONLINE = "online"
    STARTING = "starting"
    OFFLINE = "offline"
    PAUSED = "paused"
    DEGRADED = "degraded"


TRAY_LABELS = {
    TrayState.ONLINE: "🟢 Online",
    TrayState.STARTING: "🟡 Starting",
    TrayState.OFFLINE: "🔴 Offline",
    TrayState.PAUSED: "⏸ Paused",
    TrayState.DEGRADED: "⚠ Degraded",
}


@dataclass(frozen=True)
class TrayView:
    state: TrayState
    label: str
    voice: VoiceIndicator
    voice_label: str
    detail: str = ""


def compute_tray_view(status: RuntimeStatus, overall: OverallStatus | None, mode: PrivacyMode) -> TrayView:
    indicator = voice_indicator(status.state.value, status.voice_state, status.microphone_active, mode)
    detail = ""
    if mode is PrivacyMode.PRIVATE:
        state, detail = TrayState.PAUSED, "private mode"
    elif status.state is RuntimeState.PAUSED or mode is PrivacyMode.PAUSED:
        state = TrayState.PAUSED
    elif status.state in (RuntimeState.STARTING, RuntimeState.STOPPING):
        state = TrayState.STARTING
    elif status.state in (RuntimeState.ERROR, RuntimeState.STOPPED):
        state, detail = TrayState.OFFLINE, status.last_error or ""
    elif overall is OverallStatus.OFFLINE:
        state = TrayState.OFFLINE
    elif overall is OverallStatus.DEGRADED:
        state = TrayState.DEGRADED
    elif overall is OverallStatus.STARTING:
        state = TrayState.STARTING
    else:
        state = TrayState.ONLINE  # RUNNING with no health monitor, or every active service healthy
    return TrayView(state, TRAY_LABELS[state], indicator, INDICATOR_TEXT[indicator], detail)
