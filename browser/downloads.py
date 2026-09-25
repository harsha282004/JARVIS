"""Controlled downloads and the browser's structured log.

A download is verified (it happened, with a name, size and hash), recorded (filename, source URL without its query string, time), saved only
inside the approved directory, never over an existing file, and NEVER opened or executed. Files whose type can run code are refused: they are
not saved at all, and the user is told why.
"""

import hashlib
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from backend.core.redaction import redact
from backend.core.state_store import JsonLines
from browser.driver import DownloadEvent
from browser.models import DownloadRecord, public_url

DANGEROUS_EXTENSIONS = frozenset({
    ".exe", ".msi", ".bat", ".cmd", ".com", ".scr", ".pif", ".ps1", ".psm1", ".vbs", ".vbe", ".js", ".jse", ".wsf", ".wsh", ".hta", ".lnk", ".reg",
    ".dll", ".jar", ".apk", ".msp", ".cpl", ".inf", ".gadget", ".appx", ".msix", ".iso", ".dmg", ".sh", ".py", ".docm", ".xlsm", ".pptm",
})
MAX_DOWNLOAD_BYTES = 500 * 1024 * 1024
_BAD_CHARS = re.compile(r'[\x00-\x1f<>:"/\\|?*‮]')
_RESERVED = re.compile(r"^(?:con|prn|aux|nul|com\d|lpt\d)(?:\..*)?$", re.I)


def safe_filename(name: str) -> str:
    """A file name that cannot escape the folder, hide its extension (right-to-left override), or be a Windows device name."""
    base = Path(name.replace("\\", "/")).name
    base = _BAD_CHARS.sub("_", base).strip(" .") or "download"
    if _RESERVED.match(base):
        base = "_" + base
    return base[:120]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class BrowserLog:
    """Structured, redacted, secret-free browser action log (`.jarvis/browser_log.jsonl`)."""

    def __init__(self, path: Path | None):
        self._file = JsonLines(path) if path is not None else None

    def event(self, action: str, *, session_id: str = "", target: str = "", url: str = "", duration_ms: float | None = None, verified: bool | None = None,
              result: str = "", **extra: Any) -> None:
        if self._file is None:
            return
        record = {"timestamp": _now(), "session_id": session_id, "action": action, "target": redact(target)[:120], "url": public_url(url),
                  "duration_ms": None if duration_ms is None else round(duration_ms, 1), "verified": verified, "result": redact(result)[:200]}
        record.update({k: (redact(v)[:200] if isinstance(v, str) else v) for k, v in extra.items()})
        try:
            self._file.append({k: v for k, v in record.items() if v not in (None, "")})
        except OSError:
            pass  # logging never breaks the browser

    def recent(self, limit: int = 50) -> list[dict]:
        return self._file.read(limit) if self._file is not None else []


class DownloadManager:
    def __init__(self, directory: Path, log: BrowserLog | None = None):
        self._dir = directory
        self._log = log or BrowserLog(None)
        self.records: list[DownloadRecord] = []

    def handle(self, event: DownloadEvent, session_id: str = "") -> DownloadRecord:
        name = safe_filename(event.suggested_filename)
        source = public_url(event.url)
        suffix = Path(name).suffix.lower()
        if suffix in DANGEROUS_EXTENSIONS or any(part.lower() in DANGEROUS_EXTENSIONS for part in Path(name).suffixes[:-1]):
            record = DownloadRecord(name, source, None, _now(), blocked=True, reason=f"{suffix or 'that'} files can run code, so I don't save them")
        else:
            record = self._save(event, name, source)
        self.records.append(record)
        del self.records[:-50]
        self._log.event("download", session_id=session_id, target=name, url=event.url, verified=record.saved_path is not None,
                        result="blocked: " + record.reason if record.blocked else "saved" if record.saved_path else "failed: " + record.reason,
                        size=record.size, sha256=record.sha256)
        return record

    def _save(self, event: DownloadEvent, name: str, source: str) -> DownloadRecord:
        self._dir.mkdir(parents=True, exist_ok=True)
        target = self._dir / name
        counter = 1
        while target.exists():  # never overwrite
            target = self._dir / f"{Path(name).stem} ({counter}){Path(name).suffix}"
            counter += 1
        temp = target.with_name(target.name + ".part")
        try:
            event.save(str(temp))
            size = temp.stat().st_size
            if size > MAX_DOWNLOAD_BYTES:
                temp.unlink(missing_ok=True)
                return DownloadRecord(name, source, None, _now(), size, blocked=True, reason="the file is larger than the limit")
            digest = hashlib.sha256(temp.read_bytes()).hexdigest()
            temp.replace(target)
        except Exception as exc:  # noqa: BLE001 - a failed save is reported, never reported as done
            temp.unlink(missing_ok=True)
            return DownloadRecord(name, source, None, _now(), reason=type(exc).__name__)
        return DownloadRecord(target.name, source, str(target), _now(), size, digest)
