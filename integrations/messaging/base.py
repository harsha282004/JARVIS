"""Messaging abstraction: capability-based provider interfaces and the provider registry.

The rest of JARVIS depends on these interfaces, never on a platform's HTTP API. A provider implements only the
capabilities it genuinely has: the capabilities are detected from the interfaces it implements, so a provider
cannot claim one it does not provide, and asking it for one raises UnsupportedCapability.

There is NO send / edit / delete / mark-read interface. Those are future work and must be added, registered and
permission-controlled explicitly; nothing here can be used to change a message.
"""

from abc import ABC, abstractmethod

from integrations.base import Integration
from integrations.messaging.models import (
    Capability,
    Conversation,
    Message,
    MessagePage,
    MessageQuery,
    ProviderIdentity,
    UnsupportedCapability,
)


class MessagingProvider(ABC):
    """One messaging platform, read-only. Implementations raise MessagingError subclasses."""

    name: str  # short, stable identity shown to the user ("telegram")
    display_name: str

    @abstractmethod
    def is_configured(self) -> bool:
        """Are credentials present? Never contacts the provider."""
        raise NotImplementedError

    @abstractmethod
    def authenticate(self) -> ProviderIdentity:
        """Verify the credentials with the provider (one read-only request)."""
        raise NotImplementedError

    @property
    def capabilities(self) -> frozenset[Capability]:
        found = set()
        if isinstance(self, ConversationProvider):
            found.add(Capability.CONVERSATIONS)
        if isinstance(self, MessageProvider):
            found.add(Capability.MESSAGES)
        if isinstance(self, SearchProvider):
            found.add(Capability.SEARCH)
        return frozenset(found)

    def supports(self, capability: Capability) -> bool:
        return capability in self.capabilities


class ConversationProvider(ABC):
    @abstractmethod
    def list_conversations(self, limit: int) -> list[Conversation]:
        """Conversations the provider lets JARVIS see, most recently active first, at most `limit`."""
        raise NotImplementedError

    @abstractmethod
    def get_conversation(self, conversation_id: str) -> Conversation:
        raise NotImplementedError


class MessageProvider(ABC):
    @abstractmethod
    def get_messages(self, conversation_id: str | None, limit: int) -> MessagePage:
        """The newest messages of one conversation (or of all), newest first, at most `limit`."""
        raise NotImplementedError

    @abstractmethod
    def get_message(self, message_id: str) -> Message:
        raise NotImplementedError


class SearchProvider(ABC):
    @abstractmethod
    def search_messages(self, query: MessageQuery) -> MessagePage:
        """Provider-native search from validated, structured parameters."""
        raise NotImplementedError


class ProviderRegistry:
    """The providers that are registered. Nothing is registered unless real code registers a real provider."""

    def __init__(self) -> None:
        self._providers: dict[str, MessagingProvider] = {}

    def register(self, provider: MessagingProvider) -> None:
        name = provider.name.strip().lower()
        if not name or name in self._providers:
            raise ValueError("a provider with that name is already registered")
        self._providers[name] = provider

    def get(self, name: str) -> MessagingProvider | None:
        return self._providers.get(name.strip().lower())

    def names(self) -> list[str]:
        return sorted(self._providers)

    def all(self) -> list[MessagingProvider]:
        return [self._providers[n] for n in self.names()]

    def supporting(self, capability: Capability) -> list[MessagingProvider]:
        return [p for p in self.all() if p.supports(capability)]

    def require(self, name: str, capability: Capability) -> MessagingProvider:
        provider = self.get(name)
        if provider is None or not provider.supports(capability):
            raise UnsupportedCapability(name, capability.value)
        return provider

    def __len__(self) -> int:
        return len(self._providers)


class MessagingIntegration(Integration):
    """Reports whether at least one provider is set up, for the Integration registry."""

    name = "messaging"

    def __init__(self, registry: ProviderRegistry):
        self._registry = registry

    def is_configured(self) -> bool:
        return any(p.is_configured() for p in self._registry.all())
