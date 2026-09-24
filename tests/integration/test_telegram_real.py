"""REAL Telegram Bot API tests: strictly read-only, a handful of requests, against YOUR bot.

They run only when a bot token is configured (MESSAGING_TELEGRAM_BOT_TOKEN or the token file; see
docs/messaging-integration.md) and otherwise SKIP; they never fake a pass. Only `getMe` and `getUpdates` (without an offset,
so nothing is acknowledged) are ever requested. Nothing is sent, edited or deleted and nothing from your chats is printed.
Tests that need messages also skip when nobody has messaged the bot in the last 24 hours (Telegram's retention window).
"""

import pytest

from backend.core.config import get_settings
from integrations.messaging.models import MessagingProviderConflict

pytestmark = pytest.mark.integration


def _provider():
    from voice.bootstrap import build_messaging_registry

    provider = build_messaging_registry(get_settings()).get("telegram")
    if not provider.is_configured():
        pytest.skip("No Telegram bot token configured; real Telegram was NOT tested")
    return provider


def _conversations(provider):
    try:
        found = provider.list_conversations(5)
    except MessagingProviderConflict:
        pytest.skip("A webhook is set or another program reads this bot's updates; real message reads were NOT tested")
    if not found:
        pytest.skip("Nobody has messaged the bot (or added it to a group) in the last 24 hours; there is nothing to read")
    return found


def test_real_authentication_identifies_the_bot():
    identity = _provider().authenticate()
    assert identity.provider == "telegram" and identity.account.startswith("@")


def test_real_conversations_are_listed_and_bounded():
    provider = _provider()
    found = _conversations(provider)
    assert len(found) <= 5
    for conversation in found:
        assert conversation.conversation_id.startswith("telegram:") and conversation.unread_count is None


def test_real_recent_messages_and_one_conversation_are_read_without_consuming_updates():
    provider = _provider()
    first = _conversations(provider)[0]
    page = provider.get_messages(first.conversation_id, 3)
    assert 1 <= page.count <= 3 and page.scope == "recent_window"
    assert all(m.conversation_id == first.conversation_id and m.provider == "telegram" for m in page.messages)
    again = provider.get_messages(first.conversation_id, 3)  # not acknowledged: the same messages come back
    assert [m.message_id for m in again.messages] == [m.message_id for m in page.messages]
    assert provider.get_message(page.messages[0].message_id).message_id == page.messages[0].message_id
    assert provider.get_conversation(first.conversation_id).conversation_id == first.conversation_id
