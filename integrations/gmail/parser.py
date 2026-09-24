"""Gmail API JSON (format=full) -> JARVIS models. Tolerant of missing, malformed and deeply nested parts.

Nothing here downloads an attachment; only its metadata is read.
"""

import base64
import binascii
import re
from datetime import datetime, timezone
from email.utils import getaddresses, parsedate_to_datetime
from typing import Any

from integrations.gmail.models import (
    GmailAddress,
    GmailAttachment,
    GmailMessage,
    GmailResponseError,
    GmailThread,
)
from integrations.gmail.text import html_to_text, normalize_whitespace

MAX_PART_DEPTH = 20
MAX_PARTS = 300
MAX_BODY_BYTES = 2_000_000
_KEPT_HEADERS = ("message-id", "in-reply-to", "references", "list-unsubscribe", "precedence", "auto-submitted",
                 "reply-to", "x-mailer")
_CHARSET = re.compile(r"charset\s*=\s*\"?([\w.\-]+)", re.I)


def _decode_b64(data: str | None) -> bytes:
    if not data or not isinstance(data, str):
        return b""
    try:
        return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))[:MAX_BODY_BYTES]
    except (binascii.Error, ValueError):
        return b""


def _headers(part: dict[str, Any]) -> dict[str, str]:
    result: dict[str, str] = {}
    for header in part.get("headers") or []:
        if isinstance(header, dict) and isinstance(header.get("name"), str) and isinstance(header.get("value"), str):
            result.setdefault(header["name"].lower(), header["value"])
    return result


def _addresses(value: str | None) -> list[GmailAddress]:
    if not value:
        return []
    try:
        return [GmailAddress(name=" ".join(n.split()), email=e.strip()) for n, e in getaddresses([value]) if n or e]
    except Exception:  # noqa: BLE001 - odd header syntax
        return []


def _decode_text(part: dict[str, Any]) -> str:
    raw = _decode_b64((part.get("body") or {}).get("data"))
    if not raw:
        return ""
    match = _CHARSET.search(_headers(part).get("content-type", ""))
    for charset in (match.group(1) if match else "utf-8", "utf-8"):
        try:
            return raw.decode(charset, errors="replace")
        except LookupError:
            continue
    return raw.decode("utf-8", errors="replace")


class _Collected:
    def __init__(self) -> None:
        self.plain: list[str] = []
        self.html: list[str] = []
        self.attachments: list[GmailAttachment] = []
        self.parts_seen = 0


def _walk(part: dict[str, Any], message_id: str, out: _Collected, depth: int) -> None:
    if not isinstance(part, dict) or depth > MAX_PART_DEPTH or out.parts_seen >= MAX_PARTS:
        return
    out.parts_seen += 1
    mime = str(part.get("mimeType") or "").lower()
    filename = part.get("filename")
    body = part.get("body") if isinstance(part.get("body"), dict) else {}
    disposition = _headers(part).get("content-disposition", "").lower()

    if isinstance(filename, str) and filename.strip() or disposition.startswith("attachment"):
        size = body.get("size")
        out.attachments.append(GmailAttachment(
            filename=" ".join(str(filename or "attachment").split())[:255],
            mime_type=mime or "application/octet-stream",
            size=size if isinstance(size, int) and size >= 0 else 0,
            attachment_id=body.get("attachmentId") if isinstance(body.get("attachmentId"), str) else None,
            message_id=message_id,
        ))
        return  # an attachment's own content is never read
    if mime == "text/plain":
        out.plain.append(_decode_text(part))
    elif mime == "text/html":
        out.html.append(_decode_text(part))
    for child in part.get("parts") or []:
        _walk(child, message_id, out, depth + 1)


def _timestamp(headers: dict[str, str], internal_date: Any) -> datetime | None:
    if isinstance(internal_date, str) and internal_date.isdigit():
        try:
            return datetime.fromtimestamp(int(internal_date) / 1000, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            pass
    try:
        parsed = parsedate_to_datetime(headers.get("date", ""))
        return parsed.astimezone(timezone.utc) if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError, IndexError):
        return None


def parse_message(raw: dict[str, Any]) -> GmailMessage:
    """Normalize one Gmail `messages.get` response. Raises GmailResponseError only if it has no id at all."""
    if not isinstance(raw, dict) or not isinstance(raw.get("id"), str) or not raw["id"]:
        raise GmailResponseError("message without an id")
    message_id = raw["id"]
    payload = raw.get("payload") if isinstance(raw.get("payload"), dict) else {}
    headers = _headers(payload)

    collected = _Collected()
    _walk(payload, message_id, collected, 0)
    html_text = html_to_text("\n".join(collected.html)) if collected.html else ""
    plain = normalize_whitespace("\n".join(collected.plain))
    body = plain or html_text  # prefer clean plain text; fall back to converted HTML

    labels = [str(x) for x in raw.get("labelIds") or [] if isinstance(x, str)]
    return GmailMessage(
        message_id=message_id,
        thread_id=raw.get("threadId") if isinstance(raw.get("threadId"), str) else message_id,
        sender=(_addresses(headers.get("from")) or [None])[0],
        recipients=_addresses(headers.get("to")),
        cc=_addresses(headers.get("cc")),
        bcc=_addresses(headers.get("bcc")),
        subject=" ".join(headers.get("subject", "").split()),
        timestamp=_timestamp(headers, raw.get("internalDate")),
        labels=labels,
        snippet=" ".join(str(raw.get("snippet") or "").split()),
        plain_text_body=body,
        html_body=html_text or None,
        attachments=collected.attachments,
        headers={k: " ".join(v.split())[:500] for k, v in headers.items() if k in _KEPT_HEADERS},
    )


def parse_thread(raw: dict[str, Any]) -> GmailThread:
    """Normalize `threads.get`: messages in chronological order, malformed ones skipped, duplicates removed."""
    if not isinstance(raw, dict) or not isinstance(raw.get("id"), str):
        raise GmailResponseError("thread without an id")
    messages: dict[str, GmailMessage] = {}
    for item in raw.get("messages") or []:
        try:
            message = parse_message(item)
        except (GmailResponseError, ValueError):
            continue
        messages.setdefault(message.message_id, message)
    return GmailThread(thread_id=raw["id"], messages=sorted(messages.values(), key=lambda m: m.sort_key))
