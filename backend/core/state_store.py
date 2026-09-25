"""Small durable local stores for JARVIS's own state (preferences, privacy mode, audit trail, timeline, ...).

Everything lives under one state directory (default `.jarvis/`, git-ignored). Writes are atomic (temp file + replace), so a
crash or power loss never leaves a half-written file, and a corrupted file is moved aside (`.corrupt`) and reported instead of
crashing startup. These stores hold JARVIS's own metadata; personal records stay in PostgreSQL and the source systems.
"""

import json
import os
import threading
from pathlib import Path
from typing import Any

from backend.core.logging import get_logger

logger = get_logger(__name__)

_LOCKS: dict[str, threading.RLock] = {}
_LOCKS_GUARD = threading.Lock()


def _lock_for(path: Path) -> threading.RLock:
    key = str(path.resolve())
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(key, threading.RLock())


class JsonFile:
    """One JSON document with atomic writes and corruption recovery."""

    def __init__(self, path: Path, default: Any):
        self.path = Path(path)
        self._default = default
        self._lock = _lock_for(self.path)

    def read(self) -> Any:
        with self._lock:
            try:
                return json.loads(self.path.read_text(encoding="utf-8"))
            except FileNotFoundError:
                return _copy(self._default)
            except (json.JSONDecodeError, UnicodeDecodeError, OSError) as exc:
                aside = self.path.with_suffix(self.path.suffix + ".corrupt")
                try:
                    os.replace(self.path, aside)
                except OSError:
                    pass
                logger.error("State file %s was unreadable (%s); moved aside and using defaults", self.path.name, type(exc).__name__)
                return _copy(self._default)

    def write(self, data: Any) -> None:
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(self.path.suffix + ".tmp")
            tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
            os.replace(tmp, self.path)

    def update(self, fn) -> Any:
        """Read-modify-write under the file lock. `fn` receives the data and returns the new data."""
        with self._lock:
            new = fn(self.read())
            self.write(new)
            return new


class JsonLines:
    """Append-only JSON-lines log with size-based rotation. Unreadable lines are skipped, never fatal."""

    def __init__(self, path: Path, max_bytes: int = 2_000_000, backups: int = 3):
        self.path = Path(path)
        self._max_bytes = max_bytes
        self._backups = backups
        self._lock = _lock_for(self.path)

    def append(self, record: dict[str, Any]) -> None:
        line = json.dumps(record, ensure_ascii=False, default=str)
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._rotate_if_needed(len(line) + 1)
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")

    def read(self, limit: int | None = None) -> list[dict[str, Any]]:
        """Records oldest-first (current file only); with `limit`, the most recent `limit`."""
        with self._lock:
            try:
                lines = self.path.read_text(encoding="utf-8").splitlines()
            except FileNotFoundError:
                return []
            except (OSError, UnicodeDecodeError):
                return []
        records: list[dict[str, Any]] = []
        for line in lines:
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                records.append(value)
        return records[-limit:] if limit else records

    def _rotate_if_needed(self, incoming: int) -> None:
        try:
            size = self.path.stat().st_size
        except FileNotFoundError:
            return
        if size + incoming <= self._max_bytes:
            return
        for index in range(self._backups, 0, -1):
            src = self.path.with_suffix(self.path.suffix + (f".{index - 1}" if index > 1 else ""))
            dst = self.path.with_suffix(self.path.suffix + f".{index}")
            if src.exists():
                os.replace(src, dst)


def _copy(value: Any) -> Any:
    return json.loads(json.dumps(value))
