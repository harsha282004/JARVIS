"""Find the Google Desktop OAuth client JSON in the secrets folder by its structure, not by its file name.

Only key names are inspected (an "installed" object with client_id, client_secret and an auth/token URI). No value is returned, logged or printed.
"""

import json
from pathlib import Path

MAX_CANDIDATE_BYTES = 16 * 1024


def _looks_like_desktop_client(path: Path) -> bool:
    try:
        if path.stat().st_size > MAX_CANDIDATE_BYTES:
            return False
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    installed = data.get("installed") if isinstance(data, dict) else None
    return isinstance(installed, dict) and all(isinstance(installed.get(k), str) and installed[k] for k in ("client_id", "client_secret", "auth_uri", "token_uri"))


def discover_client_file(secrets_dir: Path) -> Path | None:
    """The single Desktop-client JSON in `secrets_dir` (newest first if several); None when there is none."""
    try:
        candidates = sorted((p for p in secrets_dir.glob("*.json") if p.is_file()), key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError:
        return None
    return next((p for p in candidates if _looks_like_desktop_client(p)), None)
