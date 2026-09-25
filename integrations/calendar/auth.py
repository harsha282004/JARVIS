"""Google Calendar OAuth 2.0: the shared Google OAuth handling (Phase 10 pattern), configured for Calendar.

Least privilege: the calendar API offers `calendar` (everything, including sharing and deleting whole calendars).
JARVIS asks only for what it does:
  calendar.events                      view and edit events on the user's calendars (read, create, update, delete events)
  calendar.calendarlist.readonly       see which calendars exist (names, time zones, access role), read-only
It cannot create, delete or re-share calendars and cannot change their settings.
The token is a separate file from Gmail's (different scopes); see integrations/google_oauth.py for token handling.
"""

from pathlib import Path

from integrations.calendar.models import (
    CalendarAuthError,
    CalendarAuthRevoked,
    CalendarNotConfigured,
    CalendarUnavailable,
)
from integrations.google_oauth import GoogleAuthenticator, OAuthErrors

CALENDAR_EVENTS_SCOPE = "https://www.googleapis.com/auth/calendar.events"
CALENDAR_LIST_SCOPE = "https://www.googleapis.com/auth/calendar.calendarlist.readonly"
SCOPES = (CALENDAR_EVENTS_SCOPE, CALENDAR_LIST_SCOPE)

_ERRORS = OAuthErrors(
    not_configured=CalendarNotConfigured, auth_error=CalendarAuthError, revoked=CalendarAuthRevoked,
    unavailable=CalendarUnavailable,
)


class CalendarAuthenticator(GoogleAuthenticator):
    def __init__(
        self,
        credentials_path: str | Path,
        token_path: str | Path,
        client_id: str = "",
        client_secret: str = "",
        encrypt_at_rest: bool = False,
    ):
        super().__init__(
            credentials_path, token_path, client_id, client_secret, scopes=SCOPES, label="Google Calendar", errors=_ERRORS, encrypt_at_rest=encrypt_at_rest
        )
