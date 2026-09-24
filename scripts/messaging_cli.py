#!/usr/bin/env python
"""Check the messaging integration (see docs/messaging-integration.md).

    python scripts/messaging_cli.py status         local check: is a bot token there? (never prints it)
    python scripts/messaging_cli.py check          online check: ask Telegram who the bot is (getMe)
    python scripts/messaging_cli.py conversations  list the conversations JARVIS can currently read (names only)

This script only READS. It never sends, edits, deletes or marks a message, and it never acknowledges Telegram updates.
Tokens and message text are never printed.
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.core.config import get_settings  # noqa: E402
from backend.core.logging import configure_logging  # noqa: E402
from integrations.messaging.models import MessagingError  # noqa: E402
from voice.bootstrap import build_messaging_registry  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("status", "check", "conversations"):
        sub.add_parser(name)
    args = parser.parse_args(argv)

    settings = get_settings()
    configure_logging("WARNING")
    registry = build_messaging_registry(settings)
    try:
        if args.command == "status":
            ready = False
            for provider in registry.all():
                configured = provider.is_configured()
                ready = ready or configured
                print(f"{provider.display_name}: bot token {'found' if configured else 'MISSING or malformed'}")
                print(f"  capabilities: {', '.join(sorted(c.value for c in provider.capabilities))}")
            print(f"JARVIS_MESSAGING_ENABLED: {settings.JARVIS_MESSAGING_ENABLED}")
            print(f"Token file: {settings.JARVIS_MESSAGING_TELEGRAM_TOKEN_PATH} (or MESSAGING_TELEGRAM_BOT_TOKEN in .env)")
            return 0 if ready else 1
        for provider in registry.all():
            if args.command == "check":
                identity = provider.authenticate()
                print(f"{provider.display_name} access works: bot {identity.account or '(no username)'}.")
            else:
                found = provider.list_conversations(20)  # type: ignore[attr-defined]
                print(f"{len(found)} conversation(s) in the messages {provider.display_name} currently lets JARVIS read")
                for c in found:
                    print(f"- {c.display} [{c.kind.value}]")
        return 0
    except MessagingError as exc:
        print(exc.user_message)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
