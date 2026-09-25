"""Central service health monitor: one honest view of what works right now.

Each subsystem registers a check that returns a `Health` (state + short detail). The monitor runs checks, never lets a
failing check raise, and reports the ACTUAL result: a service that is not configured is DISABLED (not "healthy"), and one
that cannot be reached is DISCONNECTED or FAILED. Transitions publish INTEGRATION_FAILED / INTEGRATION_RECOVERED on the
event bus. The overall status is derived from the reports, never assumed.
"""

import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum

from backend.core.events import EventBus, SystemEvent
from backend.core.logging import get_logger

logger = get_logger(__name__)


class ServiceState(StrEnum):
    HEALTHY = "healthy"
    STARTING = "starting"
    DEGRADED = "degraded"
    DISCONNECTED = "disconnected"
    FAILED = "failed"
    DISABLED = "disabled"


_BAD = {ServiceState.DISCONNECTED, ServiceState.FAILED}


class OverallStatus(StrEnum):
    ONLINE = "online"
    STARTING = "starting"
    DEGRADED = "degraded"
    OFFLINE = "offline"


@dataclass(frozen=True)
class Health:
    state: ServiceState
    detail: str = ""  # short, content-free ("no credentials", "connection refused")


@dataclass
class ServiceReport:
    name: str
    state: ServiceState
    detail: str
    critical: bool
    checked_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass
class _Registered:
    name: str
    check: Callable[[], Health]
    critical: bool


class HealthMonitor:
    def __init__(self, bus: EventBus | None = None):
        self._bus = bus
        self._services: dict[str, _Registered] = {}
        self._last: dict[str, ServiceReport] = {}
        self._lock = threading.Lock()

    def register(self, name: str, check: Callable[[], Health], *, critical: bool = False) -> None:
        """`critical` services (voice, database) decide OFFLINE vs DEGRADED when they are bad."""
        with self._lock:
            self._services[name] = _Registered(name, check, critical)

    def check_all(self) -> list[ServiceReport]:
        with self._lock:
            registered = list(self._services.values())
        reports = [self._run(r) for r in registered]  # checks run outside the lock: they may be slow
        with self._lock:
            transitions = [(self._last.get(r.name), r) for r in reports]
            for report in reports:
                self._last[report.name] = report
        for previous, current in transitions:
            self._announce(previous, current)
        return reports

    def snapshot(self) -> list[ServiceReport]:
        """The most recent results without running any check."""
        with self._lock:
            return list(self._last.values())

    def report(self, name: str) -> ServiceReport | None:
        with self._lock:
            return self._last.get(name)

    def overall(self, reports: list[ServiceReport] | None = None) -> OverallStatus:
        reports = self.snapshot() if reports is None else reports
        if not reports:
            return OverallStatus.STARTING
        active = [r for r in reports if r.state is not ServiceState.DISABLED]
        if any(r.state is ServiceState.STARTING for r in active if r.critical):
            return OverallStatus.STARTING
        if any(r.state in _BAD for r in active if r.critical):
            return OverallStatus.OFFLINE
        if any(r.state is not ServiceState.HEALTHY for r in active):
            return OverallStatus.DEGRADED
        return OverallStatus.ONLINE

    def as_dict(self) -> dict:
        reports = self.snapshot()
        return {
            "overall": self.overall(reports).value,
            "services": [
                {"name": r.name, "state": r.state.value, "detail": r.detail, "critical": r.critical,
                 "checked_at": r.checked_at.isoformat()}
                for r in sorted(reports, key=lambda r: r.name)
            ],
        }

    # ---- internals -------------------------------------------------------

    @staticmethod
    def _run(registered: _Registered) -> ServiceReport:
        try:
            health = registered.check()
        except Exception as exc:  # noqa: BLE001 - a failing check IS the result, never an exception
            health = Health(ServiceState.FAILED, f"check raised {type(exc).__name__}")
        return ServiceReport(registered.name, health.state, health.detail, registered.critical)

    def _announce(self, previous: ServiceReport | None, current: ServiceReport) -> None:
        was_bad = previous is not None and previous.state in _BAD
        is_bad = current.state in _BAD
        if is_bad and not was_bad:
            logger.warning("Service %s is %s (%s)", current.name, current.state.value, current.detail)
            if self._bus:
                self._bus.publish(SystemEvent.INTEGRATION_FAILED, service=current.name, state=current.state.value)
        elif was_bad and current.state is ServiceState.HEALTHY:
            logger.info("Service %s recovered", current.name)
            if self._bus:
                self._bus.publish(SystemEvent.INTEGRATION_RECOVERED, service=current.name)
