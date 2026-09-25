"""One switch for "may JARVIS use this integration right now?".

The Integration Hub's registry installs itself here at startup. Every service that reads an external account (Gmail, Calendar, Telegram, GitHub, documents)
asks `enabled(name)` as part of "is it set up?", so turning an integration off in the dashboard, by voice or in settings stops ALL access at once: the
Phase 10-15 tools, the intelligence layer, briefings and the sync engine alike. Without an installed checker everything is enabled (plain library use, tests).
"""

from collections.abc import Callable

_checker: Callable[[str], bool] | None = None


def install(checker: Callable[[str], bool] | None) -> None:
    global _checker
    _checker = checker


def enabled(name: str) -> bool:
    if _checker is None:
        return True
    try:
        return bool(_checker(name))
    except Exception:  # noqa: BLE001 - if the switch itself fails, refuse (fail closed) rather than read an account the user may have switched off
        return False
