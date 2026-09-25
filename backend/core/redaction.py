"""Secret redaction for anything that may reach a log line or an audit record.

Deterministic pattern matching only. It is a safety net, not permission to log secrets: callers should still
never pass credentials to a logger. Redaction is idempotent and never raises.
"""

import re

REDACTED = "[REDACTED]"

_SECRET_KEYS = r"(?:api[_-]?key|secret|client[_-]?secret|access[_-]?token|refresh[_-]?token|token|password|passwd|authorization|bot[_-]?token)"
_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    # key=value / key: value / "key": "value"
    (re.compile(rf"(?i)(\"?{_SECRET_KEYS}\"?\s*[:=]\s*)(?!\[REDACTED\])(?:(?:bearer|basic)\s+)?(\"[^\"]*\"|'[^']*'|[^\s,;&}}\]]+)"), rf"\1{REDACTED}"),
    (re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=\-]{8,}"), rf"\1 {REDACTED}"),
    (re.compile(r"\bGOCSPX-[A-Za-z0-9_\-]{6,}"), REDACTED),  # Google OAuth client secret
    (re.compile(r"\bya29\.[A-Za-z0-9_\-]{10,}"), REDACTED),  # Google access token
    (re.compile(r"\b1//[A-Za-z0-9_\-]{20,}"), REDACTED),  # Google refresh token
    (re.compile(r"\bAIza[A-Za-z0-9_\-]{30,}"), REDACTED),  # Google API key
    (re.compile(r"\bsk-[A-Za-z0-9_\-]{20,}"), REDACTED),  # generic "sk-" API keys
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}"), REDACTED),  # GitHub tokens
    (re.compile(r"\b\d{6,12}:[A-Za-z0-9_\-]{30,}\b"), REDACTED),  # Telegram bot token
    # user:password@host in URLs
    (re.compile(r"(?i)\b([a-z][a-z0-9+.\-]*://[^/\s:@]+:)[^@\s/]+@"), rf"\1{REDACTED}@"),
]


def redact(text: str) -> str:
    """Return `text` with credential-shaped substrings replaced by [REDACTED]."""
    if not isinstance(text, str) or not text:
        return text if isinstance(text, str) else str(text)
    try:
        for pattern, replacement in _PATTERNS:
            text = pattern.sub(replacement, text)
    except Exception:  # noqa: BLE001 - redaction must never break logging
        return REDACTED
    return text


def redact_mapping(data: dict, _depth: int = 0) -> dict:
    """A copy of `data` with secret-named keys masked and string values redacted."""
    key_re = re.compile(_SECRET_KEYS, re.I)
    out: dict = {}
    for key, value in data.items():
        if key_re.search(str(key)):
            out[key] = REDACTED
        elif isinstance(value, dict) and _depth < 5:
            out[key] = redact_mapping(value, _depth + 1)
        elif isinstance(value, str):
            out[key] = redact(value)
        else:
            out[key] = value
    return out
