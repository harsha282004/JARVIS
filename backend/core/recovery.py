"""Bounded retry with exponential backoff, and a supervisor that restores a failed service.

Rules: never retry in a tight loop (delays grow up to a cap), never retry forever (after `max_attempts` a service is
FAILED and is only tried again after `cooldown_seconds`), and never let one service's failure raise into another.
Time and sleeping are injected so behavior is testable without waiting.
"""

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import TypeVar

from backend.core.logging import get_logger, log_event

logger = get_logger(__name__)

T = TypeVar("T")


@dataclass(frozen=True)
class BackoffPolicy:
    initial_seconds: float = 1.0
    factor: float = 2.0
    max_seconds: float = 60.0
    max_attempts: int = 5
    cooldown_seconds: float = 300.0  # after max_attempts, wait this long before trying again

    def delay(self, attempt: int) -> float:
        """Seconds to wait before retry number `attempt` (1-based)."""
        return min(self.max_seconds, self.initial_seconds * (self.factor ** max(0, attempt - 1)))


def retry_call(
    work: Callable[[], T],
    policy: BackoffPolicy = BackoffPolicy(max_attempts=3),
    *,
    retry_on: tuple[type[BaseException], ...] = (Exception,),
    sleep: Callable[[float], None] = time.sleep,
    what: str = "operation",
) -> T:
    """Run `work`; on a `retry_on` error wait (backoff) and try again, at most `policy.max_attempts` times in total.
    The last error is re-raised. Only the exception type is logged."""
    attempt = 0
    while True:
        attempt += 1
        try:
            return work()
        except retry_on as exc:
            if attempt >= policy.max_attempts:
                logger.error("%s failed after %d attempts (%s)", what, attempt, type(exc).__name__)
                raise
            delay = policy.delay(attempt)
            logger.warning("%s failed (%s); retry %d in %.1fs", what, type(exc).__name__, attempt, delay)
            sleep(delay)


class SupervisedService:
    """One service the supervisor keeps alive: `check()` says whether it is healthy, `restart()` restores it."""

    def __init__(
        self,
        name: str,
        check: Callable[[], bool],
        restart: Callable[[], None],
        policy: BackoffPolicy = BackoffPolicy(),
        on_failed: Callable[[str], None] | None = None,
        on_recovered: Callable[[str], None] | None = None,
    ):
        self.name = name
        self.check = check
        self.restart = restart
        self.policy = policy
        self.on_failed = on_failed
        self.on_recovered = on_recovered
        self.attempts = 0
        self.next_try_at = 0.0
        self.failed = False  # exhausted its attempts; waiting out the cooldown
        self.healthy = True
        self.last_error: str | None = None


class Supervisor:
    """`tick()` looks at every service once. It is driven by a thread (`start`) or called directly in tests."""

    def __init__(self, interval_seconds: float = 5.0, clock: Callable[[], float] = time.monotonic):
        self._interval = interval_seconds
        self._clock = clock
        self._services: dict[str, SupervisedService] = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def add(self, service: SupervisedService) -> None:
        with self._lock:
            self._services[service.name] = service

    def get(self, name: str) -> SupervisedService | None:
        return self._services.get(name)

    def tick(self) -> None:
        with self._lock:
            services = list(self._services.values())
        for service in services:
            try:
                self._tick_one(service)
            except Exception as exc:  # noqa: BLE001 - supervising one service must never stop the others
                logger.error("Supervisor error for %s (%s)", service.name, type(exc).__name__)

    def _tick_one(self, s: SupervisedService) -> None:
        now = self._clock()
        try:
            ok = bool(s.check())
        except Exception as exc:  # noqa: BLE001
            ok, s.last_error = False, type(exc).__name__
        if ok:
            if not s.healthy and s.on_recovered:
                s.on_recovered(s.name)
            if not s.healthy:
                log_event(logger, "service_recovered", service=s.name)
            s.healthy, s.failed, s.attempts, s.last_error = True, False, 0, None
            return
        if s.healthy:
            s.healthy = False
            log_event(logger, "service_unhealthy", logging.WARNING, service=s.name)
            if s.on_failed:
                s.on_failed(s.name)
        if now < s.next_try_at:
            return  # still backing off
        if s.attempts >= s.policy.max_attempts:
            if not s.failed:
                s.failed = True
                s.next_try_at = now + s.policy.cooldown_seconds
                log_event(logger, "service_failed", logging.ERROR, service=s.name, attempts=s.attempts)
                return
            s.attempts, s.failed = 0, False  # cooldown over: start a fresh series
        s.attempts += 1
        s.next_try_at = now + s.policy.delay(s.attempts)
        try:
            s.restart()
            log_event(logger, "service_restart_attempted", service=s.name, attempt=s.attempts)
        except Exception as exc:  # noqa: BLE001
            s.last_error = type(exc).__name__
            log_event(logger, "service_restart_failed", logging.WARNING, service=s.name, attempt=s.attempts, error=s.last_error)

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="jarvis-supervisor", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=self._interval + 2.0)

    def _loop(self) -> None:
        while not self._stop.wait(self._interval):
            self.tick()
