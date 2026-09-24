"""MessagingService: bounded retrieval, search, conversation lookup and message intelligence over the registered providers.

The providers stay the source of truth: nothing is stored in PostgreSQL, memory or the knowledge graph, no history is
mirrored, and nothing is polled. Every operation asks each capable provider once and is bounded.

Search: a provider with native search (SearchProvider) is asked to search. Otherwise JARVIS filters the recent window
the provider lets it read (MessageProvider) and reports `scope="recent_window"`, so it never implies it searched a whole
history. A provider that cannot even read messages is skipped for that operation.
"""

import re
from datetime import datetime

from backend.core.llm.base import LLMProvider
from backend.core.logging import get_logger
from integrations.messaging.base import (
    ConversationProvider,
    MessageProvider,
    MessagingProvider,
    ProviderRegistry,
    SearchProvider,
)
from integrations.messaging.intelligence import (
    SummaryFocus,
    build_prompt,
    classify,
    find_action_requests,
    run_summary,
)
from integrations.messaging.models import (
    ActionCandidate,
    Capability,
    ClassificationResult,
    Conversation,
    ConversationNotFound,
    Message,
    MessageNotFound,
    MessagePage,
    MessageQuery,
    MessagingError,
    MessagingNotConfigured,
    UnsupportedCapability,
)

logger = get_logger(__name__)

DEFAULT_MAX_RESULTS = 20
ABSOLUTE_MAX_RESULTS = 100
RESOLVE_LIMIT = 5  # candidates fetched when identifying "the message from John"
_WORD = re.compile(r"[a-z0-9@_]+")
_FILLER = frozenset({"the", "a", "an", "my", "our", "of", "in", "on", "to", "from", "about", "with", "message", "messages", "chat", "group", "conversation", "and", "for"})


def _words(text: str) -> list[str]:
    return [w for w in _WORD.findall(text.lower()) if w not in _FILLER]


def _has_words(haystack: str, wanted: list[str]) -> bool:
    have = _WORD.findall(haystack.lower())
    return all(any(h == w or (len(w) >= 4 and h.startswith(w)) for h in have) for w in wanted)


def matches(message: Message, query: MessageQuery) -> bool:
    """Local filter used when a provider has no native search. Whole words (or 4+ letter prefixes), all must match."""
    if query.conversation_id is not None and message.conversation_id != query.conversation_id:
        return False
    if query.since is not None and (message.timestamp is None or message.timestamp < query.since):
        return False
    if query.until is not None and (message.timestamp is None or message.timestamp >= query.until):
        return False
    if query.sender:
        who = _words(query.sender)
        sender = message.sender
        if not who or sender is None or not _has_words(f"{sender.name} {sender.username}", who):
            return False
    if query.text:
        wanted = _words(query.text)
        if wanted and not _has_words(f"{message.text} {' '.join(a.filename for a in message.attachments)}", wanted):
            return False
    return True


def conversation_matches(conversation: Conversation, name: str) -> bool:
    wanted = _words(name)
    if not wanted:
        return False
    return _has_words(conversation.title, wanted) or _has_words(" ".join(p.name + " " + p.username for p in conversation.participants), wanted)


class MessagingService:
    def __init__(self, registry: ProviderRegistry, llm: LLMProvider, *, max_results: int = DEFAULT_MAX_RESULTS):
        self._registry = registry
        self._llm = llm
        self._max_results = max(1, min(max_results, ABSOLUTE_MAX_RESULTS))

    @property
    def max_results(self) -> int:
        return self._max_results

    @property
    def registry(self) -> ProviderRegistry:
        return self._registry

    def is_configured(self) -> bool:
        return any(p.is_configured() for p in self._registry.all())

    def clamp(self, requested: int | None) -> int:
        return max(1, min(requested or self._max_results, self._max_results))

    def _ready(self, capability: Capability) -> list[MessagingProvider]:
        """Configured providers that support the capability. Nothing configured -> the setup message; configured but
        none capable -> UnsupportedCapability (never a pretend answer)."""
        configured = [p for p in self._registry.all() if p.is_configured()]
        if not configured:
            raise MessagingNotConfigured("no provider is set up")
        capable = [p for p in configured if p.supports(capability)]
        if not capable:
            raise UnsupportedCapability(configured[0].name, capability.value)
        return capable

    # ---- conversations ---------------------------------------------------------------------------------------------

    def conversations(self, name: str | None = None, limit: int | None = None) -> list[Conversation]:
        """Most recently active first. With `name`, only conversations whose title or participants match."""
        size = self.clamp(limit)
        found: list[Conversation] = []
        for provider in self._ready(Capability.CONVERSATIONS):
            assert isinstance(provider, ConversationProvider)
            found.extend(provider.list_conversations(ABSOLUTE_MAX_RESULTS if name else size))
        if name:
            found = [c for c in found if conversation_matches(c, name)]
        found.sort(key=lambda c: (c.last_message_at is not None, c.last_message_at, c.conversation_id), reverse=True)
        logger.info("Conversations read (count=%d)", len(found[:size]))
        return found[:size]

    def get_conversation(self, conversation_id: str) -> Conversation:
        provider = self._provider_of(conversation_id, Capability.CONVERSATIONS)
        assert isinstance(provider, ConversationProvider)
        return provider.get_conversation(conversation_id)

    def _provider_of(self, item_id: str, capability: Capability) -> MessagingProvider:
        name = item_id.split(":", 1)[0]
        provider = self._registry.get(name)
        if provider is None:
            raise ConversationNotFound("unknown provider")
        if not provider.is_configured():
            raise MessagingNotConfigured("provider is not set up")
        if not provider.supports(capability):
            raise UnsupportedCapability(provider.name, capability.value)
        return provider

    # ---- messages ----------------------------------------------------------------------------------------------------

    def messages(
        self, *, conversation_id: str | None = None, text: str = "", sender: str = "", since: datetime | None = None,
        until: datetime | None = None, limit: int | None = None,
    ) -> MessagePage:
        """Newest first, at most `limit` (never above the configured maximum)."""
        size = self.clamp(limit)
        query = MessageQuery(text=text, sender=sender, conversation_id=conversation_id, since=since, until=until, limit=size)
        wanted = [p for p in self._registry.all() if p.is_configured()]
        if conversation_id is not None:
            wanted = [self._provider_of(conversation_id, Capability.MESSAGES)]
        merged: list[Message] = []
        truncated = False
        scope = "provider"
        used = 0
        if not wanted:
            raise MessagingNotConfigured("no provider is set up")
        for provider in wanted:
            page = self._one_provider(provider, query, size)
            if page is None:
                continue
            used += 1
            merged.extend(page.messages)
            truncated = truncated or page.truncated
            if page.scope != "provider":
                scope = page.scope
        if not used:
            raise UnsupportedCapability(wanted[0].name, Capability.MESSAGES.value)
        merged.sort(key=lambda m: (m.timestamp is not None, m.timestamp, m.message_id), reverse=True)
        logger.info("Messages read (count=%d, scope=%s)", len(merged[:size]), scope)
        return MessagePage(messages=merged[:size], truncated=truncated or len(merged) > size, scope=scope)

    def _one_provider(self, provider: MessagingProvider, query: MessageQuery, size: int) -> MessagePage | None:
        native = bool(query.text) and isinstance(provider, SearchProvider)
        if native:
            return provider.search_messages(query)  # type: ignore[union-attr]
        if not isinstance(provider, MessageProvider):
            return None
        page = provider.get_messages(query.conversation_id, ABSOLUTE_MAX_RESULTS)
        filtered = [m for m in page.messages if matches(m, query)]
        return MessagePage(messages=filtered[:size], truncated=page.truncated or len(filtered) > size, scope=page.scope)

    def find(self, **criteria: object) -> list[Message]:
        """Candidate messages for a description. Several candidates mean the caller must ask the user."""
        return self.messages(limit=RESOLVE_LIMIT, **criteria).messages  # type: ignore[arg-type]

    def get_message(self, message_id: str) -> Message:
        provider = self._registry.get(message_id.split(":", 1)[0])
        if provider is None:
            raise MessageNotFound("unknown provider")
        if not provider.is_configured():
            raise MessagingNotConfigured("provider is not set up")
        if not isinstance(provider, MessageProvider):
            raise UnsupportedCapability(provider.name, Capability.MESSAGES.value)
        return provider.get_message(message_id)

    # ---- intelligence --------------------------------------------------------------------------------------------------

    def classify(self, message: Message) -> ClassificationResult:
        return classify(message)

    def action_requests(self, message: Message) -> list[ActionCandidate]:
        return find_action_requests(message)

    def summarize(self, messages: list[Message], focus: SummaryFocus = SummaryFocus.SUMMARY) -> str:
        return run_summary(self._llm, build_prompt(messages, focus))


__all__ = ["MessagingService", "matches", "conversation_matches", "MessagingError"]
