"""Shared helpers for messaging tests: Telegram-shaped JSON builders and in-memory provider doubles.

The Telegram HTTP provider and parser are tested for real (httpx.MockTransport). The doubles below implement the
provider INTERFACES only, to test the layers above them; they are never registered by production code.
"""

from datetime import datetime, timedelta, timezone

from integrations.messaging.base import (
    ConversationProvider,
    MessageProvider,
    MessagingProvider,
    SearchProvider,
)
from integrations.messaging.models import (
    Attachment,
    Conversation,
    ConversationKind,
    ConversationNotFound,
    Message,
    MessageNotFound,
    MessagePage,
    MessageQuery,
    Person,
    ProviderIdentity,
)
from integrations.messaging.service import matches
from tests.task_helpers import IST, ist  # noqa: F401  (re-exported)

TOKEN = "123456789:AAExampleExampleExampleExampleExample_12"  # syntactically valid, not a real bot


def ts(day=4, hour=9, minute=0, month=3) -> int:
    """A Unix time in 2030 (UTC)."""
    return int(datetime(2030, month, day, hour, minute, tzinfo=timezone.utc).timestamp())


def t_message(chat_id=1, mid=1, text="Hello", *, chat_type="private", title=None, first="John", last="Smith", username=None,
              user_id=42, is_bot=False, date=None, reply_to=None, **extra):
    """A Telegram `message` object."""
    chat = {"id": chat_id, "type": chat_type}
    if chat_type == "private":
        chat.update({"first_name": first, "last_name": last})
    else:
        chat["title"] = title or "Project group"
    raw = {
        "message_id": mid, "date": date if date is not None else ts(), "chat": chat,
        "from": {"id": user_id, "is_bot": is_bot, "first_name": first, "last_name": last, **({"username": username} if username else {})},
    }
    if text is not None:
        raw["text"] = text
    if reply_to is not None:
        raw["reply_to_message"] = {"message_id": reply_to, "from": {"id": 7, "first_name": "Priya"}}
    raw.update(extra)
    return raw


def t_update(update_id=1, **kw):
    return {"update_id": update_id, "message": t_message(**kw)}


def person(name="John Smith", username="", person_id="42", is_bot=False):
    return Person(person_id=person_id, name=name, username=username, is_bot=is_bot)


def msg(mid="1", text="Hello", *, chat="1", sender="John Smith", when=None, kind=ConversationKind.PRIVATE, title="", provider="telegram",
        attachments=(), is_bot=False, **kw) -> Message:
    """A JARVIS Message (defaults: a private chat with John, 2030-03-04 08:00 UTC)."""
    return Message(
        message_id=f"{provider}:{chat}:{mid}", conversation_id=f"{provider}:{chat}", provider=provider,
        sender=person(sender, is_bot=is_bot) if sender else None, timestamp=when or datetime(2030, 3, 4, 8, 0, tzinfo=timezone.utc),
        text=text, attachments=list(attachments), conversation_title=title or (sender or ""), conversation_kind=kind, **kw,
    )


def attachment(name="report.pdf", mime="application/pdf", size=2048, kind="document"):
    return Attachment(attachment_id=f"file-{name}", filename=name, mime_type=mime, size=size, kind=kind)


class FakeProvider(MessagingProvider, ConversationProvider, MessageProvider):
    """In-memory provider (no native search). Holds messages newest first."""

    name = "telegram"
    display_name = "Telegram"

    def __init__(self, messages=(), *, configured=True, window_limit=100):
        self.messages = sorted(messages, key=lambda m: m.timestamp, reverse=True)
        self.configured = configured
        self.window_limit = window_limit
        self.calls: list[tuple] = []

    def is_configured(self):
        return self.configured

    def authenticate(self):
        self.calls.append(("authenticate",))
        return ProviderIdentity(provider=self.name, account="@jarvis_test_bot")

    def _conversations(self):
        seen: dict[str, Conversation] = {}
        for m in self.messages:
            if m.conversation_id not in seen:
                seen[m.conversation_id] = Conversation(
                    conversation_id=m.conversation_id, provider=self.name, title=m.conversation_title, kind=m.conversation_kind,
                    participants=[m.sender] if m.sender else [], last_message_at=m.timestamp)
        return list(seen.values())

    def list_conversations(self, limit):
        self.calls.append(("list_conversations", limit))
        return self._conversations()[:limit]

    def get_conversation(self, conversation_id):
        self.calls.append(("get_conversation", conversation_id))
        for c in self._conversations():
            if c.conversation_id == conversation_id:
                return c
        raise ConversationNotFound("missing")

    def get_messages(self, conversation_id, limit):
        self.calls.append(("get_messages", conversation_id, limit))
        wanted = [m for m in self.messages if conversation_id is None or m.conversation_id == conversation_id]
        return MessagePage(messages=wanted[:limit], truncated=len(wanted) > limit, scope="recent_window")

    def get_message(self, message_id):
        self.calls.append(("get_message", message_id))
        for m in self.messages:
            if m.message_id == message_id:
                return m
        raise MessageNotFound("missing")


class SearchableProvider(FakeProvider, SearchProvider):
    """A provider WITH native search, to test the native path."""

    name = "chatco"
    display_name = "ChatCo"

    def search_messages(self, query: MessageQuery):
        self.calls.append(("search_messages", query))
        hits = [m for m in self.messages if matches(m, query)]
        return MessagePage(messages=hits[: query.limit], truncated=len(hits) > query.limit, scope="provider")


class ReadOnlyMinimalProvider(MessagingProvider):
    """Implements no capability at all: every request for one must be reported as unsupported."""

    name = "minimal"
    display_name = "Minimal"

    def is_configured(self):
        return True

    def authenticate(self):
        return ProviderIdentity(provider=self.name)


def sample_messages():
    """A small inbox: John (private), Priya (private), and a 'Project group' with two people."""
    day = datetime(2030, 3, 4, 8, 0, tzinfo=timezone.utc)
    return [
        msg("1", "Hi, could you please submit the internship report by Friday?", chat="10", sender="John Smith", when=day),
        msg("2", "Lunch tomorrow?", chat="11", sender="Priya Rao", when=day - timedelta(hours=2)),
        msg("3", "The project demo is on Thursday.", chat="20", sender="Arun Kumar", kind=ConversationKind.GROUP, title="Project group",
            when=day - timedelta(hours=3)),
        msg("4", "I uploaded the slides for the demo.", chat="20", sender="Priya Rao", kind=ConversationKind.GROUP, title="Project group",
            when=day - timedelta(hours=1), attachments=[attachment("slides.pdf")]),
        msg("5", "Thanks for the update on the internship.", chat="10", sender="John Smith", when=day - timedelta(days=1, hours=1)),
    ]
