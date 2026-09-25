"""AppContext: the running application's shared services, for the local API/dashboard.

The Windows launcher builds the services and registers them here once. Outside the launcher (for example plain `uvicorn backend.main:app`)
no context is registered and the API says so honestly instead of inventing data.
"""

import secrets
from dataclasses import dataclass, field
from typing import Any

from backend.core.config import Settings


@dataclass
class AppContext:
    settings: Settings
    bus: Any = None
    health: Any = None
    privacy: Any = None
    prefs: Any = None
    audit: Any = None
    center: Any = None
    intelligence: Any = None
    manager: Any = None
    task_system: Any = None
    hub: Any = None
    # Required on every API request except /health and the dashboard page, which embeds it (same-origin only).
    api_token: str = field(default_factory=lambda: secrets.token_urlsafe(24))


_context: AppContext | None = None


def set_context(context: AppContext | None) -> None:
    global _context
    _context = context


def get_context() -> AppContext | None:
    return _context
