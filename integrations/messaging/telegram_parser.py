"""Telegram Bot API updates -> JARVIS messaging models. Raw Telegram objects never leave this module.

Malformed or unsupported updates are skipped (never guessed at), missing fields become empty values, and all text is
kept as untrusted data. Nothing here downloads a file or follows a link.
"""

import re
from datetime import datetime, timezone
from typing import Any

from integrations.messaging.models import (
    Attachment,
    Conversation,
    ConversationKind,
    Message,
    Person,
    ReplyRef,
)

PROVIDER = "telegram"
_KINDS = {"private": ConversationKind.PRIVATE, "group": ConversationKind.GROUP, "supergroup": ConversationKind.GROUP, "channel": ConversationKind.CHANNEL}
_ATTACHMENT_KINDS = ("document", "audio", "video", "voice", "video_note", "animation", "sticker")
_CONVERSATION_ID = re.compile(r"^telegram:-?\d{1,20}$")
_MESSAGE_ID = re.compile(r"^telegram:(-?\d{1,20}):(\d{1,12})$")
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def conversation_id(chat_id: int) -> str:
    return f"{PROVIDER}:{chat_id}"


def message_id(chat_id: int, telegram_message_id: int) -> str:
    return f"{PROVIDER}:{chat_id}:{telegram_message_id}"


def valid_conversation_id(value: str) -> bool:
    return isinstance(value, str) and bool(_CONVERSATION_ID.match(value))


def valid_message_id(value: str) -> bool:
    return isinstance(value, str) and bool(_MESSAGE_ID.match(value))


def _int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _text(value: Any, limit: int) -> str:
    return _CONTROL.sub("", value)[:limit] if isinstance(value, str) else ""


def _person(raw: Any) -> Person | None:
    if not isinstance(raw, dict):
        return None
    ident = _int(raw.get("id"))
    name = " ".join(p for p in (_text(raw.get("first_name"), 100), _text(raw.get("last_name"), 100)) if p) or _text(raw.get("title"), 200)
    person = Person(person_id=str(ident) if ident is not None else "", name=name, username=_text(raw.get("username"), 100), is_bot=bool(raw.get("is_bot")))
    return person if (person.person_id or person.name or person.username) else None


def _attachments(raw: dict[str, Any]) -> list[Attachment]:
    found: list[Attachment] = []
    photos = raw.get("photo")
    if isinstance(photos, list) and photos:
        best = max((p for p in photos if isinstance(p, dict)), key=lambda p: _int(p.get("file_size")) or 0, default=None)
        if best is not None:
            found.append(Attachment(attachment_id=_text(best.get("file_id"), 256), kind="photo", mime_type="image/jpeg", size=_size(best)))
    for kind in _ATTACHMENT_KINDS:
        item = raw.get(kind)
        if isinstance(item, dict):
            found.append(Attachment(
                attachment_id=_text(item.get("file_id"), 256), filename=_text(item.get("file_name"), 300),
                mime_type=_text(item.get("mime_type"), 120), size=_size(item), kind=kind,
            ))
    return found


def _size(item: dict[str, Any]) -> int | None:
    size = _int(item.get("file_size"))
    return size if size is not None and size >= 0 else None


def _title(chat: dict[str, Any]) -> str:
    title = _text(chat.get("title"), 200)
    if title:
        return title
    name = " ".join(p for p in (_text(chat.get("first_name"), 100), _text(chat.get("last_name"), 100)) if p)
    return name or (f"@{_text(chat.get('username'), 100)}" if chat.get("username") else "")


def parse_update(update: Any) -> Message | None:
    """One `getUpdates` entry -> a Message, or None when it is not a plain message/channel post or is malformed."""
    if not isinstance(update, dict):
        return None
    raw = update.get("message") if isinstance(update.get("message"), dict) else update.get("channel_post")
    if not isinstance(raw, dict):
        return None
    chat = raw.get("chat")
    chat_id = _int(chat.get("id")) if isinstance(chat, dict) else None
    telegram_id = _int(raw.get("message_id"))
    if chat_id is None or telegram_id is None:
        return None
    sent = _int(raw.get("date"))
    try:
        timestamp = datetime.fromtimestamp(sent, tz=timezone.utc) if sent is not None else None
    except (OverflowError, OSError, ValueError):
        timestamp = None
    reply = raw.get("reply_to_message")
    reply_id = _int(reply.get("message_id")) if isinstance(reply, dict) else None
    sender = _person(raw.get("from")) or _person(raw.get("sender_chat"))
    source: dict[str, str] = {}
    if raw.get("forward_origin") or raw.get("forward_from") or raw.get("forward_from_chat"):
        source["forwarded"] = "true"
    if isinstance(raw.get("via_bot"), dict):
        source["via_bot"] = "true"
    kind = _KINDS.get(chat.get("type"), ConversationKind.UNKNOWN) if isinstance(chat, dict) else ConversationKind.UNKNOWN
    return Message(
        message_id=message_id(chat_id, telegram_id), conversation_id=conversation_id(chat_id), provider=PROVIDER, sender=sender,
        timestamp=timestamp, text=_text(raw.get("text") or raw.get("caption"), 20000), attachments=_attachments(raw),
        reply_to=ReplyRef(message_id=message_id(chat_id, reply_id), sender=_person(reply.get("from"))) if reply_id is not None else None,
        conversation_title=_title(chat) if isinstance(chat, dict) else "", conversation_kind=kind, source=source,
    )


def parse_updates(result: Any) -> list[Message]:
    """All messages of a `getUpdates` result, newest first, without duplicates."""
    seen: set[str] = set()
    messages: list[Message] = []
    for item in result if isinstance(result, list) else []:
        message = parse_update(item)
        if message is not None and message.message_id not in seen:
            seen.add(message.message_id)
            messages.append(message)
    messages.sort(key=lambda m: (m.timestamp or datetime.min.replace(tzinfo=timezone.utc), m.message_id), reverse=True)
    return messages


def conversations_from(messages: list[Message]) -> list[Conversation]:
    """Conversations seen in a window of messages, most recently active first. Unread counts are unknown."""
    grouped: dict[str, list[Message]] = {}
    for message in messages:
        grouped.setdefault(message.conversation_id, []).append(message)
    conversations: list[Conversation] = []
    for cid, items in grouped.items():
        people: dict[str, Person] = {}
        for m in items:
            if m.sender is not None and len(people) < 10:
                people.setdefault(m.sender.person_id or m.sender.display, m.sender)
        latest = items[0]
        conversations.append(Conversation(
            conversation_id=cid, provider=PROVIDER, title=latest.conversation_title, kind=latest.conversation_kind,
            participants=list(people.values()), last_message_at=latest.timestamp, unread_count=None,
        ))
    conversations.sort(key=lambda c: (c.last_message_at or datetime.min.replace(tzinfo=timezone.utc), c.conversation_id), reverse=True)
    return conversations
