"""Shared helpers for Gmail tests: Gmail-API-shaped JSON builders and an in-memory GmailClient double.

The HTTP client and OAuth code are tested for real (with httpx.MockTransport and google-auth's real
Credentials); FakeGmailClient is only used to test the layers ABOVE the GmailClient interface.
"""

import base64
import json
from datetime import datetime, timezone

from integrations.gmail.base import GmailClient
from integrations.gmail.models import GmailMessage, GmailNotFound, GmailSearchResult, GmailThread
from integrations.gmail.parser import parse_message, parse_thread


def b64(text: str | bytes) -> str:
    raw = text.encode("utf-8") if isinstance(text, str) else text
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def part(mime, text=None, *, filename="", attachment_id=None, size=None, parts=None, headers=None, raw=None):
    body: dict = {}
    if text is not None or raw is not None:
        data = raw if raw is not None else b64(text)
        body = {"data": data, "size": len(text or "")}
    if attachment_id:
        body = {"attachmentId": attachment_id, "size": size or 0}
    result = {"mimeType": mime, "filename": filename, "headers": headers or [], "body": body}
    if parts is not None:
        result["parts"] = parts
    return result


def raw_message(
    id="m1", thread="t1", subject="Hello", sender="John Smith <john@example.com>", to="me@example.com",
    date_ms=1893456000000, labels=("INBOX", "UNREAD"), body="Hi there", html=None, attachments=(),
    extra_headers=(), snippet=None, cc=None,
):
    headers = [
        {"name": "From", "value": sender}, {"name": "To", "value": to}, {"name": "Subject", "value": subject},
        {"name": "Date", "value": "Mon, 1 Jan 2030 09:00:00 +0000"}, *extra_headers,
    ]
    if cc:
        headers.append({"name": "Cc", "value": cc})
    children = []
    if body is not None:
        children.append(part("text/plain", body))
    if html is not None:
        children.append(part("text/html", html))
    payload_body = part("multipart/alternative", parts=children) if len(children) > 1 else (children[0] if children else part("text/plain", ""))
    if attachments:
        payload_body = part("multipart/mixed", parts=[
            payload_body, *[part(m, filename=f, attachment_id=f"att-{i}", size=s) for i, (f, m, s) in enumerate(attachments)]])
    payload_body["headers"] = headers
    return {
        "id": id, "threadId": thread, "labelIds": list(labels), "snippet": snippet if snippet is not None else (body or "")[:60],
        "internalDate": str(date_ms), "payload": payload_body,
    }


def message(**kw) -> GmailMessage:
    return parse_message(raw_message(**kw))


class FakeGmailClient(GmailClient):
    """In-memory mailbox implementing the GmailClient interface (newest first)."""

    def __init__(self, raws=()):
        self.raws = list(raws)
        self.calls: list[tuple] = []
        self.attachments: dict[tuple[str, str], bytes] = {}

    def get_attachment(self, message_id, attachment_id, max_bytes):
        self.calls.append(("get_attachment", message_id, attachment_id))
        data = self.attachments[(message_id, attachment_id)]
        if len(data) > max_bytes:
            raise GmailNotFound("too large")
        return data

    def _matches(self, raw, query: str) -> bool:
        m = parse_message(raw)
        for token in query.split():
            if token == "is:unread":
                ok = m.is_unread
            elif token.startswith("from:"):
                ok = token[5:].lower() in ((m.sender.email + " " + m.sender.name).lower() if m.sender else "")
            elif token == "has:attachment":
                ok = m.has_attachments
            elif token.startswith("after:") and token[6:].isdigit():
                ok = m.timestamp is not None and m.timestamp.timestamp() >= int(token[6:])
            elif ":" in token:
                ok = True
            else:
                ok = token.lower().strip('"') in f"{m.subject} {m.plain_text_body}".lower()
            if not ok:
                return False
        return True

    def search(self, query, max_results, page_token=None):
        self.calls.append(("search", query, max_results, page_token))
        hits = [parse_message(r) for r in self.raws if self._matches(r, query)]
        hits.sort(key=lambda m: m.sort_key, reverse=True)
        offset = int(page_token) if page_token else 0
        shown = hits[offset: offset + max_results]
        more = offset + len(shown) < len(hits)
        return GmailSearchResult(query=query, messages=shown, estimated_total=len(hits), truncated=more, next_page_token=str(offset + len(shown)) if more else None)

    def get_message(self, message_id):
        self.calls.append(("get_message", message_id))
        for raw in self.raws:
            if raw["id"] == message_id:
                return parse_message(raw)
        raise GmailNotFound("missing")

    def get_thread(self, thread_id):
        self.calls.append(("get_thread", thread_id))
        mine = [r for r in self.raws if r["threadId"] == thread_id]
        if not mine:
            raise GmailNotFound("missing")
        return parse_thread({"id": thread_id, "messages": mine})


class ScriptedLLM:
    """Plain-text LLM double: returns the queued replies and records every call."""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls = []

    def chat(self, messages, json_mode=False):
        self.calls.append((list(messages), json_mode))
        reply = self.replies.pop(0) if self.replies else "A short summary."
        if isinstance(reply, Exception):
            raise reply
        return reply if isinstance(reply, str) else json.dumps(reply)


NOW = datetime(2030, 1, 2, 9, 0, tzinfo=timezone.utc)
