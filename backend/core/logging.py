"""Centralized logging configuration.

Keeps log setup in one place so every module gets a consistent format
(timestamp, level, module name, message) at a configurable verbosity.
No secrets should ever be passed to logger calls.
"""

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

_LOG_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

_configured = False


def configure_logging(level: str = "INFO", log_file: Path | None = None) -> None:
    """Configure the root logger once. Safe to call multiple times.

    Logs to stdout when a console exists, and additionally to `log_file`
    (rotating) when given — needed for windowless (pythonw) runs.
    """
    global _configured
    if _configured:
        return

    root = logging.getLogger()
    root.setLevel(level.upper())

    formatter = logging.Formatter(_LOG_FORMAT, datefmt=_DATE_FORMAT)
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
        root.addHandler(handler)

    _configured = True


def get_logger(name: str) -> logging.Logger:
    """Return a module-scoped logger. Call configure_logging() at startup first."""
    return logging.getLogger(name)
