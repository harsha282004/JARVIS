"""REAL Google Calendar tests.

READ-ONLY tests run only when a Calendar token exists at JARVIS_CALENDAR_TOKEN_PATH (create it with
`python scripts/calendar_cli.py auth`); otherwise they SKIP and never fake a pass. Nothing from your calendar is printed.

MUTATION tests (create, update, delete) additionally require JARVIS_TEST_CALENDAR_ID: the id of a DEDICATED test calendar
that you created for this purpose (never your real calendar). Without it they skip. They only touch events they created
themselves (titled "JARVIS-TEST ..."), and delete those events at the end. No guests are ever added and nothing is emailed.
"""

import os
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from backend.core.config import get_settings
from integrations.calendar.models import CalendarEventDraft, CalendarEventPatch, CalendarNotFound

pytestmark = pytest.mark.integration


def _real_client():
    from integrations.calendar.client import HttpCalendarClient
    from voice.bootstrap import build_calendar_authenticator

    settings = get_settings()
    auth = build_calendar_authenticator(settings)
    if not auth.is_ready():
        pytest.skip("No Calendar token (run: python scripts/calendar_cli.py auth); real Google Calendar was NOT tested")
    return HttpCalendarClient(auth, zone=ZoneInfo(settings.JARVIS_TIMEZONE)), auth


def _test_calendar(client) -> str:
    calendar_id = os.environ.get("JARVIS_TEST_CALENDAR_ID", "").strip()
    if not calendar_id:
        pytest.skip("JARVIS_TEST_CALENDAR_ID is not set (a dedicated test calendar); mutation tests were NOT run")
    info = next((c for c in client.list_calendars() if c.calendar_id == calendar_id), None)
    if info is None or not info.writable:
        pytest.skip("JARVIS_TEST_CALENDAR_ID is not a writable calendar of this account")
    if info.primary:
        pytest.skip("Refusing to run mutation tests against the primary calendar; use a dedicated test calendar")
    return calendar_id


def test_real_authentication_and_token_are_valid():
    _, auth = _real_client()
    assert auth.access_token()  # refreshes if needed; the token itself is never printed


def test_real_calendar_list_and_a_bounded_event_read():
    client, _ = _real_client()
    calendars = client.list_calendars()
    assert calendars and any(c.primary for c in calendars)
    now = datetime.now(timezone.utc)
    result = client.list_events(next(c for c in calendars if c.primary).calendar_id, now, now + timedelta(days=7), 5)
    assert len(result.events) <= 5
    for event in result.events:
        assert event.event_id and event.start.tzinfo is not None and event.end >= event.start


def test_real_event_details_and_search_are_read_only():
    client, _ = _real_client()
    primary = next(c for c in client.list_calendars() if c.primary)
    now = datetime.now(timezone.utc)
    events = client.list_events(primary.calendar_id, now - timedelta(days=30), now + timedelta(days=60), 3).events
    if not events:
        pytest.skip("The primary calendar has no events in the window")
    same = client.get_event(primary.calendar_id, events[0].event_id)
    assert same.event_id == events[0].event_id
    client.list_events(primary.calendar_id, now - timedelta(days=30), now + timedelta(days=60), 3, query="zzzz-no-such-event-zzzz")  # must not raise


def test_real_missing_event_is_reported_as_not_found():
    client, _ = _real_client()
    primary = next(c for c in client.list_calendars() if c.primary)
    with pytest.raises(CalendarNotFound):
        client.get_event(primary.calendar_id, "jarvisdoesnotexist000")


def test_real_create_update_and_delete_on_a_dedicated_test_calendar():
    client, _ = _real_client()
    calendar_id = _test_calendar(client)
    zone = ZoneInfo(get_settings().JARVIS_TIMEZONE)
    start = (datetime.now(zone) + timedelta(days=30)).replace(hour=10, minute=0, second=0, microsecond=0)
    draft = CalendarEventDraft(summary="JARVIS-TEST create", start=start, end=start + timedelta(minutes=30), timezone=zone.key)
    created = client.create_event(calendar_id, draft)
    try:
        assert created.summary == "JARVIS-TEST create" and created.start == start.astimezone(timezone.utc)
        again = client.create_event(calendar_id, draft)  # the same client id: idempotent, not a duplicate
        assert again.event_id == created.event_id
        moved = client.update_event(calendar_id, created.event_id, CalendarEventPatch(
            summary="JARVIS-TEST moved", start=start + timedelta(hours=1), end=start + timedelta(hours=1, minutes=30), timezone=zone.key), created.etag)
        assert moved.summary == "JARVIS-TEST moved" and moved.start == (start + timedelta(hours=1)).astimezone(timezone.utc)
    finally:
        client.delete_event(calendar_id, created.event_id)
    with pytest.raises(CalendarNotFound):
        client.get_event(calendar_id, created.event_id)
