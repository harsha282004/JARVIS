"""Notification abstraction.

    ReminderScheduler -> NotificationService.notify(message, metadata)
                           |- DesktopNotifier  (Windows tray balloon notification, local, no cloud)
                           |- VoiceNotifier    (queues an announcement the VoiceEngine speaks on its own thread)

`notify` returns normally only when the notification was actually handed to a
channel, and raises NotificationError otherwise, so the scheduler never records
a reminder as delivered when nothing was delivered. Nothing here talks to any
external service (no email, WhatsApp, push or cloud).
"""

import re
import threading
from abc import ABC, abstractmethod
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from backend.core.logging import get_logger

logger = get_logger(__name__)

MAX_NOTIFICATION_CHARS = 250  # the Windows balloon text limit is 256


class NotificationError(Exception):
    """A notification could not be delivered. Messages never contain the notification text."""


def clean_notification_text(text: str, limit: int = MAX_NOTIFICATION_CHARS) -> str:
    """Single line, printable characters only, length-limited."""
    cleaned = " ".join(re.sub(r"[\x00-\x1f\x7f]", " ", text).split())
    return cleaned[:limit]


class NotificationService(ABC):
    @abstractmethod
    def notify(self, message: str, metadata: Mapping[str, Any] | None = None) -> None:
        """Deliver `message`. Raises NotificationError if it was not delivered."""
        raise NotImplementedError


class DesktopNotifier(NotificationService):
    """Local desktop notification through an injected `send(title, message)` callable.

    In the Windows runtime that callable is the system-tray icon's balloon notification,
    so it needs the tray to be enabled and running.
    """

    def __init__(self, send: Callable[[str, str], None], title: str = "JARVIS reminder"):
        self._send = send
        self._title = title

    def notify(self, message: str, metadata: Mapping[str, Any] | None = None) -> None:
        text = clean_notification_text(message)
        if not text:
            raise NotificationError("Empty notification")
        try:
            self._send(self._title, text)
        except Exception as exc:  # noqa: BLE001 - platform backends raise assorted errors
            raise NotificationError(f"Desktop notification failed ({type(exc).__name__})") from None


PRIORITIES = ("low", "normal", "high", "critical")  # lowest to highest; the voice layer decides what may interrupt


def normalize_priority(value: Any) -> str:
    text = str(getattr(value, "name", value)).lower()
    if text == "important":
        return "high"
    return text if text in PRIORITIES else "normal"


class Announcement:
    """One queued voice announcement: the text and how much it matters."""

    __slots__ = ("text", "priority")

    def __init__(self, text: str, priority: str = "normal"):
        self.text = text
        self.priority = normalize_priority(priority)


class AnnouncementQueue:
    """Thread-safe, bounded hand-off from the scheduler thread to the VoiceEngine thread.

    The VoiceEngine marks the queue as accepting while it is running and speaks queued text
    only between conversations, on its own thread, so nothing else touches the audio devices.
    The highest priority is taken first (a critical alert is never stuck behind a low one); within a priority, oldest first.
    When full, a new item replaces the oldest *lower*-priority one, else it is refused.
    """

    def __init__(self, max_size: int = 10):
        self._items: list[Announcement] = []
        self._max = max_size
        self._lock = threading.Lock()
        self._accepting = False

    @property
    def accepting(self) -> bool:
        with self._lock:
            return self._accepting

    def set_accepting(self, value: bool) -> None:
        with self._lock:
            self._accepting = value

    def put(self, text: str, priority: str = "normal") -> bool:
        item = Announcement(text, priority)
        with self._lock:
            if not self._accepting:
                return False
            if len(self._items) >= self._max:
                lowest = min(self._items, key=lambda a: PRIORITIES.index(a.priority))
                if PRIORITIES.index(lowest.priority) >= PRIORITIES.index(item.priority):
                    return False
                self._items.remove(lowest)
            self._items.append(item)
            return True

    def get_item_nowait(self) -> Announcement | None:
        with self._lock:
            if not self._items:
                return None
            best = max(range(len(self._items)), key=lambda i: (PRIORITIES.index(self._items[i].priority), -i))
            return self._items.pop(best)

    def get_nowait(self) -> str | None:
        item = self.get_item_nowait()
        return item.text if item is not None else None

    def has_at_least(self, priority: str) -> bool:
        """True if something at `priority` or higher is waiting (used to decide whether a conversation may be interrupted)."""
        floor = PRIORITIES.index(normalize_priority(priority))
        with self._lock:
            return any(PRIORITIES.index(a.priority) >= floor for a in self._items)

    def __len__(self) -> int:
        with self._lock:
            return len(self._items)


class VoiceNotifier(NotificationService):
    """Queues the text for the VoiceEngine to speak. "Delivered" here means accepted by a running
    voice engine; it is spoken when the engine is next between conversations (see the docs)."""

    def __init__(self, queue: AnnouncementQueue):
        self._queue = queue

    def notify(self, message: str, metadata: Mapping[str, Any] | None = None) -> None:
        text = clean_notification_text(message)
        if not text:
            raise NotificationError("Empty notification")
        if not self._queue.accepting:
            raise NotificationError("The voice engine is not running")
        priority = normalize_priority((metadata or {}).get("priority", "normal"))
        if not self._queue.put(text, priority):
            raise NotificationError("The voice announcement queue is full")


class CompositeNotifier(NotificationService):
    """Send to every channel; succeed if at least one delivered, raise if none did."""

    def __init__(self, channels: Sequence[tuple[str, NotificationService]]):
        self._channels = list(channels)

    def notify(self, message: str, metadata: Mapping[str, Any] | None = None) -> None:
        delivered = 0
        for name, channel in self._channels:
            try:
                channel.notify(message, metadata)
                delivered += 1
            except Exception as exc:  # noqa: BLE001 - one broken channel must not stop the others
                logger.warning("Notification channel %s failed (%s)", name, type(exc).__name__)
        if delivered == 0:
            raise NotificationError("No notification channel delivered the message")
