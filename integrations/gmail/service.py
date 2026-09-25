"""GmailService: bounded search, message/thread retrieval and email intelligence over a GmailClient.

Gmail stays the source of truth: nothing is stored in PostgreSQL, memory or the knowledge graph, and no
mailbox is mirrored. Every retrieval is bounded.
"""

from integrations.gmail.base import GmailClient
from integrations.gmail.intelligence import (
    SummaryFocus,
    build_message_prompt,
    build_thread_prompt,
    classify,
    find_action_requests,
    run_summary,
)
from integrations.gmail.models import EmailClassification, GmailMessage, GmailQueryError, GmailSearchResult, GmailThread
from integrations.gmail.query import sanitize_query
from backend.core.llm.base import LLMProvider
from backend.core.logging import get_logger

logger = get_logger(__name__)

RESOLVE_LIMIT = 5  # candidates fetched when identifying "the email from John"
DEFAULT_MAX_RESULTS = 10
ABSOLUTE_MAX_RESULTS = 50


class GmailService:
    def __init__(self, client: GmailClient, llm: LLMProvider, *, max_results: int = DEFAULT_MAX_RESULTS, is_ready=None):
        self._client = client
        self._llm = llm
        self._max_results = max(1, min(max_results, ABSOLUTE_MAX_RESULTS))
        self._is_ready = is_ready

    @property
    def max_results(self) -> int:
        return self._max_results

    def is_configured(self) -> bool:
        return bool(self._is_ready()) if self._is_ready else True

    def clamp(self, requested: int | None) -> int:
        """The number of results to fetch: the request, never above the configured maximum."""
        return max(1, min(requested or self._max_results, self._max_results))

    def search(self, query: str, max_results: int | None = None, page_token: str | None = None) -> GmailSearchResult:
        """Validated search, newest first, at most `max_results` (never above the configured maximum).
        `truncated` says more matches exist. Pass `next_page_token` back as `page_token` to continue."""
        safe = sanitize_query(query)
        result = self._client.search(safe, self.clamp(max_results), page_token)
        logger.info("Gmail search done (results=%d, more=%s)", result.count, result.truncated)
        return result

    def search_for_sync(self, query: str, max_results: int, page_token: str | None = None) -> GmailSearchResult:
        """Search with a query built by CODE for synchronization (an epoch-second `after:` filter that the model-facing whitelist rejects). The shape is fixed
        by a strict pattern, so nothing free-form can reach the API through here; model-proposed text always goes through `search`."""
        import re

        if not re.fullmatch(r"in:inbox (?:after:\d{9,11}|newer_than:\d{1,3}d)", query):
            raise GmailQueryError("invalid sync query")
        result = self._client.search(query, self.clamp(max_results), page_token)
        logger.info("Gmail sync search done (results=%d, more=%s)", result.count, result.truncated)
        return result

    def find(self, query: str, limit: int = RESOLVE_LIMIT) -> list[GmailMessage]:
        """Candidate messages for a description. Several candidates mean the caller must ask the user."""
        return self.search(query, limit).messages

    def get_message(self, message_id: str) -> GmailMessage:
        return self._client.get_message(message_id)

    def get_attachment(self, message_id: str, attachment_id: str, max_bytes: int) -> bytes:
        return self._client.get_attachment(message_id, attachment_id, max_bytes)

    def get_thread(self, thread_id: str) -> GmailThread:
        return self._client.get_thread(thread_id)

    # ---- intelligence -------------------------------------------------------------------------------------------

    def classify(self, message: GmailMessage) -> EmailClassification:
        return classify(message)

    def action_requests(self, message: GmailMessage) -> list[str]:
        return find_action_requests(message)

    def summarize_message(self, message: GmailMessage, focus: SummaryFocus = SummaryFocus.SUMMARY) -> str:
        return run_summary(self._llm, build_message_prompt(message, focus))

    def summarize_thread(self, thread: GmailThread, focus: SummaryFocus = SummaryFocus.SUMMARY) -> str:
        return run_summary(self._llm, build_thread_prompt(thread, focus))
