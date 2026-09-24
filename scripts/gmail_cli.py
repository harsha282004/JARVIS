#!/usr/bin/env python
"""Set up and check the read-only Gmail integration (see docs/gmail-intelligence.md).

    python scripts/gmail_cli.py status            local check: is the OAuth client file / token there?
    python scripts/gmail_cli.py auth              first-time Google sign-in (opens your browser; read-only scope)
    python scripts/gmail_cli.py check             online check: refresh the token and list 1 message
    python scripts/gmail_cli.py search "<query>"  list a few matching emails (sender and subject only)

Tokens, secrets and authorization codes are never printed.
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.core.config import get_settings  # noqa: E402
from backend.core.logging import configure_logging  # noqa: E402
from integrations.gmail.auth import GMAIL_READONLY_SCOPE  # noqa: E402
from integrations.gmail.client import HttpGmailClient  # noqa: E402
from integrations.gmail.models import GmailError  # noqa: E402
from integrations.gmail.query import sanitize_query  # noqa: E402
from voice.bootstrap import build_gmail_authenticator  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status")
    auth_cmd = sub.add_parser("auth")
    auth_cmd.add_argument("--no-browser", action="store_true", help="print nothing sensitive and do not open a browser")
    sub.add_parser("check")
    search = sub.add_parser("search")
    search.add_argument("query")
    search.add_argument("--max", type=int, default=5)
    args = parser.parse_args(argv)

    settings = get_settings()
    configure_logging("WARNING")
    auth = build_gmail_authenticator(settings)

    try:
        if args.command == "status":
            status = auth.status()
            source = "client file" if auth.credentials_path.is_file() else ("GMAIL_CLIENT_ID/SECRET in the environment" if status.configured else "")
            print(f"OAuth client: {'found (' + source + ')' if status.configured else 'MISSING'} (file: {auth.credentials_path})")
            print(f"Token: {'found' if status.authorized else 'not created yet'} ({auth.token_path})")
            print(f"Scope requested: {GMAIL_READONLY_SCOPE}")
            print(f"JARVIS_GMAIL_ENABLED: {settings.JARVIS_GMAIL_ENABLED}")
            return 0 if status.configured and status.authorized else 1
        if args.command == "auth":
            print("Opening Google's sign-in page. Approve the read-only Gmail permission.")
            auth.authorize(open_browser=not args.no_browser)
            print(f"Done. The token was saved to {auth.token_path} (keep it private; it is git-ignored).")
            return 0
        client = HttpGmailClient(auth)
        if args.command == "check":
            result = client.search("", 1)
            print(f"Gmail read-only access works (a search returned {result.count} message).")
            return 0
        result = client.search(sanitize_query(args.query), max(1, min(args.max, 20)))
        print(f"{result.count} shown, about {result.estimated_total} match")
        for message in result.messages:
            print(f"- {message.describe()}{' [unread]' if message.is_unread else ''}")
        return 0
    except GmailError as exc:
        print(exc.user_message)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
