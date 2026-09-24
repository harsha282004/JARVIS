#!/usr/bin/env python
"""Set up and check the Google Calendar integration (see docs/google-calendar-integration.md).

    python scripts/calendar_cli.py status        local check: is the OAuth client / token there?
    python scripts/calendar_cli.py auth          first-time Google sign-in (opens your browser)
    python scripts/calendar_cli.py check         online check: refresh the token and list your calendars
    python scripts/calendar_cli.py events        list your events for the next few days (titles and times only)

This script only READS. It never creates, changes or deletes an event. Tokens, secrets and authorization codes are
never printed.
"""

import argparse
import sys
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent.tasks.models import utcnow  # noqa: E402
from agent.tasks.zone import resolve_timezone  # noqa: E402
from backend.core.config import get_settings  # noqa: E402
from backend.core.logging import configure_logging  # noqa: E402
from integrations.calendar.auth import SCOPES  # noqa: E402
from integrations.calendar.client import HttpCalendarClient  # noqa: E402
from integrations.calendar.models import CalendarError  # noqa: E402
from integrations.calendar.service import CalendarService  # noqa: E402
from voice.bootstrap import build_calendar_authenticator  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status")
    auth_cmd = sub.add_parser("auth")
    auth_cmd.add_argument("--no-browser", action="store_true", help="do not open a browser automatically")
    sub.add_parser("check")
    events = sub.add_parser("events")
    events.add_argument("--days", type=int, default=3)
    args = parser.parse_args(argv)

    settings = get_settings()
    configure_logging("WARNING")
    auth = build_calendar_authenticator(settings)
    try:
        if args.command == "status":
            status = auth.status()
            source = "client file" if auth.credentials_path.is_file() else "CALENDAR_/GMAIL_CLIENT_ID and SECRET in the environment"
            print(f"OAuth client: {'found (' + source + ')' if status.configured else 'MISSING'} (file: {auth.credentials_path})")
            print(f"Token: {'found' if status.authorized else 'not created yet'} ({auth.token_path})")
            print("Scopes requested: " + ", ".join(SCOPES))
            print(f"JARVIS_CALENDAR_ENABLED: {settings.JARVIS_CALENDAR_ENABLED}")
            return 0 if status.configured and status.authorized else 1
        if args.command == "auth":
            print("Opening Google's sign-in page. Approve the calendar permissions (events, and a read-only calendar list).")
            auth.authorize(open_browser=not args.no_browser)
            print(f"Done. The token was saved to {auth.token_path} (keep it private; it is git-ignored).")
            return 0
        zone = resolve_timezone(settings.JARVIS_TIMEZONE)
        service = CalendarService(HttpCalendarClient(auth, zone=zone), zone=zone, max_results=settings.JARVIS_CALENDAR_MAX_RESULTS)
        if args.command == "check":
            calendars = service.calendars(refresh=True)
            print(f"Google Calendar access works: {len(calendars)} calendar(s).")
            for c in calendars:
                print(f"- {c.summary}{' (primary)' if c.primary else ''} [{c.access_role}]")
            return 0
        now = utcnow()
        listing = service.events_between(now, now + timedelta(days=max(1, min(args.days, 14))))
        print(f"{len(listing.events)} event(s)" + (" (more exist)" if listing.truncated else ""))
        for e in listing.events:
            print(f"- {e.start.astimezone(zone):%a %d %b %H:%M}  {e.summary or '(no title)'}")
        return 0
    except CalendarError as exc:
        print(exc.user_message)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
