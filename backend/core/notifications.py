"""NotificationCenter: one reliable path from "something worth telling the user" to what the user actually sees or hears.

Levels and what they mean:

    CRITICAL   deliver immediately, on every channel, even in quiet hours
    IMPORTANT  notify now; during quiet hours (or privacy limits) it waits and is released afterwards; always in the briefing
    NORMAL     stored (shows up in briefings / on request), never interrupts
    LOW        recorded silently

Reliability rules, applied in this order and each with a recorded reason:
  1. muted by a preference                       -> SUPPRESSED (except CRITICAL)
  2. duplicate (same key, same content)          -> not delivered again while the earlier one is unacknowledged or in cooldown;
                                                    a real change (different content hash) or an escalation is a new notification
  3. privacy mode limits                         -> below the mode's level it is only stored
  4. level routing and quiet hours               -> as above; voice is dropped after the user's voice cutoff
  5. delivery failure                            -> kept as DEFERRED and retried by `release_deferred`, never lost

History (with acknowledgement) is persisted, so a restart neither forgets what was said nor repeats it.
"""

import hashlib
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import IntEnum, StrEnum
from pathlib import Path
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo

from backend.core.events import EventBus, SystemEvent
from backend.core.logging import get_logger
from backend.core.preferences import PreferenceStore
from backend.core.privacy import PrivacyController
from backend.core.state_store import JsonFile

logger = get_logger(__name__)

MAX_HISTORY = 500


class Level(IntEnum):
    LOW = 1
    NORMAL = 2
    IMPORTANT = 3
    CRITICAL = 4


class Status(StrEnum):
    DELIVERED = "delivered"
    DEFERRED = "deferred"  # waiting for quiet hours to end (or a failed delivery to be retried)
    STORED = "stored"
    SUPPRESSED = "suppressed"
    ACKNOWLEDGED = "acknowledged"


@dataclass
class Notification:
    notification_id: str
    dedupe_key: str
    level: int
    title: str
    body: str
    category: str
    source: str
    content_hash: str
    created_at: str
    status: str
    reason: str
    delivered_at: str | None = None
    acknowledged_at: str | None = None
    channels: list[str] = field(default_factory=list)
    refs: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


# deliver(channel, notification) -> None; raises on failure. Channels: "desktop", "voice".
Deliver = Callable[[str, Notification], None]


def _now() -> datetime:
    return datetime.now(timezone.utc)


class NotificationCenter:
    def __init__(
        self,
        *,
        preferences: PreferenceStore,
        zone: ZoneInfo,
        deliver: Deliver,
        privacy: PrivacyController | None = None,
        bus: EventBus | None = None,
        state_file: Path | None = None,
        cooldown_minutes: int = 60,
        clock: Callable[[], datetime] = _now,
    ):
        self._prefs = preferences
        self._zone = zone
        self._deliver = deliver
        self._privacy = privacy
        self._bus = bus
        self._file = JsonFile(state_file, []) if state_file else None
        self._cooldown = timedelta(minutes=cooldown_minutes)
        self._clock = clock
        self._lock = threading.RLock()
        self._items: list[Notification] = self._load()

    def _load(self) -> list[Notification]:
        if self._file is None:
            return []
        items: list[Notification] = []
        for raw in self._file.read():
            try:
                items.append(Notification(**raw))
            except TypeError:
                continue  # an unreadable record is dropped, the rest survive
        return items

    def _save(self) -> None:
        del self._items[:-MAX_HISTORY]
        if self._file is not None:
            try:
                self._file.write([n.to_dict() for n in self._items])
            except OSError as exc:
                logger.error("Could not persist notification history (%s)", type(exc).__name__)

    # ---- submitting ----------------------------------------------------------------------------------------------------

    def submit(
        self,
        *,
        dedupe_key: str,
        level: Level,
        title: str,
        body: str,
        category: str = "general",
        source: str = "jarvis",
        refs: dict[str, Any] | None = None,
    ) -> Notification | None:
        """Decide and (maybe) deliver. Returns the new record, or None when it was an exact duplicate (nothing new happened)."""
        now = self._clock()
        digest = hashlib.sha256(f"{title}\n{body}".encode()).hexdigest()[:16]
        with self._lock:
            previous = next((n for n in reversed(self._items) if n.dedupe_key == dedupe_key), None)
            if previous is not None and self._is_duplicate(previous, digest, level, now):
                logger.info("Notification suppressed as a duplicate (category=%s)", category)
                return None
            note = Notification(
                notification_id=uuid4().hex, dedupe_key=dedupe_key, level=int(level), title=title[:120], body=body[:600],
                category=category, source=source, content_hash=digest, created_at=now.isoformat(), status=Status.STORED.value,
                reason="", refs=refs or {},
            )
            self._items.append(note)
            self._route(note, level, now)
            self._save()
        if self._bus:
            self._bus.publish(SystemEvent.NOTIFICATION_CREATED, notification_id=note.notification_id, level=int(level), status=note.status)
        return note

    def _is_duplicate(self, previous: Notification, digest: str, level: Level, now: datetime) -> bool:
        if previous.content_hash != digest:
            return False  # the thing changed (a time moved, new detail): tell the user again
        if int(level) > previous.level:
            return False  # escalated
        if previous.status == Status.SUPPRESSED.value and previous.reason.startswith("muted"):
            return True
        if previous.status in (Status.DEFERRED.value, Status.STORED.value):
            return True  # already waiting
        delivered = datetime.fromisoformat(previous.delivered_at) if previous.delivered_at else None
        if previous.status == Status.ACKNOWLEDGED.value:
            return delivered is not None and now - delivered < self._cooldown
        return True  # delivered and not yet acknowledged: do not nag

    def _route(self, note: Notification, level: Level, now: datetime) -> None:
        if self._prefs.is_muted(note.category, note.title) and level < Level.CRITICAL:
            note.status, note.reason = Status.SUPPRESSED.value, "muted by your preference"
            return
        if self._prefs.is_important(note.category, note.title) and level == Level.NORMAL:
            level = Level.IMPORTANT
            note.level = int(level)
        if level <= Level.LOW:
            note.status, note.reason = Status.STORED.value, "low priority: recorded silently"
            return
        limit = self._privacy.capabilities.notifications if self._privacy else "all"
        if (limit == "critical" and level < Level.CRITICAL) or (limit == "important" and level < Level.IMPORTANT):
            note.status, note.reason = Status.STORED.value, "stored: current privacy mode limits notifications"
            return
        if level == Level.NORMAL:
            note.status, note.reason = Status.STORED.value, "normal priority: stored for the briefing"
            return
        quiet = self._prefs.in_quiet_hours(now, self._zone)
        if quiet and level < Level.CRITICAL:
            note.status, note.reason = Status.DEFERRED.value, "quiet hours: will be released afterwards"
            return
        self._deliver_now(note, level, now)

    def _deliver_now(self, note: Notification, level: Level, now: datetime) -> None:
        channels = ["desktop"]
        if level >= Level.CRITICAL or self._prefs.voice_allowed(now, self._zone):
            channels.append("voice")
        delivered: list[str] = []
        for channel in channels:
            try:
                self._deliver(channel, note)
                delivered.append(channel)
            except Exception as exc:  # noqa: BLE001 - one channel failing must not lose the notification
                logger.warning("Notification channel %s failed (%s)", channel, type(exc).__name__)
        if delivered:
            note.status, note.reason = Status.DELIVERED.value, "delivered on " + " and ".join(delivered)
            note.delivered_at, note.channels = now.isoformat(), delivered
        else:
            note.status, note.reason = Status.DEFERRED.value, "delivery failed: will retry"

    # ---- lifecycle -----------------------------------------------------------------------------------------------------

    def release_deferred(self) -> int:
        """Deliver notifications that were waiting (quiet hours ended, a channel came back). Returns how many were delivered."""
        now = self._clock()
        released = 0
        with self._lock:
            if self._prefs.in_quiet_hours(now, self._zone):
                pending = [n for n in self._items if n.status == Status.DEFERRED.value and n.level >= Level.CRITICAL]
            else:
                pending = [n for n in self._items if n.status == Status.DEFERRED.value]
            for note in pending:
                self._deliver_now(note, Level(note.level), now)
                released += note.status == Status.DELIVERED.value
            if pending:
                self._save()
        return released

    def acknowledge(self, notification_id: str) -> bool:
        with self._lock:
            for note in self._items:
                if note.notification_id == notification_id and note.status != Status.ACKNOWLEDGED.value:
                    note.status, note.acknowledged_at = Status.ACKNOWLEDGED.value, self._clock().isoformat()
                    self._save()
                    return True
        return False

    def acknowledge_all(self) -> int:
        with self._lock:
            count = 0
            for note in self._items:
                if note.status in (Status.DELIVERED.value, Status.DEFERRED.value, Status.STORED.value):
                    note.status, note.acknowledged_at = Status.ACKNOWLEDGED.value, self._clock().isoformat()
                    count += 1
            if count:
                self._save()
            return count

    def history(self, limit: int = 50) -> list[Notification]:
        with self._lock:
            return list(self._items[-limit:])

    def for_briefing(self) -> list[Notification]:
        """IMPORTANT and CRITICAL notifications the user has not acknowledged (delivered, waiting or stored)."""
        with self._lock:
            return [n for n in self._items if n.level >= Level.IMPORTANT and n.status in
                    (Status.DELIVERED.value, Status.DEFERRED.value, Status.STORED.value)]
