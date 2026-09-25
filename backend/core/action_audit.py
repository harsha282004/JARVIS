"""Durable action audit log: what JARVIS actually did, when, on whose authority, and how it turned out.

One JSON line per important action (tool, timestamp, action, result, confirmation, source). It survives restarts (the
in-memory `security.AuditLog` covers permission decisions inside one run). Every text field is redacted for credentials and
bounded; passwords, tokens and message bodies are never stored, and a caller cannot ask for them to be.
"""

import threading
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from typing import Any

from backend.core.redaction import redact, redact_mapping
from backend.core.state_store import JsonLines

MAX_FIELD_CHARS = 200


class ActionResult(StrEnum):
    SUCCESS = "success"
    FAILED = "failed"
    DENIED = "denied"
    DECLINED = "declined"  # the user said no
    UNVERIFIED = "unverified"  # the tool ran but the result could not be confirmed


class Confirmation(StrEnum):
    NONE = "none"  # read-only / planning
    POLICY = "policy"  # auto-approved by policy (low risk, user asked in this turn)
    USER = "user"  # the user explicitly confirmed this exact action


class ActionAuditLog:
    def __init__(self, path: Path | None):
        self._store = JsonLines(path) if path else None
        self._recent: list[dict[str, Any]] = []  # also kept in memory so it works without a state directory
        self._lock = threading.Lock()

    def record(
        self,
        *,
        tool: str,
        action: str,
        result: ActionResult,
        confirmation: Confirmation = Confirmation.NONE,
        source: str = "user",
        detail: str = "",
        refs: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "tool": _clip(tool),
            "action": _clip(action),
            "result": result.value,
            "confirmation": confirmation.value,
            "source": _clip(source),
            "detail": _clip(detail),
        }
        if refs:
            entry["refs"] = {k: _clip(str(v)) for k, v in redact_mapping(refs).items()}
        with self._lock:
            self._recent.append(entry)
            del self._recent[:-500]
        if self._store is not None:
            self._store.append(entry)
        return entry

    def entries(self, limit: int = 50) -> list[dict[str, Any]]:
        if self._store is not None:
            return self._store.read(limit)
        with self._lock:
            return self._recent[-limit:]


def _clip(value: str) -> str:
    return redact(" ".join(str(value).split()))[:MAX_FIELD_CHARS]
