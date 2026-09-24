"""Validation of Gmail search queries proposed by the model.

The model never builds an API call. It may only offer search text, which is checked here token by token
against a small whitelist of read-only Gmail search operators and rebuilt from the validated pieces.
Anything unknown raises GmailQueryError, so it can never reach the Gmail API.
"""

import re

from integrations.gmail.models import GmailQueryError

MAX_QUERY_CHARS = 300
MAX_TOKENS = 14

_EMAILISH = re.compile(r"^[\w.+'\-]{1,64}@[\w.\-]{1,255}$|^[\w.+'\-@]{1,100}$")
_WORD = re.compile(r"^[^\s\"'(){}<>\\|`$;&*=]{1,60}$", re.UNICODE)
_NEWER = re.compile(r"^\d{1,4}[dmy]$", re.I)
_DATE = re.compile(r"^(\d{4})[/-](\d{1,2})[/-](\d{1,2})$")
_LABEL = re.compile(r"^[A-Za-z0-9_\-]{1,40}$")

_VALUES: dict[str, set[str]] = {
    "is": {"unread", "read", "starred", "important"},
    "has": {"attachment"},
    "in": {"inbox", "sent", "starred", "important", "drafts"},
    "category": {"primary", "social", "promotions", "updates", "forums"},
}
_OPERATOR = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*):(.*)$", re.S)
_TOKEN = re.compile(r'-?(?:[A-Za-z]+:)?(?:"[^"\n]{1,100}"|[^\s"]+)')


def _bad(reason: str) -> GmailQueryError:
    return GmailQueryError(f"invalid Gmail search ({reason})")


def _phrase(value: str) -> str:
    inner = value[1:-1] if value.startswith('"') else value
    inner = " ".join(inner.split())
    if not inner or re.search(r"[\"<>\\{}()|`$;]", inner):
        raise _bad("bad text")
    return f'"{inner}"' if " " in inner else inner


def _operator(op: str, value: str) -> str:
    raw = value[1:-1] if value.startswith('"') else value
    if op in ("from", "to", "cc"):
        if not _EMAILISH.match(raw):
            raise _bad(f"bad {op} value")
        return f"{op}:{raw}"
    if op in ("subject", "filename"):
        return f"{op}:{_phrase(value)}"
    if op in _VALUES:
        if raw.lower() not in _VALUES[op]:
            raise _bad(f"unsupported {op} value")
        return f"{op}:{raw.lower()}"
    if op == "label":
        if not _LABEL.match(raw):
            raise _bad("bad label")
        return f"label:{raw}"
    if op in ("after", "before"):
        match = _DATE.match(raw)
        if not match or not (1990 <= int(match.group(1)) <= 2100 and 1 <= int(match.group(2)) <= 12
                             and 1 <= int(match.group(3)) <= 31):
            raise _bad(f"bad {op} date")
        return f"{op}:{int(match.group(1))}/{int(match.group(2))}/{int(match.group(3))}"
    if op in ("newer_than", "older_than"):
        if not _NEWER.match(raw):
            raise _bad(f"bad {op} value")
        return f"{op}:{raw.lower()}"
    raise _bad("unsupported operator")


def sanitize_query(query: str | None) -> str:
    """Rebuild `query` from validated pieces. An empty query is allowed (the most recent mail, bounded)."""
    if query is None or not query.strip():
        return ""
    if len(query) > MAX_QUERY_CHARS:
        raise _bad("too long")
    text = " ".join(query.replace("\n", " ").split())
    tokens = _TOKEN.findall(text)
    if len(tokens) > MAX_TOKENS or "".join(tokens).replace(" ", "") != text.replace(" ", ""):
        raise _bad("unsupported syntax")
    rebuilt: list[str] = []
    for token in tokens:
        negate = token.startswith("-")
        body = token[1:] if negate else token
        if body.upper() in ("OR", "AND") and not negate:
            rebuilt.append(body.upper())
            continue
        operator = _OPERATOR.match(body)
        if operator and not body.startswith('"'):
            part = _operator(operator.group(1).lower(), operator.group(2))  # any `name:value` must be a whitelisted operator
        else:
            part = _phrase(body) if body.startswith('"') else body
            if not body.startswith('"') and not _WORD.match(body):
                raise _bad("bad word")
        rebuilt.append(("-" if negate else "") + part)
    while rebuilt and rebuilt[0] in ("OR", "AND"):
        rebuilt.pop(0)
    while rebuilt and rebuilt[-1] in ("OR", "AND"):
        rebuilt.pop()
    return " ".join(rebuilt)
