"""Gmail abstraction: the rest of JARVIS depends on GmailClient, never on an HTTP or Google SDK call."""

from abc import ABC, abstractmethod

from integrations.base import Integration
from integrations.gmail.models import GmailMessage, GmailSearchResult, GmailThread


class GmailClient(ABC):
    """Read-only access to one Gmail mailbox. Implementations raise GmailError subclasses."""

    @abstractmethod
    def search(self, query: str, max_results: int, page_token: str | None = None) -> GmailSearchResult:
        """Messages matching a (pre-validated) Gmail search query, newest first, at most `max_results`."""
        raise NotImplementedError

    @abstractmethod
    def get_message(self, message_id: str) -> GmailMessage:
        raise NotImplementedError

    @abstractmethod
    def get_thread(self, thread_id: str) -> GmailThread:
        """Every message of the thread in chronological order (bounded)."""
        raise NotImplementedError


    def get_attachment(self, message_id: str, attachment_id: str, max_bytes: int) -> bytes:
        """The decoded bytes of one attachment, refusing anything larger than `max_bytes`. Optional: clients that cannot raise NotImplementedError."""
        raise NotImplementedError


class GmailIntegration(Integration):
    """Reports whether Gmail is set up, for the Integration registry."""

    name = "gmail"

    def __init__(self, is_ready):
        self._is_ready = is_ready

    def is_configured(self) -> bool:
        return bool(self._is_ready())
