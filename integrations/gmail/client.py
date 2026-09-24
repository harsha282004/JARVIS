"""HttpGmailClient: read-only calls to the Gmail REST API over httpx.

- Only GET requests to the fixed Gmail host and only these endpoints: users/me/messages (list, get),
  users/me/threads/{id}. The base URL, method and path shapes are constants; the model can influence
  none of them, and ids are validated before they are placed in a path.
- Bounded retries with exponential backoff (429 rate limits, 5xx, network errors), honouring
  Retry-After up to a cap; a 401 refreshes the token once. No infinite loops.
- Errors are mapped to GmailError subclasses whose text never contains email content or tokens.
"""

import re
import time
from collections.abc import Callable
from typing import Any

import httpx

from backend.core.logging import get_logger
from integrations.gmail.auth import GmailAuthenticator
from integrations.gmail.base import GmailClient
from integrations.gmail.models import (
    GmailAuthError,
    GmailError,
    GmailMessage,
    GmailNotFound,
    GmailPermissionDenied,
    GmailRateLimited,
    GmailResponseError,
    GmailSearchResult,
    GmailThread,
    GmailUnavailable,
)
from integrations.gmail.parser import parse_message, parse_thread

logger = get_logger(__name__)

GMAIL_API_BASE = "https://gmail.googleapis.com/gmail/v1/users/me"
MAX_ATTEMPTS = 4
MAX_BACKOFF_SECONDS = 8.0
MAX_RETRY_AFTER_SECONDS = 20.0
HARD_MAX_RESULTS = 50
MAX_LIST_PAGES = 3
MAX_THREAD_MESSAGES = 100
_ID = re.compile(r"^[A-Za-z0-9_\-]{1,64}$")
_RATE_REASONS = {"ratelimitexceeded", "userratelimitexceeded", "quotaexceeded", "dailylimitexceeded"}


def validate_id(value: str) -> str:
    """Gmail ids are short alphanumeric strings; anything else never reaches a URL."""
    if not isinstance(value, str) or not _ID.match(value):
        raise GmailNotFound("malformed id")
    return value


class HttpGmailClient(GmailClient):
    def __init__(
        self,
        authenticator: GmailAuthenticator,
        *,
        http: httpx.Client | None = None,
        timeout_seconds: float = 15.0,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self._auth = authenticator
        self._http = http or httpx.Client(timeout=timeout_seconds, follow_redirects=False)
        self._sleep = sleep

    def close(self) -> None:
        self._http.close()

    # ---- GmailClient ----------------------------------------------------------------------------------------

    def search(self, query: str, max_results: int, page_token: str | None = None) -> GmailSearchResult:
        limit = max(1, min(int(max_results), HARD_MAX_RESULTS))
        ids: list[str] = []
        token = page_token
        estimated = 0
        pages = 0
        while len(ids) < limit and pages < MAX_LIST_PAGES:
            params: dict[str, Any] = {"maxResults": limit - len(ids)}
            if query:
                params["q"] = query
            if token:
                params["pageToken"] = token
            data = self._get("messages", params)
            pages += 1
            estimated = int(data.get("resultSizeEstimate") or estimated or 0)
            for item in data.get("messages") or []:
                if isinstance(item, dict) and isinstance(item.get("id"), str) and len(ids) < limit:
                    ids.append(item["id"])
            token = data.get("nextPageToken") if isinstance(data.get("nextPageToken"), str) else None
            if not token:
                break
        messages: list[GmailMessage] = []
        for message_id in ids:
            try:
                messages.append(self.get_message(message_id))
            except GmailNotFound:
                continue  # deleted between list and get
        return GmailSearchResult(
            query=query, messages=messages, next_page_token=token, estimated_total=max(estimated, len(messages)),
            truncated=token is not None,
        )

    def get_message(self, message_id: str) -> GmailMessage:
        return parse_message(self._get(f"messages/{validate_id(message_id)}", {"format": "full"}))

    def get_thread(self, thread_id: str) -> GmailThread:
        thread = parse_thread(self._get(f"threads/{validate_id(thread_id)}", {"format": "full"}))
        return thread.model_copy(update={"messages": thread.messages[-MAX_THREAD_MESSAGES:]})

    # ---- transport ----------------------------------------------------------------------------------------------

    def _get(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        refreshed = False
        for attempt in range(1, MAX_ATTEMPTS + 1):
            token = self._auth.access_token()
            try:
                response = self._http.get(
                    f"{GMAIL_API_BASE}/{path}", params=params, headers={"Authorization": f"Bearer {token}"}
                )
            except httpx.HTTPError as exc:
                logger.warning("Gmail network error (%s), attempt %d", type(exc).__name__, attempt)
                if attempt == MAX_ATTEMPTS:
                    raise GmailUnavailable("network failure") from None
                self._backoff(attempt, None)
                continue

            status = response.status_code
            if status == 200:
                try:
                    data = response.json()
                except ValueError:
                    raise GmailResponseError("response was not JSON") from None
                if not isinstance(data, dict):
                    raise GmailResponseError("response was not an object")
                return data
            if status == 401:
                if refreshed:
                    logger.error("Gmail rejected a freshly refreshed token")
                    raise GmailAuthError("token rejected")
                refreshed = True
                self._auth.invalidate()
                logger.info("Gmail answered 401; refreshing the access token once")
                continue
            if status == 404:
                raise GmailNotFound("not found")
            if status == 429 or (status == 403 and self._is_rate_limit(response)):
                logger.warning("Gmail rate limited the request (attempt %d)", attempt)
                if attempt == MAX_ATTEMPTS:
                    raise GmailRateLimited("rate limited")
                self._backoff(attempt, response.headers.get("Retry-After"))
                continue
            if status == 403:
                logger.error("Gmail denied the request (403)")
                raise GmailPermissionDenied("forbidden")
            if status >= 500:
                logger.warning("Gmail API error %d (attempt %d)", status, attempt)
                if attempt == MAX_ATTEMPTS:
                    raise GmailUnavailable(f"server error {status}")
                self._backoff(attempt, None)
                continue
            logger.error("Gmail API returned unexpected status %d", status)
            raise GmailError(f"unexpected status {status}")
        raise GmailUnavailable("retries exhausted")  # unreachable: the loop always returns or raises

    @staticmethod
    def _is_rate_limit(response: httpx.Response) -> bool:
        try:
            errors = response.json().get("error", {}).get("errors", [])
            return any(str(e.get("reason", "")).lower() in _RATE_REASONS for e in errors if isinstance(e, dict))
        except (ValueError, AttributeError):
            return False

    def _backoff(self, attempt: int, retry_after: str | None) -> None:
        delay = min(0.5 * 2 ** (attempt - 1), MAX_BACKOFF_SECONDS)
        if retry_after:
            try:
                delay = max(delay, min(float(retry_after), MAX_RETRY_AFTER_SECONDS))
            except ValueError:
                pass
        self._sleep(delay)
