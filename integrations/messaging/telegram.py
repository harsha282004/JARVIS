"""Telegram provider, read-only, through the official Telegram Bot API.

What this can and cannot see (see docs/messaging-integration.md): a Telegram *bot* receives the messages people send
to it and the messages of groups it has been added to. It cannot read your personal Telegram chats; Telegram offers no
official API for that. Telegram keeps a bot's unconfirmed updates for 24 hours and returns at most 100 per request, so
JARVIS can only read that recent window, and it says so.

Safety properties (each covered by tests):
- Only the methods `getMe` and `getUpdates` exist here (a fixed allowlist). There is no sendMessage, no edit, no delete,
  no file download and no webhook change. `getUpdates` is called WITHOUT an offset, so nothing is acknowledged or
  consumed: reading is repeatable and changes nothing on Telegram.
- The token lives in the URL Telegram requires, so URLs are never logged or put in an error; failures log the exception
  type or status code only. A malformed token is rejected before any request.
- One request per operation, bounded retries with exponential backoff (429, 5xx, network), no polling.
"""

import logging
import re
import time
from collections.abc import Callable
from typing import Any

import httpx

from backend.core.logging import get_logger
from integrations.messaging.base import ConversationProvider, MessageProvider, MessagingProvider
from integrations.messaging.models import (
    Conversation,
    ConversationNotFound,
    Message,
    MessageNotFound,
    MessagePage,
    MessagingAuthError,
    MessagingAuthRevoked,
    MessagingError,
    MessagingNotConfigured,
    MessagingProviderConflict,
    MessagingRateLimited,
    MessagingResponseError,
    MessagingUnavailable,
    ProviderIdentity,
)
from integrations.messaging.telegram_parser import (
    PROVIDER,
    conversations_from,
    parse_updates,
    valid_conversation_id,
    valid_message_id,
)

logger = get_logger(__name__)

TELEGRAM_API_BASE = "https://api.telegram.org"
ALLOWED_METHODS = frozenset({"getMe", "getUpdates"})  # read-only, by construction
WINDOW_SIZE = 100  # Telegram's maximum for getUpdates
MAX_ATTEMPTS = 3
MAX_BACKOFF_SECONDS = 8.0
MAX_RETRY_AFTER_SECONDS = 20.0
_TOKEN = re.compile(r"^\d{5,20}:[A-Za-z0-9_-]{30,60}$")
_TOKEN_IN_URL = re.compile(r"/bot\d{5,20}:[A-Za-z0-9_-]+")


class _RedactBotToken(logging.Filter):
    """httpx logs every request URL at INFO, and Telegram's URL contains the bot token. Scrub it from those records."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            text = record.getMessage()
        except Exception:  # noqa: BLE001 - a malformed log record must not break logging
            return True
        if _TOKEN_IN_URL.search(text):
            record.msg, record.args = _TOKEN_IN_URL.sub("/bot<redacted>", text), None
        return True


def _protect_logs() -> None:
    """Idempotent: attach the redaction filter to the loggers that record request URLs."""
    for name in ("httpx", "httpcore"):
        target = logging.getLogger(name)
        if not any(isinstance(f, _RedactBotToken) for f in target.filters):
            target.addFilter(_RedactBotToken())


def valid_token(value: str) -> bool:
    return isinstance(value, str) and bool(_TOKEN.match(value.strip()))


class TelegramProvider(MessagingProvider, ConversationProvider, MessageProvider):
    name = PROVIDER
    display_name = "Telegram"

    def __init__(
        self,
        token_source: Callable[[], str],
        *,
        http: httpx.Client | None = None,
        timeout_seconds: float = 15.0,
        sleep: Callable[[float], None] = time.sleep,
    ):
        _protect_logs()
        self._token_source = token_source
        self._http = http or httpx.Client(timeout=timeout_seconds, follow_redirects=False)
        self._sleep = sleep

    def close(self) -> None:
        self._http.close()

    # ---- MessagingProvider ------------------------------------------------------------------------------------

    def _token(self) -> str:
        try:
            token = (self._token_source() or "").strip()
        except Exception:  # noqa: BLE001 - an unreadable token file is "not configured", never a crash
            token = ""
        if not valid_token(token):
            raise MessagingNotConfigured("no valid token")
        return token

    def is_configured(self) -> bool:
        try:
            self._token()
        except MessagingNotConfigured:
            return False
        return True

    def authenticate(self) -> ProviderIdentity:
        me = self._call("getMe", {})
        if not isinstance(me, dict):
            raise MessagingResponseError("getMe result was not an object")
        username = me.get("username")
        return ProviderIdentity(provider=PROVIDER, account=f"@{username}"[:101] if isinstance(username, str) and username else "")

    # ---- ConversationProvider / MessageProvider -----------------------------------------------------------------

    def _window(self) -> list[Message]:
        result = self._call("getUpdates", {"limit": WINDOW_SIZE, "timeout": 0, "allowed_updates": ["message", "channel_post"]})
        if not isinstance(result, list):
            raise MessagingResponseError("getUpdates result was not a list")
        return parse_updates(result)

    def list_conversations(self, limit: int) -> list[Conversation]:
        return conversations_from(self._window())[: max(1, limit)]

    def get_conversation(self, conversation_id: str) -> Conversation:
        if not valid_conversation_id(conversation_id):
            raise ConversationNotFound("malformed id")
        found = [c for c in conversations_from(self._window()) if c.conversation_id == conversation_id]
        if not found:
            raise ConversationNotFound("not in the readable window")
        return found[0]

    def get_messages(self, conversation_id: str | None, limit: int) -> MessagePage:
        if conversation_id is not None and not valid_conversation_id(conversation_id):
            raise ConversationNotFound("malformed id")
        window = self._window()
        wanted = [m for m in window if conversation_id is None or m.conversation_id == conversation_id]
        limit = max(1, limit)
        if conversation_id is not None and not wanted:
            raise ConversationNotFound("not in the readable window")
        # A full window means older messages may exist beyond what Telegram returned.
        return MessagePage(messages=wanted[:limit], truncated=len(wanted) > limit or len(window) >= WINDOW_SIZE, scope="recent_window")

    def get_message(self, message_id: str) -> Message:
        if not valid_message_id(message_id):
            raise MessageNotFound("malformed id")
        for message in self._window():
            if message.message_id == message_id:
                return message
        raise MessageNotFound("not in the readable window")

    # ---- transport ------------------------------------------------------------------------------------------------

    def _call(self, method: str, payload: dict[str, Any]) -> Any:
        if method not in ALLOWED_METHODS:  # defence in depth: no other Telegram method can ever be requested
            raise MessagingError("method not allowed")
        token = self._token()
        url = f"{TELEGRAM_API_BASE}/bot{token}/{method}"
        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                response = self._http.post(url, json=payload)
            except httpx.HTTPError as exc:  # never log the exception text: it can contain the URL (and so the token)
                logger.warning("Telegram network error (%s), attempt %d", type(exc).__name__, attempt)
                if attempt == MAX_ATTEMPTS:
                    raise MessagingUnavailable("network failure") from None
                self._backoff(attempt, None)
                continue
            status, body = response.status_code, self._json(response)
            if status == 200 and isinstance(body, dict) and body.get("ok") is True:
                return body.get("result")
            code = body.get("error_code") if isinstance(body, dict) else None
            code = code if isinstance(code, int) else status
            if code == 429:
                logger.warning("Telegram rate limited the request (attempt %d)", attempt)
                if attempt == MAX_ATTEMPTS:
                    raise MessagingRateLimited("rate limited")
                params = body.get("parameters") if isinstance(body, dict) else None
                self._backoff(attempt, params.get("retry_after") if isinstance(params, dict) else None)
                continue
            if code >= 500:
                logger.warning("Telegram API error %d (attempt %d)", code, attempt)
                if attempt == MAX_ATTEMPTS:
                    raise MessagingUnavailable(f"server error {code}")
                self._backoff(attempt, None)
                continue
            if code == 401:
                logger.error("Telegram rejected the bot token")
                raise MessagingAuthRevoked("token rejected")
            if code in (403, 404):
                logger.error("Telegram denied the request (%d)", code)
                raise MessagingAuthError("not allowed")
            if code == 409:
                logger.error("Telegram reports a conflicting getUpdates consumer or a webhook")
                raise MessagingProviderConflict("conflict")
            logger.error("Telegram returned an unexpected status %d", code)
            raise MessagingResponseError(f"unexpected status {code}")
        raise MessagingUnavailable("retries exhausted")  # unreachable: the loop always returns or raises

    @staticmethod
    def _json(response: httpx.Response) -> Any:
        try:
            return response.json()
        except ValueError:
            return None

    def _backoff(self, attempt: int, retry_after: Any) -> None:
        delay = min(0.5 * 2 ** (attempt - 1), MAX_BACKOFF_SECONDS)
        if isinstance(retry_after, (int, float)) and not isinstance(retry_after, bool):
            delay = max(delay, min(float(retry_after), MAX_RETRY_AFTER_SECONDS))
        self._sleep(delay)
