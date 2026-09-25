"""Centralized logging configuration.

Keeps log setup in one place so every module gets a consistent format at a configurable verbosity.
Two formats: the human-readable line (default) and JSON lines (`json_format=True`), which carry
timestamp, severity, component, event, error details and a correlation id. Every record passes through a
redacting filter, so a credential that slips into a message or field is masked before it is written.
Sensitive personal content (email bodies, message text) must still never be passed to a logger.
"""

import contextvars
import json
import logging
import sys
import uuid
from contextlib import contextmanager
from logging.handlers import RotatingFileHandler
from pathlib import Path

from backend.core.redaction import redact

_LOG_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

_configured = False
_correlation: contextvars.ContextVar[str | None] = contextvars.ContextVar("jarvis_correlation_id", default=None)

# LogRecord attributes that are not structured fields.
_STANDARD = set(logging.LogRecord("x", 0, "x", 0, "", (), None).__dict__) | {"message", "asctime", "event", "fields"}


def new_correlation_id() -> str:
    return uuid.uuid4().hex[:12]


def current_correlation_id() -> str | None:
    return _correlation.get()


@contextmanager
def correlation(correlation_id: str | None = None):
    """Tag every log record emitted (in this context) with one correlation id, e.g. one voice request."""
    token = _correlation.set(correlation_id or new_correlation_id())
    try:
        yield _correlation.get()
    finally:
        _correlation.reset(token)


class RedactingFilter(logging.Filter):
    """Masks credential-shaped text in the message and in string arguments; adds the correlation id."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            record.msg = redact(record.getMessage())
            record.args = ()
            fields = getattr(record, "fields", None)
            if isinstance(fields, dict):
                record.fields = {k: redact(v) if isinstance(v, str) else v for k, v in fields.items()}
        except Exception:  # noqa: BLE001 - logging must never raise
            pass
        record.correlation_id = _correlation.get()
        return True


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict = {
            "timestamp": self.formatTime(record, "%Y-%m-%dT%H:%M:%S") + f".{int(record.msecs):03d}",
            "severity": record.levelname,
            "component": record.name,
            "event": getattr(record, "event", None) or record.getMessage()[:80],
            "message": record.getMessage(),
        }
        cid = getattr(record, "correlation_id", None)
        if cid:
            payload["correlation_id"] = cid
        fields = getattr(record, "fields", None)
        if isinstance(fields, dict):
            payload.update({k: v for k, v in fields.items() if k not in payload})
        if record.exc_info:
            # exception type and location only: the message text may echo personal content
            etype = record.exc_info[0]
            payload["error"] = {"type": etype.__name__ if etype else "Exception"}
        return json.dumps(payload, default=str, ensure_ascii=False)


def log_event(logger: logging.Logger, event: str, level: int = logging.INFO, message: str | None = None, **fields) -> None:
    """Structured event: `event` is a stable snake_case name, `fields` are small non-sensitive values."""
    logger.log(level, message or event, extra={"event": event, "fields": fields})


def configure_logging(level: str = "INFO", log_file: Path | None = None, json_format: bool = False) -> None:
    """Configure the root logger once. Safe to call multiple times.

    Logs to stdout when a console exists, and additionally to `log_file`
    (rotating) when given — needed for windowless (pythonw) runs.
    """
    global _configured
    if _configured:
        return

    root = logging.getLogger()
    root.setLevel(level.upper())

    formatter: logging.Formatter = JsonFormatter() if json_format else logging.Formatter(_LOG_FORMAT, datefmt=_DATE_FORMAT)
    handlers: list[logging.Handler] = []
    if sys.stdout is not None:
        handlers.append(logging.StreamHandler(stream=sys.stdout))
    if log_file is not None:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(
            RotatingFileHandler(log_file, maxBytes=1_000_000, backupCount=3, encoding="utf-8")
        )
    for handler in handlers:
        handler.setFormatter(formatter)
        handler.addFilter(RedactingFilter())
        root.addHandler(handler)

    _configured = True


def get_logger(name: str) -> logging.Logger:
    """Return a module-scoped logger. Call configure_logging() at startup first."""
    return logging.getLogger(name)
