#!/usr/bin/env python
"""Connect JARVIS to GitHub (read-only).

    python scripts/github_cli.py token     paste a fine-grained personal access token (hidden input); it is stored encrypted (Windows DPAPI)
    python scripts/github_cli.py device    sign in with the OAuth device flow (needs GITHUB_OAUTH_CLIENT_ID in .env)
    python scripts/github_cli.py status    check the stored token against GitHub (prints the account name only)
    python scripts/github_cli.py forget    delete the stored token

Create the token at https://github.com/settings/personal-access-tokens with READ-ONLY repository permissions (Metadata, Contents, Issues, Pull requests) and only
the repositories you want JARVIS to see. The token is never printed, logged or written to .env.
"""

import getpass
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.core.config import get_settings  # noqa: E402
from integrations.github.auth import DeviceFlow, GitHubTokenStore  # noqa: E402
from integrations.github.client import GitHubClient  # noqa: E402
from integrations.github.models import GitHubError  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


def _store() -> tuple[GitHubTokenStore, object]:
    settings = get_settings()
    path = Path(settings.JARVIS_GITHUB_TOKEN_PATH)
    path = path if path.is_absolute() else ROOT / path
    return GitHubTokenStore(path, lambda: settings.GITHUB_TOKEN.get_secret_value(), settings.JARVIS_ENCRYPT_TOKENS), settings


def main(argv: list[str]) -> int:
    command = argv[0] if argv else "status"
    store, settings = _store()
    try:
        if command == "token":
            encrypted = store.save(getpass.getpass("GitHub token (input hidden): "))
            print("Token saved" + (" and encrypted for this Windows account." if encrypted else " (DPAPI unavailable: stored as plaintext in your profile)."))
            print("Now set JARVIS_GITHUB_ENABLED=true in .env and restart JARVIS.")
        elif command == "device":
            flow = DeviceFlow(settings.GITHUB_OAUTH_CLIENT_ID, settings.JARVIS_GITHUB_OAUTH_SCOPE)
            code = flow.start()
            print(f"Open {code.verification_uri} and enter the code: {code.user_code}")
            store.save(flow.poll(code))
            print("Signed in. Token stored encrypted.")
        elif command == "forget":
            print("Token deleted." if store.forget() else "There was no stored token.")
        elif command == "status":
            if not store.is_ready():
                print("GitHub is not connected (no token).")
                return 1
            print(f"GitHub reachable as {GitHubClient(store.token).viewer()}. Token storage: {'encrypted' if store.encrypted() else 'environment variable or plaintext file'}.")
        else:
            print(__doc__)
            return 2
    except GitHubError as exc:
        print(exc.user_message)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
