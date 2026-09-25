"""BrowserControl (what the dashboard, tray and health check see), the ComputerState abstraction, and the production builder.

`ComputerState` is the seam a later phase can extend (active window, application, screen, input). Only the browser part exists now;
the other fields are deliberately empty rather than half-built.
"""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from backend.core.metrics import metrics
from browser.downloads import BrowserLog
from browser.engine import BrowserConfig, BrowserEngine
from browser.tools import BrowserTools
from browser.voice import BrowserRouter


@dataclass
class ComputerState:
    browser: dict[str, Any] | None = None
    active_window: str | None = None        # not implemented yet
    active_application: str | None = None   # not implemented yet
    screen: dict[str, Any] | None = None    # not implemented yet
    input: dict[str, Any] | None = None     # not implemented yet

    @classmethod
    def read(cls, engine: BrowserEngine) -> "ComputerState":
        return cls(browser=engine.status())


@dataclass
class BrowserControl:
    engine: BrowserEngine
    tools: BrowserTools
    log: BrowserLog
    router: BrowserRouter | None = None
    enabled: bool = True
    _extra: dict[str, Any] = field(default_factory=dict)

    def snapshot(self) -> dict[str, Any]:
        st = self.engine.status()
        timers = metrics.snapshot()["timers"]
        counters = metrics.snapshot()["counters"]
        st["enabled"] = self.enabled
        st["headless"] = self._extra.get("headless")
        st["browser_type"] = self._extra.get("browser_type")
        st["metrics_ms"] = {k: v for k, v in timers.items() if k.startswith("browser.")}
        st["counters"] = {k: v for k, v in counters.items() if k.startswith("browser.")}
        st["computer"] = {"browser": True, "active_window": None, "active_application": None}
        return st

    def open_browser(self):
        """Tray "Open browser": start the browser with one empty tab (nothing is navigated). No-op if a page is already open."""
        if self.engine.status()["tab_count"] > 0:
            return self.tools.call("get_page_state", {}, session_id="tray")
        return self.tools.call("open_new_tab", {}, session_id="tray")

    def close_browser(self):
        return self.tools.call("close_browser", {}, session_id="tray")

    def stop_action(self) -> None:
        self.engine.stop_current_action()

    def shutdown(self) -> None:
        self.engine.shutdown()


def build_browser(settings, state_dir: Path, project_root: Path) -> BrowserControl | None:
    """The browser agent from application settings. Building it never starts a browser (it opens on demand). None when disabled."""
    if not settings.BROWSER_ENABLED:
        return None
    from browser.driver import PlaywrightBrowserDriver

    def resolve(value: str) -> Path:
        p = Path(value).expanduser()
        return p if p.is_absolute() else project_root / p

    config = BrowserConfig(
        default_timeout_s=settings.BROWSER_DEFAULT_TIMEOUT_SECONDS, navigation_timeout_s=settings.BROWSER_NAVIGATION_TIMEOUT_SECONDS, max_tabs=settings.BROWSER_MAX_TABS,
        retries=settings.BROWSER_RETRIES, download_dir=resolve(settings.BROWSER_DOWNLOAD_DIR), upload_dir=resolve(settings.BROWSER_UPLOAD_DIR),
        screenshot_mode=settings.BROWSER_SCREENSHOT_MODE if settings.BROWSER_SCREENSHOT_MODE in ("off", "memory", "disk") else "memory",
        screenshot_dir=state_dir / "screenshots", allow_private_hosts=settings.BROWSER_ALLOW_PRIVATE_HOSTS, search_url=settings.BROWSER_SEARCH_URL,
    )
    log = BrowserLog(state_dir / "browser_log.jsonl")

    def factory():
        return PlaywrightBrowserDriver(browser_type=settings.BROWSER_TYPE, headless=settings.BROWSER_HEADLESS, profile_dir=resolve(settings.BROWSER_PROFILE_DIR),
                                       download_dir=config.download_dir, allow_private=config.allow_private_hosts,
                                       nav_timeout_ms=int(config.navigation_timeout_s * 1000), default_timeout_ms=int(config.default_timeout_s * 1000))

    engine = BrowserEngine(factory, config, log)
    tools = BrowserTools(engine)
    return BrowserControl(engine, tools, log, enabled=True, _extra={"headless": settings.BROWSER_HEADLESS, "browser_type": settings.BROWSER_TYPE})
