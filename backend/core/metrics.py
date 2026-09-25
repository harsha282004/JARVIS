"""Process-wide measurements: counters and timers, so performance claims come from measurement, not assumption.

`metrics.timer("stt")` records how long a block took; `metrics.incr("llm_calls")` counts events. `snapshot()` returns count, mean, p95 and
max per timer. Memory is bounded (the last 500 samples per timer). Nothing here records content, only names and durations.
"""

import threading
import time
from collections import defaultdict, deque
from contextlib import contextmanager

_MAX_SAMPLES = 500


class Metrics:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counters: dict[str, int] = defaultdict(int)
        self._timers: dict[str, deque[float]] = defaultdict(lambda: deque(maxlen=_MAX_SAMPLES))
        self._totals: dict[str, int] = defaultdict(int)
        self.started_at = time.time()

    def incr(self, name: str, amount: int = 1) -> None:
        with self._lock:
            self._counters[name] += amount

    def observe(self, name: str, milliseconds: float) -> None:
        with self._lock:
            self._timers[name].append(float(milliseconds))
            self._totals[name] += 1

    @contextmanager
    def timer(self, name: str):
        start = time.perf_counter()
        try:
            yield
        finally:
            self.observe(name, (time.perf_counter() - start) * 1000)

    def counter(self, name: str) -> int:
        with self._lock:
            return self._counters.get(name, 0)

    def snapshot(self) -> dict:
        with self._lock:
            timers = {}
            for name, samples in self._timers.items():
                data = sorted(samples)
                if not data:
                    continue
                timers[name] = {
                    "count": self._totals[name], "mean_ms": round(sum(data) / len(data), 2),
                    "p95_ms": round(data[min(len(data) - 1, int(len(data) * 0.95))], 2), "max_ms": round(data[-1], 2),
                }
            return {"counters": dict(self._counters), "timers": timers, "uptime_seconds": round(time.time() - self.started_at, 1)}

    def reset(self) -> None:
        with self._lock:
            self._counters.clear()
            self._timers.clear()
            self._totals.clear()


metrics = Metrics()
