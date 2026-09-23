"""Lightweight security audit trail.

Events go to a bounded in-memory store and to the central logger. Nothing is
persisted: the trail is lost on exit (see docs/security-and-permissions.md).
Events carry identifiers, names and reason codes only: never action
parameters, message bodies, credentials or audio.
"""

from collections import deque
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from backend.core.logging import get_logger
from backend.core.security.models import ReasonCode

logger = get_logger("jarvis.security.audit")

DEFAULT_MAX_EVENTS = 1000


class SecurityEventType(StrEnum):
    PERMISSION_REQUESTED = "PERMISSION_REQUESTED"
    PERMISSION_APPROVED = "PERMISSION_APPROVED"
    PERMISSION_DENIED = "PERMISSION_DENIED"
    PERMISSION_EXPIRED = "PERMISSION_EXPIRED"
    PERMISSION_CANCELLED = "PERMISSION_CANCELLED"
    AUTHORIZATION_ALLOWED = "AUTHORIZATION_ALLOWED"
    AUTHORIZATION_DENIED = "AUTHORIZATION_DENIED"


@dataclass(frozen=True)
class SecurityEvent:
    timestamp: datetime
    event_type: SecurityEventType
    request_id: str | None
    tool_name: str
    action: str
    result: str
    code: ReasonCode
    session_id: str | None = None
    actor: str = ""


class AuditLog:
    def __init__(self, enabled: bool = True, max_events: int = DEFAULT_MAX_EVENTS):
        self._enabled = enabled
        self._events: deque[SecurityEvent] = deque(maxlen=max_events)

    def record(self, event: SecurityEvent) -> None:
        if not self._enabled:
            return
        self._events.append(event)
        logger.info(
            "SECURITY_EVENT type=%s request_id=%s tool=%s action=%s result=%s code=%s session=%s actor=%s",
            event.event_type.value,
            event.request_id,
            event.tool_name,
            event.action,
            event.result,
            event.code.value,
            event.session_id,
            event.actor,
        )

    def events(self) -> list[SecurityEvent]:
        return list(self._events)
