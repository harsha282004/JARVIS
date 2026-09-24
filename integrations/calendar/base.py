"""Calendar abstraction: the rest of JARVIS depends on CalendarClient, never on an HTTP or Google SDK call."""

from abc import ABC, abstractmethod
from datetime import datetime

from integrations.base import Integration
from integrations.calendar.models import (
    CalendarEvent,
    CalendarEventDraft,
    CalendarEventPatch,
    CalendarEventsResult,
    CalendarInfo,
)


class CalendarClient(ABC):
    """Access to one user's Google Calendars. Implementations raise CalendarError subclasses.

    Invitations are never sent by JARVIS: writes use sendUpdates=none (see docs/google-calendar-integration.md)."""

    @abstractmethod
    def list_calendars(self) -> list[CalendarInfo]:
        raise NotImplementedError

    @abstractmethod
    def list_events(
        self, calendar_id: str, time_min: datetime, time_max: datetime, max_results: int,
        query: str | None = None, page_token: str | None = None,
    ) -> CalendarEventsResult:
        """Events in [time_min, time_max) with recurring events expanded into occurrences, in start order."""
        raise NotImplementedError

    @abstractmethod
    def get_event(self, calendar_id: str, event_id: str) -> CalendarEvent:
        raise NotImplementedError

    @abstractmethod
    def create_event(self, calendar_id: str, draft: CalendarEventDraft) -> CalendarEvent:
        raise NotImplementedError

    @abstractmethod
    def update_event(
        self, calendar_id: str, event_id: str, patch: CalendarEventPatch, etag: str | None = None
    ) -> CalendarEvent:
        """Apply `patch`. With `etag`, fails (CalendarConflictError) if the event changed since it was read."""
        raise NotImplementedError

    @abstractmethod
    def delete_event(self, calendar_id: str, event_id: str, etag: str | None = None) -> None:
        raise NotImplementedError


class CalendarIntegration(Integration):
    """Reports whether Google Calendar is set up, for the Integration registry."""

    name = "google_calendar"

    def __init__(self, is_ready):
        self._is_ready = is_ready

    def is_configured(self) -> bool:
        return bool(self._is_ready())
