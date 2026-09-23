"""Centralized logging configuration.

Keeps log setup in one place so every module gets a consistent format
(timestamp, level, module name, message) at a configurable verbosity.
No secrets should ever be passed to logger calls.
"""

import logging
import sys

_LOG_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

_configured = False


def configure_logging(level: str = "INFO") -> None:
    """Configure the root logger once. Safe to call multiple times."""
    global _configured
    if _configured:
        return

    root = logging.getLogger()
    root.setLevel(level.upper())

    handler = logging.StreamHandler(stream=sys.stdout)
    handler.setFormatter(logging.Formatter(_LOG_FORMAT, datefmt=_DATE_FORMAT))
    root.addHandler(handler)

    _configured = True


def get_logger(name: str) -> logging.Logger:
    """Return a module-scoped logger. Call configure_logging() at startup first."""
    return logging.getLogger(name)
