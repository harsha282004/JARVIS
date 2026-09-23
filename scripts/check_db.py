#!/usr/bin/env python
"""Verify database connectivity using the application's configured DATABASE_URL.

Usage:
    python scripts/check_db.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.core.database import check_database_connection  # noqa: E402
from backend.core.logging import configure_logging  # noqa: E402


def main() -> int:
    configure_logging("INFO")
    if check_database_connection():
        print("Database connection: OK")
        return 0
    print("Database connection: FAILED (see log output above)")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
