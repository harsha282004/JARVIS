"""The user's timezone: an explicit IANA name from settings, else the system's, resolved once."""

from zoneinfo import ZoneInfo

from backend.core.logging import get_logger

logger = get_logger(__name__)


def resolve_timezone(name: str) -> ZoneInfo:
    """`name` is JARVIS_TIMEZONE. Empty means "use this computer's timezone", detected once at
    startup and logged, so every later calculation uses the same explicit zone (never an implicit
    machine-local one). If detection fails, UTC is used and a warning says so."""
    if name:
        return ZoneInfo(name)
    try:
        import tzlocal

        zone = ZoneInfo(tzlocal.get_localzone_name())
        logger.info("Using the system timezone %s (set JARVIS_TIMEZONE to override)", zone.key)
        return zone
    except Exception as exc:  # noqa: BLE001 - detection is best effort
        logger.warning("Could not detect the system timezone (%s); using UTC. Set JARVIS_TIMEZONE.", type(exc).__name__)
        return ZoneInfo("UTC")
