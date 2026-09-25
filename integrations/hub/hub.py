"""IntegrationHub: registry + normalized store + sync engine + tool router + the feed into the rest of JARVIS, as one object.

The feed (`_on_items`) turns synchronized changes into events on the bus (so the intelligence layer reacts, not polls) and, conservatively, into notifications:
only CRITICAL emails are announced; important ones are stored for the briefing; ordinary and promotional mail is never announced.
`external_items()` hands the context engine what the hub knows (GitHub, dates found in messages, registrations) from what was synchronized, only for integrations
that are currently switched on. Disabling an integration therefore stops it everywhere at once; `disconnect(purge=True)` also deletes what it stored.
"""

from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path

from agent.intelligence.models import ExternalItem
from agent.memory.models import Confidence
from backend.core.events import EventBus, SystemEvent
from backend.core.logging import get_logger
from integrations.hub.models import ItemKind, NormalizedItem, utcnow
from integrations.hub.registry import IntegrationAdapter, IntegrationRegistry
from integrations.hub.repository import HubRepository
from integrations.hub.sync import SyncEngine
from integrations.hub.tools import HubTools

logger = get_logger(__name__)

_CONFIDENCE = {"low": Confidence.LOW, "medium": Confidence.MEDIUM, "high": Confidence.HIGH}


class IntegrationHub:
    def __init__(self, session_factory, state_file: Path | None, bus: EventBus | None, zone, clock: Callable[[], datetime] = utcnow, *, center=None,
                 retention_days: int = 90):
        self.bus, self.zone, self._clock, self._center = bus, zone, clock, center
        self.repo = HubRepository(session_factory, clock)
        self.registry = IntegrationRegistry(state_file, bus, clock, counter=self._count)
        self.engine = SyncEngine(self.registry, self.repo, bus, clock, on_items=self._on_items)
        self.tools = HubTools(self.registry, self.repo, clock, zone)
        self._retention = retention_days

    def _count(self, name: str) -> int:
        adapter = self.registry.adapter(name)
        try:
            return sum(self.repo.count(s) for s in (adapter.sources if adapter else (name,)))
        except Exception:  # noqa: BLE001 - a count is decoration; the database being down must not break status
            return 0

    def register(self, adapter: IntegrationAdapter) -> None:
        self.registry.register(adapter)

    # ---- disabling / disconnecting -----------------------------------------------------------------------------------------
    def purge(self, name: str) -> int:
        adapter = self.registry.adapter(name)
        return sum(self.repo.purge(s) for s in (adapter.sources if adapter else (name,)))

    def disconnect(self, name: str, *, revoke: bool = False, purge: bool = False):
        return self.registry.disconnect(name, revoke=revoke, purge=self.purge if purge else None)

    def prune(self) -> int:
        return self.repo.prune(self._retention)

    # ---- feed --------------------------------------------------------------------------------------------------------------
    def _on_items(self, integration: str, changed: list[tuple[NormalizedItem, str]]) -> None:
        if self.bus is None:
            return
        github = documents = 0
        for item, outcome in changed:
            if item.kind is ItemKind.EMAIL and outcome == "created":
                importance = str(item.metadata.get("importance", "NORMAL"))
                self.bus.publish(SystemEvent.EMAIL_RECEIVED, message_id=item.source_id, importance=importance, topic=item.metadata.get("topic"))
                self._maybe_notify(item, importance)
            elif item.kind in (ItemKind.EVENT, ItemKind.DEADLINE) and item.source in ("gmail", "telegram") and "#" in item.source_id and not item.metadata.get("registration"):
                self.bus.publish(SystemEvent.DEADLINE_DETECTED, source=item.source, source_id=item.source_id, kind=item.kind.value)
            elif item.source == "calendar" and item.kind is ItemKind.EVENT:
                self.bus.publish(SystemEvent.CALENDAR_EVENT_CREATED if outcome == "created" else SystemEvent.CALENDAR_EVENT_UPDATED, source_id=item.source_id)
            elif item.source == "github":
                github += 1
            elif item.source == "documents" and item.metadata.get("outcome") in ("indexed", "reindexed"):
                documents += 1
                self.bus.publish(SystemEvent.DOCUMENT_INDEXED, source_id=item.source_id)
        if github:
            self.bus.publish(SystemEvent.GITHUB_ACTIVITY_RECEIVED, items=github)

    def _maybe_notify(self, item: NormalizedItem, importance: str) -> None:
        """Do not announce every email: only a CRITICAL one interrupts. An IMPORTANT one is stored for the briefing (no sound, no popup)."""
        if self._center is None or importance not in ("CRITICAL", "IMPORTANT") or item.metadata.get("injection_suspected"):
            return
        from backend.core.notifications import Level

        level = Level.CRITICAL if importance == "CRITICAL" else Level.NORMAL
        try:
            self._center.submit(dedupe_key=f"email:{item.source_id}", level=level, title=f"Email: {item.title[:80]}",
                                body=f"An email from {item.metadata.get('sender', 'a sender')} looks {importance.lower()}: {'; '.join(item.metadata.get('importance_reasons', [])[:2])}.",
                                category="email", source="gmail", refs={"message_id": item.source_id})
        except Exception as exc:  # noqa: BLE001
            logger.warning("Email notification failed (%s)", type(exc).__name__)

    # ---- what the context engine sees --------------------------------------------------------------------------------------
    def external_items(self, now: datetime) -> list[ExternalItem]:
        out: list[ExternalItem] = []

        def add(source: str, kind: ItemKind, since: datetime | None = None, limit: int = 40, extra_filter=None) -> None:
            adapter = next((a for a in (self.registry.adapter(n) for n in self.registry.names()) if a and source in a.sources), None)
            if adapter is None or not self.registry.is_enabled(adapter.name):
                return  # a source the user switched off is not read, not even from the local store
            for item in self.repo.search("", source=source, kind=kind, since=since, limit=limit):
                if extra_filter and not extra_filter(item):
                    continue
                out.append(ExternalItem(item.kind.value, item.source, item.source_id, item.title, item.timestamp, _CONFIDENCE.get(item.confidence, Confidence.MEDIUM),
                                        tuple((k, tuple(v) if isinstance(v, list) else v) for k, v in item.metadata.items())))

        recent = now - timedelta(days=14)
        add("github", ItemKind.REPOSITORY, None, 20)
        add("github", ItemKind.COMMIT, recent, 40)
        add("github", ItemKind.ISSUE, None, 40, lambda i: i.metadata.get("state", "open") == "open")
        add("github", ItemKind.PULL_REQUEST, None, 30, lambda i: i.metadata.get("state", "open") == "open")
        add("telegram", ItemKind.DEADLINE, now - timedelta(days=1), 30)
        add("telegram", ItemKind.EVENT, now - timedelta(days=1), 30)
        add("gmail", ItemKind.EVENT, None, 30, lambda i: bool(i.metadata.get("registration")))
        return out
