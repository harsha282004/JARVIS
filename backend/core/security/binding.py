"""Action binding: a deterministic digest tying an approval to one exact action."""

import hashlib
import json
from collections.abc import Mapping
from typing import Any


def compute_action_digest(tool_name: str, action: str, parameters: Mapping[str, Any] | None) -> str:
    """SHA-256 over canonical JSON of (tool, action, parameters).

    Canonical = sorted keys, compact separators, ASCII-escaped, so the same
    logical action always hashes the same and any change to the tool, action
    or parameters changes the digest. Raises TypeError/ValueError for
    parameters that are not JSON-serializable (callers treat that as malformed).

    This detects a request being swapped for a different one inside the
    process; it is not a signature and protects nothing across processes.
    """
    canonical = json.dumps(
        {"tool": tool_name, "action": action, "parameters": dict(parameters or {})},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
