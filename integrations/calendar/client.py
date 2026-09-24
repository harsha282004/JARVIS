"""HttpCalendarClient: Google Calendar REST calls over httpx.

- Fixed host and fixed endpoint shapes: calendarList (GET); calendars/{id}/events (GET list, POST insert);
  calendars/{id}/events/{id} (GET, PATCH, DELETE). The base URL, methods and paths are constants; ids are
  validated and percent-encoded before they reach a URL; the model can influence none of it.
- Writes always send sendUpdates=none: Google emails nobody (no invitations, no update notices).
- Bounded retries with exponential backoff (429 rate limits, 5xx, network errors), honouring Retry-After
  up to a cap; a 401 refreshes the token once. Nothing loops forever. A retried insert is safe because JARVIS
  supplies the event id (a repeat answers 409 and the existing event is returned). If a write's outcome cannot
  be confirmed after the last attempt, CalendarOutcomeUnknown says so instead of guessing.
- Errors map to CalendarError subclasses whose text never contains event content or tokens.
"""

import re
import time
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote
from zoneinfo import ZoneInfo

import httpx

from backend.core.logging import get_logger
from integrations.calendar.base import CalendarClient
from integrations.calendar.models import (
    CalendarConflictError,
    CalendarError,
    CalendarEvent,
    CalendarEventDraft,
    CalendarEventPatch,
    CalendarEventsResult,
    CalendarInfo,
    CalendarInvalid,
    CalendarNotFound,
    CalendarOutcomeUnknown,
    CalendarPermissionDenied,
    CalendarRateLimited,
    CalendarResponseError,
    CalendarUnavailable,
    CalendarAuthError,
)
from integrations.calendar.parser import parse_calendar, parse_event

logger = get_logger(__name__)

CALENDAR_API_BASE = "https://www.googleapis.com/calendar/v3"
MAX_ATTEMPTS = 4
MAX_BACKOFF_SECONDS = 8.0
MAX_RETRY_AFTER_SECONDS = 20.0
HARD_MAX_EVENTS = 100
MAX_LIST_PAGES = 3
MAX_CALENDARS = 50
_CALENDAR_ID = re.compile(r"^[A-Za-z0-9_.@\-#%]{1,200}$")
_EVENT_ID = re.compile(r"^[A-Za-z0-9_\-]{1,1024}$")
_RATE_REASONS = {"ratelimitexceeded", "userratelimitexceeded", "quotaexceeded", "dailylimitexceeded", "rateLimitExceeded".lower()}


def validate_calendar_id(value: str) -> str:
    if not isinstance(value, str) or not _CALENDAR_ID.match(value):
        raise CalendarNotFound("malformed calendar id")
    return value


def validate_event_id(value: str) -> str:
    if not isinstance(value, str) or not _EVENT_ID.match(value):
        raise CalendarNotFound("malformed event id")
    return value


def _rfc3339(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _time_field(value: datetime, all_day: bool, zone_name: str) -> dict[str, Any]:
    local = value.astimezone(ZoneInfo(zone_name))
    if all_day:
        return {"date": local.date().isoformat()}
    return {"dateTime": local.isoformat(), "timeZone": zone_name}


def draft_body(draft: CalendarEventDraft) -> dict[str, Any]:
    body: dict[str, Any] = {
        "id": draft.event_id, "summary": draft.summary,
        "start": _time_field(draft.start, draft.all_day, draft.timezone),
        "end": _time_field(draft.end, draft.all_day, draft.timezone),
    }
    if draft.location:
        body["location"] = draft.location
    if draft.description:
        body["description"] = draft.description
    if draft.attendees:
        body["attendees"] = [{"email": e} for e in draft.attendees]
    if draft.recurrence:
        body["recurrence"] = list(draft.recurrence)
    return body


def patch_body(patch: CalendarEventPatch, zone_name: str, all_day: bool) -> dict[str, Any]:
    body: dict[str, Any] = {}
    if patch.summary is not None:
        body["summary"] = patch.summary
    if patch.location is not None:
        body["location"] = patch.location
    if patch.description is not None:
        body["description"] = patch.description
    if patch.start is not None and patch.end is not None:
        zone = patch.timezone or zone_name
        body["start"] = _time_field(patch.start, all_day, zone)
        body["end"] = _time_field(patch.end, all_day, zone)
    return body


class HttpCalendarClient(CalendarClient):
    def __init__(
        self,
        authenticator: Any,
        *,
        zone: ZoneInfo,
        http: httpx.Client | None = None,
        timeout_seconds: float = 15.0,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self._auth = authenticator
        self._zone = zone
        self._http = http or httpx.Client(timeout=timeout_seconds, follow_redirects=False)
        self._sleep = sleep

    def close(self) -> None:
        self._http.close()

    # ---- CalendarClient --------------------------------------------------------------------------------------

    def list_calendars(self) -> list[CalendarInfo]:
        data = self._request("GET", "users/me/calendarList", params={"maxResults": MAX_CALENDARS, "showHidden": "false"})
        calendars = []
        for item in data.get("items") or []:
            try:
                calendars.append(parse_calendar(item))
            except CalendarResponseError:
                continue
        return calendars

    def list_events(
        self, calendar_id: str, time_min: datetime, time_max: datetime, max_results: int,
        query: str | None = None, page_token: str | None = None,
    ) -> CalendarEventsResult:
        cid = validate_calendar_id(calendar_id)
        limit = max(1, min(int(max_results), HARD_MAX_EVENTS))
        events: list[CalendarEvent] = []
        token, pages = page_token, 0
        while len(events) < limit and pages < MAX_LIST_PAGES:
            params: dict[str, Any] = {
                "timeMin": _rfc3339(time_min), "timeMax": _rfc3339(time_max), "maxResults": limit - len(events),
                "singleEvents": "true", "orderBy": "startTime", "showDeleted": "false",
            }
            if query:
                params["q"] = query
            if token:
                params["pageToken"] = token
            data = self._request("GET", f"calendars/{quote(cid, safe='')}/events", params=params)
            pages += 1
            for item in data.get("items") or []:
                try:
                    event = parse_event(item, cid, self._zone)
                except (CalendarResponseError, ValueError):
                    continue  # one malformed event does not hide the others
                if not event.is_cancelled and len(events) < limit:
                    events.append(event)
            token = data.get("nextPageToken") if isinstance(data.get("nextPageToken"), str) else None
            if not token:
                break
        return CalendarEventsResult(events=events, next_page_token=token, truncated=token is not None)

    def get_event(self, calendar_id: str, event_id: str) -> CalendarEvent:
        cid, eid = validate_calendar_id(calendar_id), validate_event_id(event_id)
        data = self._request("GET", f"calendars/{quote(cid, safe='')}/events/{quote(eid, safe='')}")
        if data.get("status") == "cancelled":
            raise CalendarNotFound("event was deleted")
        return parse_event(data, cid, self._zone)

    def create_event(self, calendar_id: str, draft: CalendarEventDraft) -> CalendarEvent:
        cid = validate_calendar_id(calendar_id)
        validate_event_id(draft.event_id)
        try:
            data = self._request("POST", f"calendars/{quote(cid, safe='')}/events", params={"sendUpdates": "none"},
                                 json_body=draft_body(draft), mutation=True)
        except _AlreadyExists:
            return self.get_event(cid, draft.event_id)  # a retried insert that had in fact succeeded
        return parse_event(data, cid, self._zone)

    def update_event(
        self, calendar_id: str, event_id: str, patch: CalendarEventPatch, etag: str | None = None
    ) -> CalendarEvent:
        cid, eid = validate_calendar_id(calendar_id), validate_event_id(event_id)
        current = self.get_event(cid, eid)  # the body needs the event's current zone and all-day shape
        body = patch_body(patch, current.timezone or self._zone.key, current.all_day if patch.all_day is None else patch.all_day)
        if not body:
            raise CalendarInvalid("nothing to change")
        data = self._request("PATCH", f"calendars/{quote(cid, safe='')}/events/{quote(eid, safe='')}",
                             params={"sendUpdates": "none"}, json_body=body, mutation=True,
                             headers={"If-Match": etag} if etag else None)
        return parse_event(data, cid, self._zone)

    def delete_event(self, calendar_id: str, event_id: str, etag: str | None = None) -> None:
        cid, eid = validate_calendar_id(calendar_id), validate_event_id(event_id)
        self._request("DELETE", f"calendars/{quote(cid, safe='')}/events/{quote(eid, safe='')}",
                      params={"sendUpdates": "none"}, mutation=True, gone_ok_after_retry=True,
                      headers={"If-Match": etag} if etag else None)

    # ---- transport --------------------------------------------------------------------------------------------

    def _request(
        self, method: str, path: str, *, params: dict[str, Any] | None = None, json_body: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None, mutation: bool = False, gone_ok_after_retry: bool = False,
    ) -> dict[str, Any]:
        refreshed = False
        uncertain = False  # a previous attempt of a write may have reached Google
        for attempt in range(1, MAX_ATTEMPTS + 1):
            token = self._auth.access_token()
            try:
                response = self._http.request(
                    method, f"{CALENDAR_API_BASE}/{path}", params=params, json=json_body,
                    headers={"Authorization": f"Bearer {token}", **(headers or {})},
                )
            except httpx.HTTPError as exc:
                logger.warning("Calendar network error (%s), attempt %d", type(exc).__name__, attempt)
                uncertain = uncertain or mutation
                if attempt == MAX_ATTEMPTS:
                    raise (CalendarOutcomeUnknown("network failure") if mutation else CalendarUnavailable("network failure")) from None
                self._backoff(attempt, None)
                continue

            status = response.status_code
            if 200 <= status < 300:
                if status == 204 or not response.content:
                    return {}
                try:
                    data = response.json()
                except ValueError:
                    raise CalendarResponseError("response was not JSON") from None
                if not isinstance(data, dict):
                    raise CalendarResponseError("response was not an object")
                return data
            if status == 401:
                if refreshed:
                    logger.error("Calendar rejected a freshly refreshed token")
                    raise CalendarAuthError("token rejected")
                refreshed = True
                self._auth.invalidate()
                logger.info("Calendar answered 401; refreshing the access token once")
                continue
            if status == 409 and mutation and method == "POST":
                raise _AlreadyExists()
            if status == 412:
                raise CalendarConflictError("etag mismatch")
            if status in (404, 410):
                if gone_ok_after_retry and uncertain:
                    return {}  # the earlier, unanswered delete had in fact worked
                raise CalendarNotFound("not found")
            if status == 429 or (status == 403 and self._is_rate_limit(response)):
                logger.warning("Calendar rate limited the request (attempt %d)", attempt)
                if attempt == MAX_ATTEMPTS:
                    raise CalendarRateLimited("rate limited")
                self._backoff(attempt, response.headers.get("Retry-After"))
                continue
            if status == 403:
                logger.error("Calendar denied the request (403)")
                raise CalendarPermissionDenied("forbidden")
            if status == 400:
                logger.error("Calendar rejected the request as invalid (400)")
                raise CalendarInvalid("bad request")
            if status >= 500:
                logger.warning("Calendar API error %d (attempt %d)", status, attempt)
                uncertain = uncertain or mutation
                if attempt == MAX_ATTEMPTS:
                    raise (CalendarOutcomeUnknown(f"server error {status}") if mutation else CalendarUnavailable(f"server error {status}"))
                self._backoff(attempt, None)
                continue
            logger.error("Calendar API returned unexpected status %d", status)
            raise CalendarError(f"unexpected status {status}")
        raise CalendarUnavailable("retries exhausted")  # unreachable: the loop always returns or raises

    @staticmethod
    def _is_rate_limit(response: httpx.Response) -> bool:
        try:
            errors = response.json().get("error", {}).get("errors", [])
            return any(str(e.get("reason", "")).lower() in _RATE_REASONS for e in errors if isinstance(e, dict))
        except (ValueError, AttributeError):
            return False

    def _backoff(self, attempt: int, retry_after: str | None) -> None:
        delay = min(0.5 * 2 ** (attempt - 1), MAX_BACKOFF_SECONDS)
        if retry_after:
            try:
                delay = max(delay, min(float(retry_after), MAX_RETRY_AFTER_SECONDS))
            except ValueError:
                pass
        self._sleep(delay)


class _AlreadyExists(Exception):
    """Internal: an insert answered 409 because the event id already exists (the insert had succeeded)."""
