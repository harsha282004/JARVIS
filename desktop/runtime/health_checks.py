"""The real health checks JARVIS registers with the HealthMonitor.

Each check reports what is actually true right now:
  * a component that is not configured or is switched off is DISABLED (never "healthy");
  * voice checks derive from the RuntimeManager's real state and the microphone stream, not from a flag;
  * integration checks make one small live call, cached for `ttl` seconds so monitoring never hammers Gmail or Calendar,
    and map the integration's own error types (not configured / sign-in needed / unreachable) to a state.
"""

import time
from collections.abc import Callable
from pathlib import Path

from backend.core.config import Settings
from backend.core.health import Health, HealthMonitor, ServiceState
from backend.core.privacy import PrivacyController, PrivacyMode
from desktop.runtime.manager import RuntimeManager
from desktop.runtime.state import RuntimeState


class _Cached:
    """Runs `check` at most once per `ttl` seconds; in between it returns the last result. After a failure the re-check
    interval starts at `retry_first` seconds and doubles (up to `ttl`), so a recovered service is noticed quickly without
    hammering one that stays down."""

    def __init__(self, check: Callable[[], Health], ttl: float, clock: Callable[[], float] = time.monotonic, retry_first: float = 15.0):
        self._check, self._ttl, self._clock, self._retry_first = check, ttl, clock, retry_first
        self._at = -1e18
        self._last: Health | None = None
        self._failures = 0

    def __call__(self) -> Health:
        now = self._clock()
        wait = self._ttl if self._failures == 0 else min(self._ttl, self._retry_first * (2 ** (self._failures - 1)))
        if self._last is None or now - self._at >= wait:
            self._last, self._at = self._check(), now
            bad = self._last.state in (ServiceState.DISCONNECTED, ServiceState.FAILED, ServiceState.DEGRADED)
            self._failures = self._failures + 1 if bad else 0
        return self._last


def database_check(check: Callable[[], tuple[bool, str]]) -> Callable[[], Health]:
    def run() -> Health:
        ok, detail = check()
        if ok:
            return Health(ServiceState.HEALTHY, detail)
        return Health(ServiceState.DISCONNECTED if "unreachable" in detail else ServiceState.DEGRADED, detail)

    return run


def runtime_check(manager: RuntimeManager) -> Callable[[], Health]:
    def run() -> Health:
        status = manager.status()
        return {
            RuntimeState.RUNNING: Health(ServiceState.HEALTHY, "voice runtime running"),
            RuntimeState.STARTING: Health(ServiceState.STARTING, "voice runtime starting"),
            RuntimeState.PAUSED: Health(ServiceState.DISABLED, "paused by the user"),
            RuntimeState.STOPPING: Health(ServiceState.STARTING, "stopping"),
            RuntimeState.STOPPED: Health(ServiceState.DISCONNECTED, "voice runtime stopped"),
            RuntimeState.ERROR: Health(ServiceState.FAILED, (status.last_error or "voice runtime error")[:120]),
        }[status.state]

    return run


def microphone_check(manager: RuntimeManager, privacy: PrivacyController | None) -> Callable[[], Health]:
    def run() -> Health:
        if privacy is not None and privacy.mode is PrivacyMode.PRIVATE:
            return Health(ServiceState.DISABLED, "private mode: microphone off")
        status = manager.status()
        if status.state is RuntimeState.PAUSED:
            return Health(ServiceState.DISABLED, "paused: microphone released")
        if status.state is not RuntimeState.RUNNING:
            return Health(ServiceState.DISCONNECTED, "voice runtime is not running")
        if status.microphone_active or status.voice_state in ("speaking", "thinking", "transcribing"):
            return Health(ServiceState.HEALTHY, "microphone in use by JARVIS")
        return Health(ServiceState.DEGRADED, "microphone stream is not open")

    return run


def model_file_check(path: str, label: str, enabled: bool = True) -> Callable[[], Health]:
    def run() -> Health:
        if not enabled:
            return Health(ServiceState.DISABLED, f"{label} disabled")
        if not path:
            return Health(ServiceState.FAILED, f"{label} model path is not set")
        return Health(ServiceState.HEALTHY, f"{label} model present") if Path(path).is_file() else Health(ServiceState.FAILED, f"{label} model file not found")

    return run


def llm_check(base_url: str, probe: Callable[[str], bool]) -> Callable[[], Health]:
    def run() -> Health:
        return Health(ServiceState.HEALTHY, "LLM server reachable") if probe(base_url) else Health(ServiceState.DISCONNECTED, "LLM server not reachable")

    return run


def ollama_probe(base_url: str, timeout: float = 1.0) -> bool:
    import urllib.request

    url = base_url.rstrip("/").replace("//localhost", "//127.0.0.1")  # "localhost" tries IPv6 first and doubles the wait when only IPv4 listens
    try:
        with urllib.request.urlopen(url + "/api/tags", timeout=timeout) as response:  # noqa: S310 - local, configured URL
            return response.status == 200
    except Exception:  # noqa: BLE001
        return False


def integration_check(name: str, service, live_call: Callable[[], object], settings: Settings, ttl: float = 300.0) -> Callable[[], Health]:
    """`service` None (not enabled) -> DISABLED. Offline mode -> DISABLED. Otherwise one small live call, cached."""
    from integrations.calendar.models import CalendarAuthError, CalendarNotConfigured, CalendarUnavailable
    from integrations.gmail.models import GmailAuthError, GmailNotConfigured, GmailUnavailable
    from integrations.messaging.models import MessagingNotConfigured

    def call() -> Health:
        try:
            live_call()
            return Health(ServiceState.HEALTHY, f"{name} reachable")
        except (GmailNotConfigured, CalendarNotConfigured, MessagingNotConfigured):
            return Health(ServiceState.DISABLED, f"{name} is not set up")
        except (GmailAuthError, CalendarAuthError):
            return Health(ServiceState.FAILED, f"{name} needs you to sign in again")
        except (GmailUnavailable, CalendarUnavailable):
            return Health(ServiceState.DISCONNECTED, f"{name} cannot be reached")
        except Exception as exc:  # noqa: BLE001
            return Health(ServiceState.DEGRADED, f"{name} check failed ({type(exc).__name__})")

    cached = _Cached(call, ttl)

    def run() -> Health:
        if service is None:
            return Health(ServiceState.DISABLED, f"{name} is not enabled")
        if settings.JARVIS_OFFLINE_MODE:
            return Health(ServiceState.DISABLED, "offline mode")
        if hasattr(service, "is_configured") and not service.is_configured():
            return Health(ServiceState.DISABLED, f"{name} is not set up")
        return cached()

    return run


def component_check(name: str, present: bool, detail: str = "") -> Callable[[], Health]:
    """A local component that exists or not (scheduler, memory, dashboard...)."""
    def run() -> Health:
        return Health(ServiceState.HEALTHY, detail) if present else Health(ServiceState.DISABLED, f"{name} is not enabled")

    return run


def thread_check(name: str, is_alive: Callable[[], bool | None]) -> Callable[[], Health]:
    def run() -> Health:
        alive = is_alive()
        if alive is None:
            return Health(ServiceState.DISABLED, f"{name} is not enabled")
        return Health(ServiceState.HEALTHY, f"{name} running") if alive else Health(ServiceState.FAILED, f"{name} thread is not running")

    return run


def build_health_monitor(monitor: HealthMonitor, *, settings: Settings, manager: RuntimeManager, privacy: PrivacyController | None,
                         db_status: Callable[[], tuple[bool, str]], scheduler_alive: Callable[[], bool | None],
                         gmail=None, calendar=None, messaging=None, memory_enabled: bool = False, hub=None) -> HealthMonitor:
    """Registers every service the user cares about. The checks stay honest: a service that is off says so."""
    monitor.register("voice_runtime", runtime_check(manager), critical=True)
    monitor.register("stt", runtime_check(manager))  # the speech model is loaded when the voice runtime starts
    monitor.register("microphone", microphone_check(manager, privacy))
    monitor.register("wake_word", model_file_check(settings.WAKE_WORD_MODEL_PATH, "wake word", settings.WAKE_WORD_ENABLED))
    monitor.register("tts", model_file_check(settings.TTS_MODEL_PATH, "text-to-speech"))
    monitor.register("database", _Cached(database_check(db_status), 15.0), critical=True)  # the schema check reads migration files: not on every call
    monitor.register("llm", _Cached(llm_check(settings.OLLAMA_BASE_URL, ollama_probe), 30.0) if settings.LLM_PROVIDER == "ollama"
                     else component_check("llm", False))
    monitor.register("scheduler", thread_check("scheduler", scheduler_alive))
    monitor.register("memory", component_check("memory", memory_enabled, "personal memory enabled"))
    monitor.register("gmail", integration_check("Gmail", gmail, lambda: gmail.search("in:inbox", 1), settings))
    monitor.register("calendar", integration_check("Calendar", calendar, lambda: calendar.calendars(), settings))
    monitor.register("messaging", integration_check("Messaging", messaging, lambda: messaging.is_configured(), settings))
    if hub is not None:  # integrations with no live probe of their own report through the hub's recorded state
        monitor.register("github", registry_check(hub.registry, "github"))
        monitor.register("documents", registry_check(hub.registry, "documents"))
    return monitor


def registry_check(registry, name: str) -> Callable[[], Health]:
    """Health of an integration from the hub's recorded facts (last sync, last error): no network call."""
    from integrations.hub.models import IntegrationStatus as S

    def run() -> Health:
        info = registry.info(name)
        mapping = {S.HEALTHY: ServiceState.HEALTHY, S.CONNECTED: ServiceState.HEALTHY, S.SYNCING: ServiceState.HEALTHY, S.DEGRADED: ServiceState.DEGRADED,
                   S.DISCONNECTED: ServiceState.DISCONNECTED if info.configured else ServiceState.DISABLED, S.ERROR: ServiceState.FAILED, S.DISABLED: ServiceState.DISABLED,
                   S.AUTHENTICATING: ServiceState.STARTING}
        return Health(mapping[info.status], info.detail[:120])

    return run
