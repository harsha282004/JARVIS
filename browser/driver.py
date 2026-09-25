"""The thin layer between the BrowserEngine and a real browser.

`PageDriver` / `BrowserDriver` are small abstract interfaces (so the engine is tested with a deterministic fake and never depends on a
website being up); `PlaywrightBrowserDriver` is the real implementation. Playwright's sync API is thread-affine: the engine calls every
driver method from ONE dedicated thread. This module never accepts script text: page scripts are the named constants in `browser.scripts`.
"""

import time
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from backend.core.logging import get_logger
from browser import scripts
from browser.models import BrowserCrashed, BrowserUnavailable, ElementInfo, MediaState, PageSnapshot, Target
from browser.urlsafe import validate_url

logger = get_logger(__name__)

ALLOWED_KEYS = frozenset({"Enter", "Escape", "Tab", "Space", "ArrowUp", "ArrowDown", "ArrowLeft", "ArrowRight", "PageUp", "PageDown", "Home", "End",
                          "k", "j", "l", "m", "f", "n"})  # single keys only: no Control/Alt/Meta combinations, no function keys


@dataclass
class DownloadEvent:
    suggested_filename: str
    url: str
    save: Callable[[str], None]  # save(path)


class PageDriver(ABC):
    @property
    @abstractmethod
    def url(self) -> str: ...

    @property
    @abstractmethod
    def title(self) -> str: ...

    @property
    @abstractmethod
    def is_crashed(self) -> bool: ...

    @abstractmethod
    def goto(self, url: str, timeout_ms: int) -> int | None: ...

    @abstractmethod
    def back(self, timeout_ms: int) -> bool: ...

    @abstractmethod
    def forward(self, timeout_ms: int) -> bool: ...

    @abstractmethod
    def reload(self, timeout_ms: int) -> None: ...

    @abstractmethod
    def wait_ready(self, timeout_ms: int) -> None: ...

    @abstractmethod
    def snapshot(self) -> PageSnapshot: ...

    @abstractmethod
    def match(self, target: Target) -> list[ElementInfo]: ...

    @abstractmethod
    def click(self, target: Target, timeout_ms: int) -> None: ...

    @abstractmethod
    def fill(self, target: Target, text: str, timeout_ms: int) -> bool: ...

    @abstractmethod
    def field_type(self, target: Target) -> str: ...

    @abstractmethod
    def press(self, key: str) -> None: ...

    @abstractmethod
    def scroll(self, direction: str, amount: int) -> tuple[int, int]: ...

    @abstractmethod
    def wait_for(self, target: Target, timeout_ms: int) -> bool: ...

    @abstractmethod
    def media_state(self) -> MediaState: ...

    @abstractmethod
    def media_command(self, cmd: str, value: float = 0.0) -> bool: ...

    @abstractmethod
    def extract(self, name: str) -> list[dict[str, Any]]: ...

    @abstractmethod
    def screenshot(self) -> bytes: ...

    @abstractmethod
    def set_files(self, target: Target, path: str) -> list[str]: ...

    @abstractmethod
    def pop_downloads(self, wait_ms: int) -> list[DownloadEvent]: ...

    @abstractmethod
    def bring_to_front(self) -> None: ...

    @abstractmethod
    def close(self) -> None: ...


class BrowserDriver(ABC):
    @abstractmethod
    def launch(self) -> None: ...

    @abstractmethod
    def new_page(self) -> PageDriver: ...

    @abstractmethod
    def take_new_pages(self) -> list[PageDriver]:
        """Pages the site opened itself (target=_blank, window.open) since the last call."""

    @property
    @abstractmethod
    def alive(self) -> bool: ...

    @abstractmethod
    def close(self) -> None: ...


# ---- Playwright ---------------------------------------------------------------------------------------------------------------------

_DEAD = ("target closed", "has been closed", "browser closed", "connection closed", "target page, context or browser", "browser has been", "crashed")


def _crash_or(exc: Exception) -> Exception:
    text = str(exc).lower()
    if any(marker in text for marker in _DEAD):
        return BrowserCrashed("The browser or the page went away.")
    return exc


class PlaywrightPageDriver(PageDriver):
    def __init__(self, page):
        self._page = page
        self._crashed = False
        self._downloads: list[DownloadEvent] = []
        page.on("crash", lambda *_: setattr(self, "_crashed", True))
        page.on("download", self._on_download)

    def _on_download(self, download) -> None:
        self._downloads.append(DownloadEvent(download.suggested_filename, download.url, lambda path: download.save_as(path)))

    def _guard(self, fn, *args, **kwargs):
        if self._crashed:
            raise BrowserCrashed("The page crashed.")
        try:
            return fn(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001 - map "the browser went away" to one typed error, leave others
            raise _crash_or(exc) from None

    @property
    def url(self) -> str:
        try:
            return self._page.url
        except Exception:  # noqa: BLE001
            return ""

    @property
    def title(self) -> str:
        try:
            return self._page.title()
        except Exception:  # noqa: BLE001
            return ""

    @property
    def is_crashed(self) -> bool:
        try:
            return self._crashed or self._page.is_closed()
        except Exception:  # noqa: BLE001
            return True

    def goto(self, url, timeout_ms):
        try:
            response = self._guard(self._page.goto, url, timeout=timeout_ms, wait_until="domcontentloaded")
        except Exception as exc:  # noqa: BLE001
            if "ERR_HTTP_RESPONSE_CODE_FAILURE" in str(exc):
                return 0  # the site answered with an error status and no page: reported as an error, status unknown
            raise
        return response.status if response is not None else None

    def back(self, timeout_ms):
        return self._guard(self._page.go_back, timeout=timeout_ms, wait_until="domcontentloaded") is not None

    def forward(self, timeout_ms):
        return self._guard(self._page.go_forward, timeout=timeout_ms, wait_until="domcontentloaded") is not None

    def reload(self, timeout_ms):
        self._guard(self._page.reload, timeout=timeout_ms, wait_until="domcontentloaded")

    def wait_ready(self, timeout_ms):
        try:
            self._guard(self._page.wait_for_load_state, "load", timeout=timeout_ms)
        except BrowserCrashed:
            raise
        except Exception:  # noqa: BLE001 - a page that never finishes "load" (streaming, long polling) is still usable
            pass

    def snapshot(self):
        raw = self._guard(self._page.evaluate, scripts.SNAPSHOT)

        def conv(items):
            return [ElementInfo(i.get("role", ""), i.get("name", ""), i.get("tag", ""), i.get("input_type", ""), i.get("href", ""), True, i.get("enabled", True)) for i in items]

        return PageSnapshot(url=raw["url"], title=raw["title"], headings=raw["headings"], links=conv(raw["links"]), buttons=conv(raw["buttons"]),
                            fields=conv(raw["fields"]), tables=raw["tables"], text=raw["text"], has_password_field=raw["has_password_field"],
                            has_dialog=raw["has_dialog"], captcha=raw["captcha"], loading=raw["loading"], scroll_y=raw["scroll_y"], scroll_max=raw["scroll_max"])

    def _locator(self, t: Target):
        p = self._page
        if t.css:
            loc = p.locator(t.css)
        elif t.role:
            loc = p.get_by_role(t.role, name=t.name, exact=False) if t.name else p.get_by_role(t.role)
        elif t.label:
            loc = p.get_by_label(t.label, exact=False)
        elif t.placeholder:
            loc = p.get_by_placeholder(t.placeholder, exact=False)
        elif t.text or t.name:
            loc = p.get_by_text(t.text or t.name, exact=False)
        else:
            raise ValueError("a target needs a role, name, text, label or placeholder")
        return loc.filter(visible=True)

    def match(self, target):
        loc = self._locator(target)
        out = []
        for i in range(min(self._guard(loc.count), 8)):
            el = loc.nth(i)
            name = (self._guard(el.get_attribute, "aria-label") or self._guard(el.inner_text) or "").strip()[:120]
            out.append(ElementInfo(target.role or "element", " ".join(name.split()), self._guard(el.evaluate, "e => e.tagName.toLowerCase()"),
                                   self._guard(el.get_attribute, "type") or "", self._guard(el.get_attribute, "href") or "", True, self._guard(el.is_enabled)))
        return out

    def click(self, target, timeout_ms):
        self._guard(self._locator(target).nth(target.index or 0).click, timeout=timeout_ms)

    def fill(self, target, text, timeout_ms):
        el = self._locator(target).nth(target.index or 0)
        self._guard(el.fill, text, timeout=timeout_ms)
        return self._guard(el.input_value, timeout=timeout_ms) == text

    def field_type(self, target):
        el = self._locator(target).nth(target.index or 0)
        return (self._guard(el.get_attribute, "type", timeout=2000) or "").lower()

    def press(self, key):
        if key not in ALLOWED_KEYS:
            raise ValueError("key not allowed")
        self._guard(self._page.keyboard.press, key)

    def scroll(self, direction, amount):
        mode = direction if direction in ("top", "bottom") else "by"
        dy = amount if direction == "down" else -amount if direction == "up" else 0
        y, mx = self._guard(self._page.evaluate, scripts.SCROLL, [0, dy, mode])
        return int(y), int(mx)

    def wait_for(self, target, timeout_ms):
        deadline = time.monotonic() + timeout_ms / 1000
        while time.monotonic() < deadline:
            if self._guard(self._locator(target).count) > 0:
                return True
            time.sleep(0.15)
        return False

    def media_state(self):
        raw = self._guard(self._page.evaluate, scripts.MEDIA_STATE)
        return MediaState(**{k: v for k, v in raw.items() if k in MediaState.__dataclass_fields__})

    def media_command(self, cmd, value=0.0):
        return bool(self._guard(self._page.evaluate, scripts.MEDIA_COMMAND, [cmd, value]))

    def extract(self, name):
        script = scripts.TRUSTED.get(name)
        if script is None or name in ("snapshot", "media_state"):
            raise ValueError("unknown extraction")
        return self._guard(self._page.evaluate, script)

    def screenshot(self):
        return self._guard(self._page.screenshot, type="png")

    def set_files(self, target, path):
        el = self._page.locator("input[type=file]").first
        self._guard(el.set_input_files, path)
        return [Path(path).name]

    def pop_downloads(self, wait_ms):
        deadline = time.monotonic() + wait_ms / 1000
        while not self._downloads and time.monotonic() < deadline:
            self._page.wait_for_timeout(100)
        out, self._downloads = self._downloads, []
        return out

    def bring_to_front(self):
        self._guard(self._page.bring_to_front)

    def close(self):
        try:
            self._page.close()
        except Exception:  # noqa: BLE001 - closing a dead page is fine
            pass


class PlaywrightBrowserDriver(BrowserDriver):
    def __init__(self, *, browser_type: str, headless: bool, profile_dir: Path, download_dir: Path, allow_private: bool, nav_timeout_ms: int, default_timeout_ms: int):
        self._type, self._headless, self._profile = browser_type, headless, profile_dir
        self._downloads, self._allow_private = download_dir, allow_private
        self._nav_ms, self._default_ms = nav_timeout_ms, default_timeout_ms
        self._pw = None
        self._ctx = None
        self._new: list[PlaywrightPageDriver] = []
        self._connected = False

    def _channels(self) -> list[str | None]:
        return {"auto": ["msedge", "chrome", None], "msedge": ["msedge"], "chrome": ["chrome"], "chromium": [None]}.get(self._type, ["msedge", "chrome", None])

    def launch(self) -> None:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:  # pragma: no cover
            raise BrowserUnavailable("Playwright is not installed. Run: pip install -r requirements.txt") from exc
        self._profile.mkdir(parents=True, exist_ok=True)
        self._downloads.mkdir(parents=True, exist_ok=True)
        self._pw = sync_playwright().start()
        last: Exception | None = None
        for channel in self._channels():
            try:
                kwargs: dict[str, Any] = {"headless": self._headless, "accept_downloads": True, "downloads_path": str(self._downloads),
                                          "viewport": {"width": 1280, "height": 800} if self._headless else None, "args": ["--disable-notifications"]}
                if channel:
                    kwargs["channel"] = channel
                self._ctx = self._pw.chromium.launch_persistent_context(str(self._profile), **kwargs)
                break
            except Exception as exc:  # noqa: BLE001 - this browser is not installed; try the next
                last = exc
        if self._ctx is None:
            self._stop_playwright()
            raise BrowserUnavailable("I couldn't start a browser. Install Microsoft Edge or Chrome, or run: python -m playwright install chromium") from last
        self._ctx.set_default_timeout(self._default_ms)
        self._ctx.set_default_navigation_timeout(self._nav_ms)
        self._ctx.route("**/*", self._route)
        self._ctx.on("page", lambda page: self._new.append(PlaywrightPageDriver(page)))
        self._ctx.on("close", lambda *_: setattr(self, "_connected", False))
        self._connected = True
        logger.info("BROWSER_LAUNCHED headless=%s", self._headless)

    def _route(self, route) -> None:
        """Main-frame navigations pass the same URL policy as open_url (so a redirect or a click cannot reach file:/private hosts)."""
        request = route.request
        try:
            main_frame = True
            try:
                main_frame = request.frame.parent_frame is None  # a popup's first request has no frame yet: it is a main-frame navigation
            except Exception:  # noqa: BLE001
                pass
            if request.is_navigation_request() and main_frame:
                if not request.url.startswith("about:") and not validate_url(request.url, allow_private=self._allow_private).ok:
                    route.abort("blockedbyclient")
                    return
            route.continue_()
        except Exception:  # noqa: BLE001 - the page went away mid-route; never leave the request hanging
            try:
                route.continue_()
            except Exception:  # noqa: BLE001
                pass

    def new_page(self):
        page = self._ctx.new_page()
        self._new = [p for p in self._new if p._page is not page]  # the "page" event also fired for this page: it is not a popup
        return PlaywrightPageDriver(page)

    def take_new_pages(self):
        out, self._new = self._new, []
        return out

    @property
    def alive(self) -> bool:
        if not self._connected or self._ctx is None:
            return False
        try:
            _ = self._ctx.pages
            return self._ctx.browser is None or self._ctx.browser.is_connected()
        except Exception:  # noqa: BLE001
            return False

    def _stop_playwright(self) -> None:
        if self._pw is not None:
            try:
                self._pw.stop()
            except Exception:  # noqa: BLE001
                pass
            self._pw = None

    def close(self) -> None:
        ctx, self._ctx, self._connected = self._ctx, None, False
        if ctx is not None:
            try:
                ctx.close()
            except Exception:  # noqa: BLE001
                pass
        self._stop_playwright()
