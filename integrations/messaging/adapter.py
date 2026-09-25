"""MessagingAdapter: the hub's view of the messaging providers. Wraps the existing MessagingService (nothing is re-implemented).

What is legitimately supported today: the official **Telegram Bot API** (messages sent to a bot you created, and groups the bot was added to), read-only.
Not supported, and not faked: WhatsApp (Meta offers no API for reading a personal account; scraping WhatsApp Web violates its terms and is fragile),
personal Telegram chats (bots cannot read them), Signal, iMessage. The provider abstraction (`integrations/messaging/base.py`) is the seam where
a legitimate provider (for example the WhatsApp Business Cloud API for a business number you own) would be added later.

Message text is untrusted: it is sanitized, bounded, scanned, mined only with the deterministic extractor, and never treated as an instruction.
"""

import json
from collections.abc import Callable
from datetime import datetime, timedelta, timezone

from agent.intelligence.extraction import CommitmentKind, TextExtractor
from agent.intelligence.models import SourceKind
from backend.core.security.trust import sanitize_external, scan_for_injection
from integrations.hub.models import ItemKind, NormalizedItem, Permission, utcnow
from integrations.hub.registry import IntegrationAdapter, SyncBatch
from integrations.messaging.models import Message
from integrations.messaging.service import MessagingService

UNSUPPORTED = {
    "whatsapp": "WhatsApp has no official API for reading a personal account, and scraping WhatsApp Web is against its terms, so JARVIS does not support it.",
    "signal": "Signal offers no API for reading messages.",
    "imessage": "iMessage offers no supported API for reading messages.",
}


def normalize_message(message: Message, extractor: TextExtractor, retrieved_at: datetime) -> list[NormalizedItem]:
    sender = message.sender.display if message.sender else "an unknown sender"
    scan = scan_for_injection(message.text)
    text = sanitize_external(message.text, 4000)
    items = [NormalizedItem(
        ItemKind.MESSAGE, message.provider, message.message_id, message.timestamp, sanitize_external(f"{sender}: {text[:80]}", 160), text[:240],
        {"conversation": sanitize_external(message.conversation_title, 100), "conversation_id": message.conversation_id, "sender": sanitize_external(sender, 60),
         "unread": message.is_unread, "attachments": [a.filename[:100] for a in message.attachments[:10]], "injection_suspected": scan.flagged},
        "high", message.message_id, retrieved_at)]
    out = extractor.extract(text, source_type=SourceKind.DOCUMENT, source_id=message.message_id, label="a message", source_timestamp=message.timestamp)
    for n, c in enumerate(out.commitments):
        if c.when is None:
            continue
        kind = ItemKind.EVENT if c.kind is CommitmentKind.EVENT else ItemKind.DEADLINE
        items.append(NormalizedItem(kind, message.provider, f"{message.message_id}#{kind.value}{n}", c.when, c.title, c.evidence[:240],
                                    {"message_id": message.message_id, "status": "pending", "is_task": c.kind is CommitmentKind.TASK, "injection_suspected": c.flagged or scan.flagged},
                                    {1: "low", 2: "medium", 3: "high"}[int(c.confidence)], message.message_id, retrieved_at))
    return items


class MessagingAdapter(IntegrationAdapter):
    name = "messaging"
    display_name = "Messaging (Telegram)"
    permissions = frozenset({Permission.READ_MESSAGES, Permission.SEARCH_MESSAGES})
    item_sources = ("telegram",)
    sync_interval_seconds = 600.0
    manual_connect = True  # the bot token comes from you (BotFather)

    def __init__(self, service: MessagingService, zone, clock: Callable[[], datetime] = utcnow, initial_days: int = 7):
        self._svc, self._zone, self._clock, self._initial_days = service, zone, clock, initial_days

    def is_configured(self) -> bool:
        return self._svc.is_configured()

    def health_check(self) -> str:
        for provider in self._svc.registry.all():
            if provider.is_configured():
                identity = provider.authenticate()  # one read-only getMe-style request
                return f"{provider.display_name} reachable as {getattr(identity, 'name', None) or 'the bot'}"
        raise NotImplementedError

    def _normalize(self, message: Message) -> list[NormalizedItem]:
        return normalize_message(message, TextExtractor(self._zone, self._clock), self._clock())

    def search(self, query: str, limit: int) -> list[NormalizedItem]:
        page = self._svc.messages(text=query, limit=limit)
        return [self._normalize(m)[0] for m in page.messages]

    def fetch(self, source_id: str) -> NormalizedItem:
        return self._normalize(self._svc.get_message(source_id))[0]

    def sync(self, cursor: str | None, limit: int) -> SyncBatch:
        now = self._clock()
        since = datetime.fromtimestamp(json.loads(cursor)["since"], tz=timezone.utc) if cursor else now - timedelta(days=self._initial_days)
        page = self._svc.messages(since=since, limit=limit)
        items, newest = [], since.timestamp()
        for m in page.messages:
            items.extend(self._normalize(m))
            if m.timestamp:
                newest = max(newest, m.timestamp.timestamp())
        return SyncBatch(items, json.dumps({"since": max(0.0, newest - 1)}), [], "more messages remain" if page.truncated else "")
