"""In-process event bus: subsystems publish facts, others react, and nobody imports each other.

Delivery is synchronous, in subscription order, on the publishing thread. A handler that raises is logged (type only)
and never stops the publisher or the other handlers, so one broken subscriber cannot break a subsystem. Payloads carry
references and short labels, never message bodies or credentials.
"""

import threading
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any

from backend.core.logging import get_logger

logger = get_logger(__name__)


class SystemEvent(StrEnum):
    SYSTEM_START = "system_start"
    SYSTEM_SHUTDOWN = "system_shutdown"
    SYSTEM_SLEEP = "system_sleep"
    SYSTEM_RESUME = "system_resume"
    VOICE_WAKE = "voice_wake"
    VOICE_COMMAND = "voice_command"
    AGENT_REQUEST = "agent_request"
    AGENT_RESPONSE = "agent_response"
    EMAIL_RECEIVED = "email_received"
    CALENDAR_UPDATED = "calendar_updated"
    TASK_CREATED = "task_created"
    TASK_COMPLETED = "task_completed"
    REMINDER_TRIGGERED = "reminder_triggered"
    DEADLINE_DETECTED = "deadline_detected"
    NOTIFICATION_CREATED = "notification_created"
    INTEGRATION_FAILED = "integration_failed"
    INTEGRATION_RECOVERED = "integration_recovered"
    PRIVACY_CHANGED = "privacy_changed"
    POWER_STATE_CHANGED = "power_state_changed"
    # Phase 18 integration hub
    INTEGRATION_CONNECTED = "integration_connected"
    INTEGRATION_DISCONNECTED = "integration_disconnected"
    INTEGRATION_SYNC_STARTED = "integration_sync_started"
    INTEGRATION_SYNC_COMPLETED = "integration_sync_completed"
    INTEGRATION_SYNC_FAILED = "integration_sync_failed"
    CALENDAR_EVENT_CREATED = "calendar_event_created"
    CALENDAR_EVENT_UPDATED = "calendar_event_updated"
    GITHUB_ACTIVITY_RECEIVED = "github_activity_received"
    DOCUMENT_INDEXED = "document_indexed"


@dataclass(frozen=True)
class Event:
    type: SystemEvent
    payload: dict[str, Any] = field(default_factory=dict)
    at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


Handler = Callable[[Event], None]


class EventBus:
    def __init__(self, history: int = 200):
        self._lock = threading.Lock()
        self._handlers: dict[SystemEvent | None, list[Handler]] = {}
        self._recent: deque[Event] = deque(maxlen=history)

    def subscribe(self, event_type: SystemEvent | None, handler: Handler) -> Callable[[], None]:
        """Register `handler` for one event type (None = every event). Returns an unsubscribe function."""
        with self._lock:
            self._handlers.setdefault(event_type, []).append(handler)

        def unsubscribe() -> None:
            with self._lock:
                handlers = self._handlers.get(event_type, [])
                if handler in handlers:
                    handlers.remove(handler)

        return unsubscribe

    def publish(self, event_type: SystemEvent, **payload: Any) -> Event:
        event = Event(event_type, payload)
        with self._lock:
            self._recent.append(event)
            handlers = [*self._handlers.get(event_type, []), *self._handlers.get(None, [])]
        for handler in handlers:
            try:
                handler(event)
            except Exception as exc:  # noqa: BLE001 - a subscriber must never break the publisher
                logger.error("Event handler failed (event=%s, error=%s)", event_type.value, type(exc).__name__)
        return event

    def recent(self, event_type: SystemEvent | None = None) -> list[Event]:
        with self._lock:
            return [e for e in self._recent if event_type is None or e.type is event_type]

    def clear(self) -> None:
        with self._lock:
            self._handlers.clear()
            self._recent.clear()
