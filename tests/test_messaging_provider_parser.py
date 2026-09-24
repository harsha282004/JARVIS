"""Messaging abstraction, models, Telegram parser and the Telegram Bot API provider (real httpx code on a mock transport)."""

import json
import logging
from datetime import datetime, timezone

import httpx
import pytest

from integrations.messaging.base import (
    ConversationProvider,
    MessageProvider,
    MessagingIntegration,
    MessagingProvider,
    ProviderRegistry,
    SearchProvider,
)
from integrations.messaging.models import (
    Attachment,
    Capability,
    Conversation,
    ConversationKind,
    ConversationNotFound,
    Message,
    MessageNotFound,
    MessageQuery,
    MessagingAuthError,
    MessagingAuthRevoked,
    MessagingError,
    MessagingNotConfigured,
    MessagingProviderConflict,
    MessagingRateLimited,
    MessagingResponseError,
    MessagingUnavailable,
    Person,
    UnsupportedCapability,
)
from integrations.messaging.telegram import ALLOWED_METHODS, TELEGRAM_API_BASE, TelegramProvider, valid_token
from integrations.messaging.telegram_parser import (
    conversations_from,
    parse_update,
    parse_updates,
    valid_conversation_id,
    valid_message_id,
)
from tests.messaging_helpers import (
    TOKEN,
    FakeProvider,
    ReadOnlyMinimalProvider,
    SearchableProvider,
    msg,
    t_message,
    t_update,
    ts,
)

# ---- abstraction ------------------------------------------------------------------------------------------------------


def test_capabilities_are_detected_from_the_interfaces_a_provider_implements():
    assert FakeProvider().capabilities == {Capability.CONVERSATIONS, Capability.MESSAGES}
    assert SearchableProvider().capabilities == {Capability.CONVERSATIONS, Capability.MESSAGES, Capability.SEARCH}
    assert ReadOnlyMinimalProvider().capabilities == frozenset()
    assert TelegramProvider(lambda: TOKEN).capabilities == {Capability.CONVERSATIONS, Capability.MESSAGES}  # bots cannot search
    assert not isinstance(TelegramProvider(lambda: TOKEN), SearchProvider)
    assert {c.value for c in Capability} == {"conversations", "messages", "search"}  # no send/edit/delete capability exists


def test_registry_registers_finds_and_rejects_duplicates():
    registry = ProviderRegistry()
    a, b, c = FakeProvider(), SearchableProvider(), ReadOnlyMinimalProvider()
    for p in (a, b, c):
        registry.register(p)
    assert registry.names() == ["chatco", "minimal", "telegram"] and len(registry) == 3
    assert registry.get("Telegram") is a and registry.get("nope") is None
    assert registry.supporting(Capability.SEARCH) == [b]
    assert registry.supporting(Capability.MESSAGES) == [b, a]
    with pytest.raises(ValueError):
        registry.register(FakeProvider())
    assert ProviderRegistry().all() == [] and len(ProviderRegistry()) == 0  # nothing is registered by default


def test_asking_for_an_unsupported_capability_is_reported_never_faked():
    registry = ProviderRegistry()
    registry.register(ReadOnlyMinimalProvider())
    for capability in Capability:
        with pytest.raises(UnsupportedCapability) as exc:
            registry.require("minimal", capability)
        assert exc.value.capability == capability.value and "doesn't support" in exc.value.user_message
    with pytest.raises(UnsupportedCapability):
        registry.require("unknown", Capability.MESSAGES)


def test_the_providers_interfaces_have_no_send_edit_or_delete_methods():
    forbidden = ("send", "reply", "edit", "delete", "forward", "mark", "post", "write", "update", "remove")
    for cls in (MessagingProvider, ConversationProvider, MessageProvider, SearchProvider, TelegramProvider):
        names = [n for n in dir(cls) if not n.startswith("_") and callable(getattr(cls, n, None))]
        assert not [n for n in names if any(n.lower().startswith(f) for f in forbidden)], (cls.__name__, names)


def test_integration_registry_reports_whether_a_provider_is_ready():
    registry = ProviderRegistry()
    assert MessagingIntegration(registry).is_configured() is False
    registry.register(FakeProvider(configured=False))
    assert MessagingIntegration(registry).is_configured() is False
    other = SearchableProvider()
    registry.register(other)
    assert MessagingIntegration(registry).is_configured() is True


# ---- models -----------------------------------------------------------------------------------------------------------


def test_message_and_conversation_models_validate_and_normalize_times():
    m = Message(message_id="p:1:1", conversation_id="p:1", provider="p", timestamp=datetime(2030, 1, 1, 12, 0))
    assert m.timestamp.tzinfo is timezone.utc and m.is_unread is None and m.attachments == [] and m.recipients == []
    aware = Message(message_id="p:1:2", conversation_id="p:1", provider="p", timestamp=datetime(2030, 1, 1, 12, 0, tzinfo=timezone.utc))
    assert aware.timestamp == m.timestamp
    for bad in ({"message_id": ""}, {"conversation_id": ""}, {"provider": ""}, {"text": "x" * 20001}):
        with pytest.raises(ValueError):
            Message(**{"message_id": "p:1:1", "conversation_id": "p:1", "provider": "p", **bad})
    c = Conversation(conversation_id="p:1", provider="p", last_message_at=datetime(2030, 1, 1))
    assert c.last_message_at.tzinfo is timezone.utc and c.unread_count is None and c.display == "an unnamed conversation"
    with pytest.raises(ValueError):
        Conversation(conversation_id="p:1", provider="p", unread_count=-1)
    assert Conversation(conversation_id="p:1", provider="p", participants=[Person(name="Ann"), Person(username="bob")]).display == "Ann, @bob"


def test_attachment_metadata_and_person_display():
    a = Attachment(attachment_id="f1", filename="a.pdf", mime_type="application/pdf", size=10)
    assert (a.filename, a.size, a.kind) == ("a.pdf", 10, "file")
    with pytest.raises(ValueError):
        Attachment(size=-1)
    assert Person().display == "an unknown sender" and Person(username="x").display == "@x" and Person(name="Ann", username="x").display == "Ann"


def test_message_query_is_structured_and_bounded():
    q = MessageQuery(text="project", sender="John", limit=5)
    assert q.limit == 5
    for bad in (0, 101):
        with pytest.raises(ValueError):
            MessageQuery(limit=bad)
    with pytest.raises(ValueError):
        MessageQuery(text="x" * 101)


def test_error_messages_are_speakable_and_content_free():
    for cls in (MessagingNotConfigured, MessagingAuthError, MessagingAuthRevoked, MessagingRateLimited, MessagingUnavailable,
                MessagingProviderConflict, MessagingResponseError, ConversationNotFound, MessageNotFound):
        error = cls("detail with SECRET-TOKEN")
        assert error.user_message and "SECRET" not in error.user_message and issubclass(cls, MessagingError)
    assert "docs/messaging-integration.md" in MessagingNotConfigured.user_message


# ---- Telegram parser --------------------------------------------------------------------------------------------------------


def test_a_private_message_is_normalized():
    m = parse_update(t_update(chat_id=10, mid=5, text="Hi there", username="john_s", date=ts(4, 8, 0)))
    assert (m.message_id, m.conversation_id, m.provider) == ("telegram:10:5", "telegram:10", "telegram")
    assert (m.sender.person_id, m.sender.name, m.sender.username, m.sender.is_bot) == ("42", "John Smith", "john_s", False)
    assert m.text == "Hi there" and m.timestamp == datetime(2030, 3, 4, 8, 0, tzinfo=timezone.utc)
    assert (m.conversation_kind, m.conversation_title, m.is_unread, m.attachments, m.reply_to) == (ConversationKind.PRIVATE, "John Smith", None, [], None)


def test_group_channel_reply_forward_and_bot_metadata():
    g = parse_update(t_update(chat_id=-100200, mid=9, text="see below", chat_type="supergroup", title="Project group", reply_to=3, forward_origin={"type": "user"}, via_bot={"id": 1}))
    assert g.conversation_kind is ConversationKind.GROUP and g.conversation_title == "Project group" and g.conversation_id == "telegram:-100200"
    assert g.reply_to.message_id == "telegram:-100200:3" and g.reply_to.sender.name == "Priya"
    assert g.source == {"forwarded": "true", "via_bot": "true"}
    post = parse_update({"update_id": 1, "channel_post": {"message_id": 4, "date": ts(), "chat": {"id": -100300, "type": "channel", "title": "News"}, "text": "Big news",
                                                          "sender_chat": {"id": -100300, "title": "News"}}})
    assert post.conversation_kind is ConversationKind.CHANNEL and post.sender.name == "News"
    assert parse_update(t_update(is_bot=True)).sender.is_bot is True


def test_attachments_metadata_only():
    m = parse_update(t_update(text=None, caption="the report", document={"file_id": "F1", "file_name": "report.pdf", "mime_type": "application/pdf", "file_size": 2048},
                              photo=[{"file_id": "P1", "file_size": 10}, {"file_id": "P2", "file_size": 999}], voice={"file_id": "V1", "mime_type": "audio/ogg", "file_size": 5}))
    assert m.text == "the report"
    kinds = {(a.kind, a.attachment_id, a.filename, a.mime_type, a.size) for a in m.attachments}
    assert kinds == {("photo", "P2", "", "image/jpeg", 999), ("document", "F1", "report.pdf", "application/pdf", 2048), ("voice", "V1", "", "audio/ogg", 5)}


@pytest.mark.parametrize("raw", [
    None, [], "text", 7, {}, {"update_id": 1}, {"update_id": 1, "edited_message": t_message()}, {"update_id": 1, "message": "nope"},
    {"update_id": 1, "message": {"message_id": 1}}, {"update_id": 1, "message": {"chat": {"id": 1}}},
    {"update_id": 1, "message": {"message_id": True, "chat": {"id": 1}}}, {"update_id": 1, "message": {"message_id": 1, "chat": {"id": "1"}}},
])
def test_malformed_or_unsupported_updates_are_skipped_not_guessed(raw):
    assert parse_update(raw) is None


def test_missing_and_odd_fields_become_safe_values():
    m = parse_update({"update_id": 1, "message": {"message_id": 1, "chat": {"id": 5, "type": "weird"}, "date": "yesterday", "text": 12, "from": {"id": True}}})
    assert (m.text, m.timestamp, m.sender, m.conversation_kind, m.attachments) == ("", None, None, ConversationKind.UNKNOWN, [])
    huge = parse_update({"update_id": 1, "message": {"message_id": 1, "chat": {"id": 5}, "date": 10**18, "text": "a\x00b\x07c"}})
    assert huge.timestamp is None and huge.text == "abc"  # control characters are dropped


def test_updates_are_deduplicated_and_newest_first_and_a_bad_result_is_empty():
    updates = [t_update(1, chat_id=1, mid=1, date=ts(4, 8)), t_update(2, chat_id=1, mid=2, date=ts(4, 10)), t_update(3, chat_id=1, mid=1, date=ts(4, 8)), "junk"]
    assert [m.message_id for m in parse_updates(updates)] == ["telegram:1:2", "telegram:1:1"]
    assert parse_updates(None) == [] and parse_updates({"a": 1}) == []


def test_conversations_are_derived_from_a_window_without_invented_unread_counts():
    window = parse_updates([
        t_update(1, chat_id=1, mid=1, date=ts(4, 8), first="John"), t_update(2, chat_id=-9, mid=2, date=ts(4, 10), chat_type="group", first="Ann", user_id=1),
        t_update(3, chat_id=-9, mid=3, date=ts(4, 11), chat_type="group", first="Bob", user_id=2)])
    convs = conversations_from(window)
    assert [c.conversation_id for c in convs] == ["telegram:-9", "telegram:1"]
    assert convs[0].title == "Project group" and {p.name.split()[0] for p in convs[0].participants} == {"Ann", "Bob"}
    assert all(c.unread_count is None for c in convs) and convs[0].last_message_at == datetime(2030, 3, 4, 11, 0, tzinfo=timezone.utc)


def test_ids_are_validated_before_use():
    assert valid_conversation_id("telegram:-100200") and valid_conversation_id("telegram:5")
    assert valid_message_id("telegram:-100200:9") and valid_message_id("telegram:5:1")
    for bad in ("", "5", "telegram:", "telegram:5:", "telegram:x", "telegram:5:x", "telegram:5:1:2", "telegram:5/../x", "telegram:1;drop", "other:5", None, 5):
        assert not valid_conversation_id(bad) or bad == "telegram:5"
        assert not valid_message_id(bad)


# ---- Telegram provider over a mock transport ---------------------------------------------------------------------------------


class Recorder:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests: list[httpx.Request] = []

    def __call__(self, request):
        self.requests.append(request)
        response = self.responses.pop(0) if self.responses else self.responses_default
        return response

    responses_default = httpx.Response(200, json={"ok": True, "result": []})


def ok(result):
    return httpx.Response(200, json={"ok": True, "result": result})


def err(code, description="x", **extra):
    return httpx.Response(code, json={"ok": False, "error_code": code, "description": description, **extra})


def provider(*responses, token=TOKEN):
    recorder = Recorder(*responses)
    sleeps: list[float] = []
    p = TelegramProvider(lambda: token, http=httpx.Client(transport=httpx.MockTransport(recorder)), sleep=sleeps.append)
    p.recorder, p.sleeps = recorder, sleeps
    return p


WINDOW = [
    t_update(1, chat_id=10, mid=1, text="Hi", date=ts(4, 8)), t_update(2, chat_id=10, mid=2, text="Are you there?", date=ts(4, 9)),
    t_update(3, chat_id=-20, mid=3, text="Demo Thursday", chat_type="group", first="Arun", user_id=8, date=ts(4, 7)),
]


def test_valid_token_shape_and_configuration_without_any_request():
    assert valid_token(TOKEN) and not valid_token("") and not valid_token("abc") and not valid_token("123:short") and not valid_token(None)
    p = provider(token="not-a-token")
    assert p.is_configured() is False
    with pytest.raises(MessagingNotConfigured):
        p.authenticate()
    assert p.recorder.requests == []  # a malformed token never reaches the network
    assert provider().is_configured() is True
    broken = TelegramProvider(lambda: (_ for _ in ()).throw(OSError("unreadable")))
    assert broken.is_configured() is False  # an unreadable token file is "not configured", not a crash


def test_authenticate_reads_the_bot_identity_only():
    p = provider(ok({"id": 1, "is_bot": True, "username": "jarvis_bot", "first_name": "J"}))
    identity = p.authenticate()
    assert (identity.provider, identity.account) == ("telegram", "@jarvis_bot")
    [request] = p.recorder.requests
    assert request.method == "POST" and str(request.url) == f"{TELEGRAM_API_BASE}/bot{TOKEN}/getMe"
    with pytest.raises(MessagingResponseError):
        provider(ok("not an object")).authenticate()


def test_reading_asks_for_the_window_without_an_offset_so_nothing_is_consumed():
    p = provider(ok(WINDOW))
    page = p.get_messages(None, 10)
    [request] = p.recorder.requests
    body = json.loads(request.content)
    assert request.url.path.endswith("/getUpdates") and body == {"limit": 100, "timeout": 0, "allowed_updates": ["message", "channel_post"]}
    assert "offset" not in body  # never acknowledges: reading is repeatable and changes nothing on Telegram
    assert [m.message_id for m in page.messages] == ["telegram:10:2", "telegram:10:1", "telegram:-20:3"] and page.scope == "recent_window"


def test_conversations_messages_and_single_message_come_from_the_window():
    p = provider(ok(WINDOW), ok(WINDOW), ok(WINDOW), ok(WINDOW), ok(WINDOW))
    assert [c.conversation_id for c in p.list_conversations(10)] == ["telegram:10", "telegram:-20"]
    assert p.get_conversation("telegram:-20").title == "Project group"
    page = p.get_messages("telegram:10", 1)
    assert [m.message_id for m in page.messages] == ["telegram:10:2"] and page.truncated is True  # one more exists beyond the limit
    assert p.get_message("telegram:10:1").text == "Hi"
    with pytest.raises(MessageNotFound):
        p.get_message("telegram:10:99")


def test_missing_and_malformed_ids_never_reach_telegram():
    p = provider(ok(WINDOW), ok(WINDOW))
    with pytest.raises(ConversationNotFound):
        p.get_conversation("telegram:999")
    with pytest.raises(ConversationNotFound):
        p.get_messages("telegram:999", 5)
    n = len(p.recorder.requests)
    for call, exc in ((lambda: p.get_conversation("../etc"), ConversationNotFound), (lambda: p.get_messages("x;y", 5), ConversationNotFound), (lambda: p.get_message("nope"), MessageNotFound)):
        with pytest.raises(exc):
            call()
    assert len(p.recorder.requests) == n  # malformed ids are rejected locally


def test_a_full_window_is_reported_as_possibly_truncated():
    many = [t_update(i, chat_id=1, mid=i, date=ts(4, 1) + i) for i in range(1, 101)]
    assert provider(ok(many)).get_messages(None, 5).truncated is True
    assert provider(ok(WINDOW)).get_messages(None, 50).truncated is False


@pytest.mark.parametrize("response, error", [
    (err(401), MessagingAuthRevoked), (err(404), MessagingAuthError), (err(403), MessagingAuthError), (err(409), MessagingProviderConflict),
    (err(400), MessagingResponseError), (httpx.Response(200, text="<html>"), MessagingResponseError), (httpx.Response(200, json={"ok": False}), MessagingResponseError),
    (httpx.Response(200, json=["not", "a", "dict"]), MessagingResponseError),
])
def test_errors_map_to_clear_messages_without_retrying_hopeless_ones(response, error):
    p = provider(response)
    with pytest.raises(error):
        p.get_messages(None, 5)
    assert len(p.recorder.requests) == 1


def test_a_result_that_is_not_a_list_is_a_response_error():
    with pytest.raises(MessagingResponseError):
        provider(ok({"unexpected": True})).get_messages(None, 5)


def test_retries_are_bounded_with_backoff_and_honour_retry_after():
    p = provider(err(429, parameters={"retry_after": 3}), err(500), ok(WINDOW))
    assert len(p.get_messages(None, 5).messages) == 3
    assert len(p.recorder.requests) == 3 and p.sleeps[0] == 3.0 and p.sleeps[1] == 1.0
    limited = provider(*[err(429, parameters={"retry_after": 999})] * 5)
    with pytest.raises(MessagingRateLimited):
        limited.get_messages(None, 5)
    assert len(limited.recorder.requests) == 3 and max(limited.sleeps) <= 20.0  # bounded attempts and bounded waits
    down = provider(*[err(503)] * 5)
    with pytest.raises(MessagingUnavailable):
        down.get_messages(None, 5)
    assert len(down.recorder.requests) == 3


def test_network_failures_are_bounded_and_never_expose_the_token():
    def boom(request):
        raise httpx.ConnectError(f"cannot reach {request.url}")

    sleeps = []
    p = TelegramProvider(lambda: TOKEN, http=httpx.Client(transport=httpx.MockTransport(boom)), sleep=sleeps.append)
    with pytest.raises(MessagingUnavailable) as exc:
        p.get_messages(None, 5)
    assert TOKEN not in str(exc.value) and TOKEN not in repr(exc.value) and exc.value.__cause__ is None and len(sleeps) == 2


def test_the_token_and_message_text_never_appear_in_logs(caplog):
    secret_text = "my-private-message-body"
    p = provider(err(429), ok([t_update(1, text=secret_text)]), err(401))
    with caplog.at_level(logging.DEBUG):
        p.get_messages(None, 5)
        with pytest.raises(MessagingAuthRevoked):
            p.get_messages(None, 5)
    assert TOKEN not in caplog.text and TOKEN.split(":")[1] not in caplog.text and secret_text not in caplog.text


def test_only_get_me_and_get_updates_can_ever_be_requested():
    assert ALLOWED_METHODS == {"getMe", "getUpdates"}
    p = provider()
    for method in ("sendMessage", "deleteMessage", "editMessageText", "forwardMessage", "setWebhook", "getFile", "getMe/../sendMessage", ""):
        with pytest.raises(MessagingError):
            p._call(method, {})
    assert p.recorder.requests == []
    provider(ok(WINDOW)).get_messages(None, 1)
    p2 = provider(ok(WINDOW), ok({"id": 1, "username": "b"}))
    p2.get_messages(None, 1)
    p2.authenticate()
    assert {r.url.path.rsplit("/", 1)[1] for r in p2.recorder.requests} == {"getUpdates", "getMe"}
    assert all(r.url.host == "api.telegram.org" and r.url.scheme == "https" and r.method == "POST" for r in p2.recorder.requests)


def test_msg_helper_builds_valid_messages():
    m = msg("7", "hey", chat="3")
    assert m.message_id == "telegram:3:7" and m.conversation_id == "telegram:3" and m.text == "hey"
