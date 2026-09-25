"""Integration contract, registry and per-integration user settings.

`IntegrationAdapter` is the one interface the hub knows. It is deliberately optional in its parts: an integration implements only what it truly supports
(a read-only messaging provider has no `create`, a document folder has no OAuth), and the registry reports `supported` capabilities from what an adapter
actually overrides. Adding or removing an integration means registering or dropping one adapter; the agent, sync engine and tool router do not change.

`IntegrationRegistry` is the truth about each integration: enabled?, configured?, authenticated?, syncing?, healthy?, last sync, last error, granted
permissions. The status shown to the user (and returned to the agent) is derived here from recorded facts, never assumed.

Persisted user settings (enabled flag, granted permissions, sync state) live in one JSON file; no credentials are stored in it.
"""

import threading
from abc import ABC
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from backend.core.events import EventBus, SystemEvent
from backend.core.logging import get_logger
from backend.core.state_store import JsonFile
from integrations.hub.models import (
    NEEDS_USER_KINDS,
    OPT_IN_PERMISSIONS,
    TRANSIENT_KINDS,
    ErrorKind,
    HubError,
    IntegrationStatus,
    ItemKind,
    NormalizedItem,
    Permission,
    classify_error,
    utcnow,
)

logger = get_logger(__name__)


@dataclass
class SyncBatch:
    """What one incremental sync returned. `cursor` is opaque and only the adapter that produced it reads it back."""

    items: list[NormalizedItem] = field(default_factory=list)
    cursor: str | None = None
    removed: list[tuple[ItemKind, str]] = field(default_factory=list)  # items known to be gone at the source
    detail: str = ""


class IntegrationAdapter(ABC):
    name: str = ""
    display_name: str = ""
    permissions: frozenset[Permission] = frozenset()
    sync_interval_seconds: float = 900.0
    item_sources: tuple[str, ...] = ()  # the `source` values this adapter's items carry (default: its own name); used when its data is purged

    @property
    def sources(self) -> tuple[str, ...]:
        return self.item_sources or (self.name,)

    manual_connect: bool = False  # True: connecting needs the user (browser consent, pasted token), so JARVIS never does it unprompted

    @property
    def default_permissions(self) -> frozenset[Permission]:
        """Read permissions are granted by default; write and content-copying permissions only when the user grants them."""
        return frozenset(p for p in self.permissions if p not in OPT_IN_PERMISSIONS)

    # ---- state (must be cheap and must never touch the network) ------------------------------------------------------------
    def is_configured(self) -> bool:
        raise NotImplementedError

    def is_authenticated(self) -> bool:
        return self.is_configured()

    # ---- operations (each raises on failure; the registry/sync engine classify the exception) -------------------------------
    def authenticate(self) -> None:
        raise HubError(ErrorKind.CONFIGURATION_ERROR, f"{self.display_name or self.name} cannot be connected from here. See the setup guide.")

    def health_check(self) -> str:
        """One cheap live request proving the connection works. Returns a short detail; raises on failure."""
        raise NotImplementedError

    def disconnect(self) -> None:
        """Forget local credentials/caches. Default: nothing to forget."""

    def revoke(self) -> bool:
        """Revoke access at the provider where the provider offers it. True if revoked."""
        return False

    def search(self, query: str, limit: int) -> list[NormalizedItem]:
        raise HubError(ErrorKind.INVALID_REQUEST, f"{self.display_name or self.name} does not support search.")

    def fetch(self, source_id: str) -> NormalizedItem:
        raise HubError(ErrorKind.INVALID_REQUEST, f"{self.display_name or self.name} does not support fetching a single item.")

    def sync(self, cursor: str | None, limit: int) -> SyncBatch:
        raise HubError(ErrorKind.INVALID_REQUEST, f"{self.display_name or self.name} does not support synchronization.")

    @property
    def supported(self) -> frozenset[str]:
        base = IntegrationAdapter
        return frozenset(op for op in ("authenticate", "health_check", "disconnect", "revoke", "search", "fetch", "sync")
                         if getattr(type(self), op) is not getattr(base, op))


@dataclass(frozen=True)
class IntegrationInfo:
    name: str
    display_name: str
    status: IntegrationStatus
    enabled: bool
    configured: bool
    authenticated: bool
    detail: str
    permissions_available: tuple[str, ...]
    permissions_granted: tuple[str, ...]
    last_sync_at: datetime | None
    last_error_kind: str | None
    last_error: str | None
    supported: tuple[str, ...]
    manual_connect: bool
    items: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name, "display_name": self.display_name, "status": self.status.value, "enabled": self.enabled, "configured": self.configured,
            "authenticated": self.authenticated, "detail": self.detail, "permissions_available": list(self.permissions_available),
            "permissions_granted": list(self.permissions_granted), "last_sync_at": self.last_sync_at.isoformat() if self.last_sync_at else None,
            "last_error_kind": self.last_error_kind, "last_error": self.last_error, "supported": list(self.supported), "manual_connect": self.manual_connect,
            "items": self.items,
        }


def _parse(value: str | None) -> datetime | None:
    try:
        return datetime.fromisoformat(value) if value else None
    except ValueError:
        return None


class IntegrationRegistry:
    def __init__(self, state_file: Path | None = None, bus: EventBus | None = None, clock: Callable[[], datetime] = utcnow, counter: Callable[[str], int] | None = None):
        self._file = JsonFile(state_file, {}) if state_file else None
        self._mem: dict[str, dict] = {}
        self._bus = bus
        self._clock = clock
        self._count = counter or (lambda name: 0)
        self._adapters: dict[str, IntegrationAdapter] = {}
        self._lock = threading.RLock()
        self._syncing: set[str] = set()
        self._authenticating: set[str] = set()

    # ---- adapters ----------------------------------------------------------------------------------------------------------
    def register(self, adapter: IntegrationAdapter) -> None:
        if not adapter.name:
            raise ValueError("an integration needs a name")
        self._adapters[adapter.name] = adapter

    def unregister(self, name: str) -> None:
        self._adapters.pop(name, None)

    def adapter(self, name: str) -> IntegrationAdapter | None:
        return self._adapters.get(name.lower())

    def names(self) -> list[str]:
        return list(self._adapters)

    # ---- persisted state ---------------------------------------------------------------------------------------------------
    def _all(self) -> dict[str, dict]:
        return self._mem if self._file is None else (self._file.read() if isinstance(self._file.read(), dict) else {})

    def _entry(self, name: str) -> dict:
        return dict(self._all().get(name, {}))

    def _update(self, name: str, **changes: Any) -> None:
        with self._lock:
            data = self._all()
            entry = dict(data.get(name, {}))
            entry.update(changes)
            data[name] = entry
            if self._file is None:
                self._mem = data
            else:
                self._file.write(data)

    # ---- user controls -----------------------------------------------------------------------------------------------------
    def is_enabled(self, name: str) -> bool:
        return bool(self._entry(name).get("enabled", True))

    def set_enabled(self, name: str, enabled: bool) -> None:
        self._update(name, enabled=bool(enabled))

    def granted(self, name: str) -> frozenset[Permission]:
        adapter = self.adapter(name)
        if adapter is None:
            return frozenset()
        stored = self._entry(name).get("granted")
        if stored is None:
            return adapter.default_permissions
        valid = {p.value for p in adapter.permissions}
        return frozenset(Permission(p) for p in stored if p in valid)

    def grant(self, name: str, permission: Permission) -> bool:
        adapter = self.adapter(name)
        if adapter is None or permission not in adapter.permissions:
            return False
        self._update(name, granted=sorted({*(p.value for p in self.granted(name)), permission.value}))
        return True

    def revoke_permission(self, name: str, permission: Permission) -> None:
        self._update(name, granted=sorted(p.value for p in self.granted(name) if p is not permission))

    def allowed(self, name: str, permission: Permission | None = None) -> tuple[bool, str]:
        """(ok, reason). The single gate every read/write through the hub passes: enabled, set up, and the permission granted."""
        adapter = self.adapter(name)
        if adapter is None:
            return False, f"There is no {name} integration."
        label = adapter.display_name or adapter.name
        if not self.is_enabled(name):
            return False, f"{label} is switched off, so I'm not accessing it."
        if not adapter.is_configured():
            return False, f"{label} isn't connected yet."
        if permission is not None and permission not in self.granted(name):
            return False, f"{label} hasn't been given the {permission.value} permission."
        return True, ""

    # ---- recorded facts ----------------------------------------------------------------------------------------------------
    def set_syncing(self, name: str, value: bool) -> None:
        with self._lock:
            (self._syncing.add if value else self._syncing.discard)(name)

    def cursor(self, name: str) -> str | None:
        return self._entry(name).get("cursor")

    def next_allowed_at(self, name: str) -> datetime | None:
        return _parse(self._entry(name).get("retry_at"))

    def record_success(self, name: str, cursor: str | None, items_changed: int = 0) -> None:
        self._update(name, cursor=cursor, last_sync_at=self._clock().isoformat(), last_error_kind=None, last_error=None, failures=0, retry_at=None,
                     last_changed=items_changed)

    def record_failure(self, name: str, error: HubError, retry_at: datetime | None) -> None:
        failures = int(self._entry(name).get("failures", 0)) + 1
        self._update(name, last_error_kind=error.kind.value, last_error=error.message, failures=failures, failed_at=self._clock().isoformat(),
                     retry_at=retry_at.isoformat() if retry_at else None)

    def failures(self, name: str) -> int:
        return int(self._entry(name).get("failures", 0))

    def clear_sync_state(self, name: str) -> None:
        self._update(name, cursor=None, last_sync_at=None, last_error_kind=None, last_error=None, failures=0, retry_at=None)

    # ---- status ------------------------------------------------------------------------------------------------------------
    def info(self, name: str) -> IntegrationInfo:
        adapter = self._adapters[name.lower()]
        e = self._entry(adapter.name)
        enabled = self.is_enabled(adapter.name)
        configured = adapter.is_configured()
        authenticated = configured and adapter.is_authenticated()
        kind = ErrorKind(e["last_error_kind"]) if e.get("last_error_kind") in ErrorKind._value2member_map_ else None
        last_sync = _parse(e.get("last_sync_at"))
        if not enabled:
            status, detail = IntegrationStatus.DISABLED, "switched off by you"
        elif adapter.name in self._authenticating:
            status, detail = IntegrationStatus.AUTHENTICATING, "waiting for you to finish signing in"
        elif not configured or not authenticated or kind in NEEDS_USER_KINDS:
            status = IntegrationStatus.DISCONNECTED
            detail = (e.get("last_error") if kind in NEEDS_USER_KINDS else None) or ("authentication required" if configured else "not set up")
        elif adapter.name in self._syncing:
            status, detail = IntegrationStatus.SYNCING, "synchronizing"
        elif kind in TRANSIENT_KINDS:
            status, detail = IntegrationStatus.DEGRADED, e.get("last_error") or "temporary problem; retrying"
        elif kind is not None:
            status, detail = IntegrationStatus.ERROR, e.get("last_error") or "error"
        elif last_sync is not None:
            status, detail = IntegrationStatus.HEALTHY, "last sync succeeded"
        else:
            status, detail = IntegrationStatus.CONNECTED, "connected; not synchronized yet"
        return IntegrationInfo(
            name=adapter.name, display_name=adapter.display_name or adapter.name, status=status, enabled=enabled, configured=configured, authenticated=authenticated,
            detail=detail, permissions_available=tuple(sorted(p.value for p in adapter.permissions)), permissions_granted=tuple(sorted(p.value for p in self.granted(adapter.name))),
            last_sync_at=last_sync, last_error_kind=kind.value if kind else None, last_error=e.get("last_error"), supported=tuple(sorted(adapter.supported)),
            manual_connect=adapter.manual_connect, items=self._count(adapter.name),
        )

    def all_info(self) -> list[IntegrationInfo]:
        return [self.info(n) for n in self._adapters]

    def is_connected(self, name: str) -> tuple[bool, str]:
        """The honest answer to "Is Gmail connected?"."""
        if name.lower() not in self._adapters:
            return False, f"I don't have a {name} integration."
        info = self.info(name)
        if info.status in (IntegrationStatus.CONNECTED, IntegrationStatus.HEALTHY, IntegrationStatus.SYNCING, IntegrationStatus.DEGRADED):
            when = f" Last synchronized {_ago(info.last_sync_at, self._clock())}." if info.last_sync_at else ""
            return True, f"{info.display_name} is connected ({info.status.value}).{when}" + (f" Note: {info.detail}." if info.status is IntegrationStatus.DEGRADED else "")
        return False, f"{info.display_name} is not connected: {info.detail}."

    # ---- connect / disconnect ----------------------------------------------------------------------------------------------
    def connect(self, name: str) -> IntegrationInfo:
        """Run the adapter's authentication (blocking; may open a browser). Records the outcome truthfully."""
        adapter = self._adapters[name.lower()]
        with self._lock:
            self._authenticating.add(adapter.name)
        try:
            adapter.authenticate()
            adapter.health_check()
            self._update(adapter.name, enabled=True, last_error_kind=None, last_error=None, failures=0, retry_at=None)
            if self._bus:
                self._bus.publish(SystemEvent.INTEGRATION_CONNECTED, integration=adapter.name)
        except Exception as exc:  # noqa: BLE001 - recorded, classified, never raised into the UI
            err = classify_error(exc, adapter.display_name)
            self.record_failure(adapter.name, err, None)
            logger.warning("Connecting %s failed (%s)", adapter.name, err.kind.value)
        finally:
            with self._lock:
                self._authenticating.discard(adapter.name)
        return self.info(adapter.name)

    def connect_async(self, name: str) -> threading.Thread:
        thread = threading.Thread(target=self.connect, args=(name,), name=f"jarvis-connect-{name}", daemon=True)
        with self._lock:
            self._authenticating.add(self._adapters[name.lower()].name)
        thread.start()
        return thread

    def disconnect(self, name: str, *, revoke: bool = False, purge: Callable[[str], int] | None = None) -> IntegrationInfo:
        adapter = self._adapters[name.lower()]
        if revoke:
            try:
                adapter.revoke()
            except Exception as exc:  # noqa: BLE001 - failing to revoke remotely must not stop local disconnection
                logger.warning("Revoking %s failed (%s)", adapter.name, classify_error(exc).kind.value)
        adapter.disconnect()
        self.clear_sync_state(adapter.name)
        if purge is not None:
            purge(adapter.name)
        if self._bus:
            self._bus.publish(SystemEvent.INTEGRATION_DISCONNECTED, integration=adapter.name)
        return self.info(adapter.name)


def _ago(moment: datetime | None, now: datetime) -> str:
    if moment is None:
        return "never"
    seconds = max(0, int((now - moment).total_seconds()))
    if seconds < 90:
        return "just now"
    if seconds < 5400:
        return f"{round(seconds / 60)} minutes ago"
    if seconds < 172800:
        return f"{round(seconds / 3600)} hours ago"
    return f"{seconds // 86400} days ago"


class UnconfiguredAdapter(IntegrationAdapter):
    """Placeholder for an integration that is not enabled in the configuration, so the dashboard and the agent can still report it truthfully
    ("not set up: set JARVIS_GITHUB_ENABLED=true") instead of pretending it does not exist."""

    def __init__(self, name: str, display_name: str, permissions: frozenset[Permission], hint: str):
        self.name, self.display_name, self.permissions, self._hint = name, display_name, permissions, hint

    def is_configured(self) -> bool:
        return False

    @property
    def hint(self) -> str:
        return self._hint
