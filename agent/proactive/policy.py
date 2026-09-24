"""NotificationPolicy: the one deterministic, explainable place that decides whether a signal may notify the user now.

Rules, in this order (the first that applies wins; every decision carries a short reason):

  1. expired        the signal's expiry has passed                                        -> EXPIRE
  2. duplicate      this exact signal was already delivered (or is being delivered)       -> SUPPRESS
  3. confidence     below MEDIUM (a guess)                                                -> SUPPRESS
  4. priority       LOW priority is only worth a notification when SOON or IMMEDIATE      -> SUPPRESS
  5. quiet hours    inside the configured window: only CRITICAL + IMMEDIATE may break     -> DEFER (else tray only)
                    through, and only to the tray (never voice); everything else waits
  6. cooldown       the same source was notified less than the cooldown ago, unless the
                    urgency has since ESCALATED (e.g. "due tomorrow" then "due in an hour")
                    or the thing MEANINGFULLY CHANGED (its due/start time moved)            -> SUPPRESS
  7. hourly limit   at most N notifications per hour, except IMMEDIATE ones                -> DEFER
  8. channels       tray always; voice for SOON/IMMEDIATE or HIGH/CRITICAL priority       -> DELIVER

Nothing here looks at wording, and no language model takes part: priority and urgency come from the source data
(TaskPriority, times), so a model can never talk its way past quiet hours or the limits.
"""

from dataclasses import dataclass
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from agent.memory.models import Confidence
from agent.proactive.models import (
    Channel,
    HistoryRecord,
    PolicyAction,
    PolicyDecision,
    ProactiveSignal,
    Urgency,
)
from agent.tasks.models import TaskPriority

MIN_CONFIDENCE = Confidence.MEDIUM
IMMEDIATE_MINUTES = 15
SOON_MINUTES = 60


def urgency_for(minutes_until: float, lookahead_minutes: int) -> Urgency:
    """Urgency from time alone: overdue or within 15 minutes -> IMMEDIATE, within an hour -> SOON, within the lookahead
    -> UPCOMING, otherwise NORMAL (never notified)."""
    if minutes_until <= IMMEDIATE_MINUTES:
        return Urgency.IMMEDIATE
    if minutes_until <= SOON_MINUTES:
        return Urgency.SOON
    if minutes_until <= lookahead_minutes:
        return Urgency.UPCOMING
    return Urgency.NORMAL


def parse_clock(value: str) -> time:
    """'HH:MM' -> time. Raises ValueError otherwise."""
    hours, _, minutes = value.strip().partition(":")
    if not (hours.isdigit() and minutes.isdigit() and len(minutes) == 2):
        raise ValueError("expected HH:MM")
    return time(int(hours), int(minutes))


@dataclass(frozen=True)
class PolicyConfig:
    enabled: bool = True
    quiet_hours_enabled: bool = True
    quiet_start: time = time(23, 0)
    quiet_end: time = time(7, 0)
    cooldown_minutes: int = 60
    lookahead_minutes: int = 1440
    max_per_hour: int = 6


class NotificationPolicy:
    def __init__(self, config: PolicyConfig, zone: ZoneInfo):
        self._config = config
        self._zone = zone

    @property
    def config(self) -> PolicyConfig:
        return self._config

    def in_quiet_hours(self, now: datetime) -> bool:
        c = self._config
        if not c.quiet_hours_enabled or c.quiet_start == c.quiet_end:
            return False
        local = now.astimezone(self._zone).time().replace(second=0, microsecond=0)
        if c.quiet_start < c.quiet_end:
            return c.quiet_start <= local < c.quiet_end
        return local >= c.quiet_start or local < c.quiet_end  # the window crosses midnight

    def decide(
        self,
        signal: ProactiveSignal,
        now: datetime,
        *,
        existing: HistoryRecord | None,
        last_for_source: HistoryRecord | None,
        delivered_last_hour: int,
        available: frozenset[Channel],
    ) -> PolicyDecision:
        c = self._config
        if not c.enabled:
            return PolicyDecision(action=PolicyAction.SUPPRESS, reason="proactive notifications are turned off")
        if signal.expires_at is not None and signal.expires_at <= now:
            return PolicyDecision(action=PolicyAction.EXPIRE, reason="expired")
        if existing is not None and existing.status.value in ("delivered", "pending"):
            return PolicyDecision(action=PolicyAction.SUPPRESS, reason="already notified about this")
        if signal.confidence < MIN_CONFIDENCE:
            return PolicyDecision(action=PolicyAction.SUPPRESS, reason="low confidence")
        if signal.priority <= TaskPriority.LOW and signal.urgency < Urgency.SOON:
            return PolicyDecision(action=PolicyAction.SUPPRESS, reason="low priority and not soon")
        if signal.urgency <= Urgency.NORMAL:
            return PolicyDecision(action=PolicyAction.SUPPRESS, reason="not within the lookahead")

        quiet = self.in_quiet_hours(now)
        breakthrough = quiet and signal.priority >= TaskPriority.CRITICAL and signal.urgency >= Urgency.IMMEDIATE
        if quiet and not breakthrough:
            return PolicyDecision(action=PolicyAction.DEFER, reason="quiet hours")

        if last_for_source is not None and last_for_source.delivered_at is not None:
            recent = now - last_for_source.delivered_at < timedelta(minutes=c.cooldown_minutes)
            unchanged = last_for_source.relevant_at == signal.relevant_at
            if recent and unchanged and signal.urgency <= last_for_source.urgency:
                return PolicyDecision(action=PolicyAction.SUPPRESS, reason="cooldown: told about this source recently")

        if delivered_last_hour >= c.max_per_hour and signal.urgency < Urgency.IMMEDIATE:
            return PolicyDecision(action=PolicyAction.DEFER, reason="hourly notification limit")

        channels = self._channels(signal, available, tray_only=breakthrough)
        if not channels:
            return PolicyDecision(action=PolicyAction.DEFER, reason="no notification channel available")
        reason = "critical and immediate: allowed during quiet hours (tray only)" if breakthrough else "allowed by policy"
        return PolicyDecision(action=PolicyAction.DELIVER, reason=reason, channels=channels)

    @staticmethod
    def _channels(signal: ProactiveSignal, available: frozenset[Channel], *, tray_only: bool) -> list[Channel]:
        chosen: list[Channel] = []
        if Channel.DESKTOP in available:
            chosen.append(Channel.DESKTOP)
        if not tray_only and Channel.VOICE in available:
            wants_voice = signal.urgency >= Urgency.SOON or signal.priority >= TaskPriority.HIGH
            if wants_voice or not chosen:  # with no tray, voice is the fallback channel
                chosen.append(Channel.VOICE)
        return chosen
