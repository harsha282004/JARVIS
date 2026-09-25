"""BrowserEngine: the one place a browser is driven.

Owns the browser lifecycle, the session (tabs, active tab, last action), navigation, element targeting, verification, downloads/uploads,
crash recovery, timeouts and metrics. Every public method returns a normalized `BrowserResult` and never raises. All driver calls run on ONE
dedicated thread (Playwright's sync API is thread-affine), so the voice thread, the API and the tray can call in safely.

Rules the engine enforces (docs/BROWSER_SECURITY.md):
  * URLs pass `validate_url` before and after navigation; only http/https, never this computer or its network.
  * An action is "verified" only when the page (or media) state actually changed as expected; otherwise it reports failure.
  * Only safe, repeatable operations are ever retried (loading, reading, finding). A click, a typed value, an upload is never repeated
    blindly, not even after a crash: the user is told the real state instead.
  * No password field is typed into or read, no cookie or storage is touched, no script other than the fixed ones in `browser.scripts` runs.
"""

import queue
import re
import threading
import time
import uuid
from collections.abc import Callable
from concurrent.futures import Future
from concurrent.futures import TimeoutError as FutureTimeout
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from backend.core.logging import get_logger
from backend.core.metrics import metrics
from backend.core.security.trust import sanitize_external, scan_for_injection
from browser.downloads import BrowserLog, DownloadManager, safe_filename
from browser.driver import ALLOWED_KEYS, BrowserDriver, PageDriver
from browser.models import (
    ActionCancelled,
    ActionTimeout,
    BrowserCrashed,
    BrowserError,
    BrowserResult,
    BrowserState,
    BrowserUnavailable,
    DownloadRecord,
    ElementInfo,
    MediaState,
    PageSnapshot,
    TabInfo,
    Target,
    public_url,
)
from browser.urlsafe import Resolver, same_site, system_resolver, validate_url

logger = get_logger(__name__)

MAX_TYPED_CHARS = 500
MAX_SCROLL_PIXELS = 5000
MAX_UPLOAD_BYTES = 25 * 1024 * 1024
UPLOAD_BLOCKED = frozenset({".exe", ".bat", ".cmd", ".ps1", ".vbs", ".js", ".lnk", ".reg", ".dll", ".msi", ".scr", ".sh"})
_STOPWORDS = frozenset({"the", "a", "an", "button", "link", "tab", "field", "box", "input", "menu", "icon", "page", "please", "on", "of", "to", "for", "my"})


@dataclass
class BrowserConfig:
    default_timeout_s: float = 10.0
    navigation_timeout_s: float = 20.0
    max_tabs: int = 8
    retries: int = 2
    download_dir: Path = Path(".jarvis/downloads")
    upload_dir: Path = Path(".jarvis/uploads")
    screenshot_mode: str = "memory"   # off | memory | disk
    screenshot_dir: Path = Path(".jarvis/screenshots")
    allow_private_hosts: bool = False
    search_url: str = "https://www.bing.com/search?q={query}"
    resolver: Resolver | None = system_resolver
    settle_seconds: float = 2.0       # how long to wait for a click to change something


@dataclass
class Tab:
    tab_id: str
    page: PageDriver
    opened_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="seconds"))
    url: str = ""      # last known address/title, refreshed on the browser thread: other threads (dashboard, tray) read these, never the page
    title: str = ""


class BrowserSession:
    def __init__(self) -> None:
        self.session_id = uuid.uuid4().hex[:12]
        self.tabs: dict[str, Tab] = {}
        self.active: str | None = None
        self.last_action = ""
        self.action_status = ""
        self._counter = 0

    def next_id(self) -> str:
        self._counter += 1
        return f"t{self._counter}"


class _Worker:
    """One thread that runs every browser call, in order."""

    def __init__(self) -> None:
        self._q: queue.Queue = queue.Queue()
        self._thread = threading.Thread(target=self._run, name="jarvis-browser", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        while True:
            item = self._q.get()
            if item is None:
                return
            fn, future = item
            if not future.set_running_or_notify_cancel():
                continue
            try:
                future.set_result(fn())
            except BaseException as exc:  # noqa: BLE001 - delivered to the caller
                future.set_exception(exc)

    def submit(self, fn: Callable[[], Any]) -> Future:
        future: Future = Future()
        self._q.put((fn, future))
        return future

    def stop(self) -> None:
        self._q.put(None)

    @property
    def is_worker_thread(self) -> bool:
        return threading.current_thread() is self._thread


def _tokens(text: str) -> set[str]:
    return {t for t in re.findall(r"[a-z0-9]+", text.lower()) if t not in _STOPWORDS}


class BrowserEngine:
    def __init__(self, driver_factory: Callable[[], BrowserDriver], config: BrowserConfig | None = None, log: BrowserLog | None = None,
                 sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.monotonic):
        self._factory = driver_factory
        self.config = config or BrowserConfig()
        self.log = log or BrowserLog(None)
        self._sleep = sleep
        self._clock = clock
        self._worker = _Worker()
        self._lock = threading.RLock()
        self._driver: BrowserDriver | None = None
        self._state = BrowserState.CLOSED
        self.session = BrowserSession()
        self._cancel = threading.Event()
        self.downloads = DownloadManager(self.config.download_dir, self.log)
        self.last_result: BrowserResult | None = None
        self.last_error = ""
        self._last_url = ""
        self.crashes = 0
        self.recoveries = 0
        self.closed_for_good = False

    # ---- observation (any thread) ----------------------------------------------------------------------------------------------

    @property
    def state(self) -> BrowserState:
        return self._state

    def _set_state(self, state: BrowserState) -> None:
        self._state = state

    def status(self) -> dict[str, Any]:
        """A snapshot for the dashboard/tray/agent: state, tabs (public URLs only), last action. No page content, no cookies."""
        with self._lock:
            tabs = self._tab_infos()
            active = next((t for t in tabs if t.active), None)
            last = self.last_result
            return {
                "state": self._state.value, "session_id": self.session.session_id, "tabs": [t.__dict__ for t in tabs], "tab_count": len(tabs),
                "active_tab": active.tab_id if active else None, "url": active.url if active else "", "title": active.title if active else "",
                "last_action": self.session.last_action, "action_status": self.session.action_status,
                "last_result": last.message or last.error if last else "", "verified": last.verified if last else None,
                "error": self.last_error, "crashes": self.crashes, "recoveries": self.recoveries,
                "downloads": [{"filename": d.filename, "saved": d.saved_path is not None, "blocked": d.blocked, "at": d.timestamp} for d in self.downloads.records[-5:]],
            }

    def _tab_infos(self) -> list[TabInfo]:
        out = []
        for tab in self.session.tabs.values():
            out.append(TabInfo(tab.tab_id, public_url(tab.url), sanitize_external(tab.title, 80), tab.tab_id == self.session.active))
        return out

    def stop_current_action(self) -> None:
        """Cooperative cancel: the running action stops at its next check (they poll at least every 200 ms)."""
        self._cancel.set()

    # ---- the guarded runner ------------------------------------------------------------------------------------------------------

    def _check_cancel(self) -> None:
        if self._cancel.is_set():
            raise ActionCancelled("Stopped.")

    def _wait(self, seconds: float) -> None:
        end = self._clock() + seconds
        while self._clock() < end:
            self._check_cancel()
            self._sleep(min(0.1, max(0.0, end - self._clock())))

    def _translate(self, exc: BaseException) -> BrowserError:
        if isinstance(exc, BrowserError):
            return exc
        name = type(exc).__name__
        if name == "TimeoutError":
            return ActionTimeout("That took too long.")
        text = str(exc).lower()
        if "net::err_" in text:
            if "cert" in text or "ssl" in text:
                return BrowserError("That website's security certificate isn't valid, so I stopped.")
            if "name_not_resolved" in text:
                return BrowserError("That website's name doesn't resolve.")
            if "unsafe_port" in text:
                return BrowserError("I won't connect to that port.")
            if any(k in text for k in ("connection_refused", "timed_out", "internet_disconnected", "connection_reset", "network_changed", "address_unreachable", "connection_closed")):
                return BrowserError("I couldn't connect to that website.")
            if "blockedbyclient" in text or "blocked_by_client" in text:
                return BrowserError("That address is blocked, so I didn't open it.")
            return BrowserError("The website couldn't be loaded.")
        if any(m in text for m in ("target closed", "has been closed", "browser closed", "connection closed", "crashed")):
            return BrowserCrashed("The browser went away.")
        return BrowserError(f"The browser reported an error ({name}).")

    def _run(self, action: str, target: str, fn: Callable[[], BrowserResult], *, safe: bool, start: bool = True, timeout_s: float | None = None, record: bool = True) -> BrowserResult:
        """Run `fn` on the browser thread with a bound, recovery and (only when `safe`) bounded retries. Never raises."""
        if self.closed_for_good:
            result = BrowserResult(False, action, target, error="The browser is shut down.")
            if record:
                self._record(result)
            return result
        began = time.perf_counter()
        self._cancel.clear()
        limit = (timeout_s or self.config.navigation_timeout_s * 2 + 10)
        result: BrowserResult | None = None
        retried, recovered = 0, False
        attempts = 1 + (self.config.retries if safe else 0)
        try:
            for attempt in range(attempts):
                try:
                    future = self._worker.submit(lambda: self._guarded(action, fn, start))
                    result = future.result(timeout=limit)
                    break
                except FutureTimeout:
                    self._cancel.set()
                    raise ActionTimeout("That took too long.") from None
                except BaseException as raw:  # noqa: BLE001
                    exc = self._translate(raw)
                    if isinstance(exc, BrowserCrashed):
                        metrics.incr("browser.crashes")
                        self.crashes += 1
                        recovered = self._recover_from_crash()
                        if not safe or attempt + 1 >= attempts or not recovered:
                            result = BrowserResult(False, action, target, error=("The browser crashed and I restarted it. Please ask again."
                                                                                 if recovered else "The browser crashed and I couldn't restart it."), recovered=recovered)
                            break
                    elif isinstance(exc, ActionTimeout) and safe and attempt + 1 < attempts:
                        pass
                    else:
                        raise exc from None
                    retried += 1
                    metrics.incr("browser.retries")
                    self._sleep(0.3 * (attempt + 1))
        except ActionCancelled:
            result = BrowserResult(False, action, target, error="Stopped.")
        except BrowserUnavailable as exc:
            self._set_state(BrowserState.ERROR)
            result = BrowserResult(False, action, target, error=str(exc))
        except BrowserError as exc:
            result = BrowserResult(False, action, target, error=str(exc))
        except Exception as exc:  # noqa: BLE001 - the boundary: nothing escapes as an exception
            logger.error("Browser action %s failed unexpectedly (%s)", action, type(exc).__name__)
            result = BrowserResult(False, action, target, error=f"Something unexpected went wrong ({type(exc).__name__}).")
        assert result is not None
        result.duration_ms = (time.perf_counter() - began) * 1000
        result.retried, result.recovered = retried, result.recovered or recovered
        if record:
            self._record(result)
        return result

    def _guarded(self, action: str, fn: Callable[[], BrowserResult], start: bool) -> BrowserResult:
        """Runs on the browser thread."""
        if start:
            self._ensure_open()
            self._check_tab_health()
        self._set_state(BrowserState.ACTION if self._state in (BrowserState.READY, BrowserState.ACTION) else self._state)
        try:
            return fn()
        finally:
            self._refresh_tabs()
            if self._state in (BrowserState.ACTION, BrowserState.NAVIGATING, BrowserState.WAITING):
                self._set_state(BrowserState.READY if self._driver is not None else BrowserState.CLOSED)

    def _refresh_tabs(self) -> None:
        """Runs on the browser thread: copy each tab's address and title where the dashboard/tray can read them safely."""
        for tab in list(self.session.tabs.values()):
            try:
                tab.url, tab.title = tab.page.url, tab.page.title
            except Exception:  # noqa: BLE001 - a dying page keeps its last known values
                pass

    def _record(self, result: BrowserResult) -> None:
        with self._lock:
            self.last_result = result
            self.session.last_action = result.action
            self.session.action_status = "ok" if result.success and result.verified else "unverified" if result.success else "failed"
            self.last_error = "" if result.success else result.error
        metrics.observe("browser.action_ms", result.duration_ms)
        metrics.incr("browser.actions")
        if not result.success:
            metrics.incr("browser.failures")
        self.log.event(result.action, session_id=self.session.session_id, target=result.target, url=result.url, duration_ms=result.duration_ms,
                       verified=result.verified, result="success" if result.success else "failure: " + result.error, retried=result.retried or None,
                       recovered=result.recovered or None)

    # ---- lifecycle (browser thread) --------------------------------------------------------------------------------------------

    def _ensure_open(self) -> None:
        if self.closed_for_good:
            raise BrowserUnavailable("The browser is shut down.")
        if self._driver is not None and self._driver.alive:
            return
        if self._driver is not None:  # it died between calls
            raise BrowserCrashed("The browser went away.")
        self._set_state(BrowserState.OPENING)
        began = time.perf_counter()
        driver = self._factory()
        try:
            driver.launch()
        except BrowserError:
            self._set_state(BrowserState.ERROR)
            raise
        except Exception as exc:  # noqa: BLE001
            self._set_state(BrowserState.ERROR)
            raise BrowserUnavailable("I couldn't start a browser.") from exc
        metrics.observe("browser.startup_ms", (time.perf_counter() - began) * 1000)
        with self._lock:
            self._driver = driver
            self.session = BrowserSession()
        self._set_state(BrowserState.READY)

    def _teardown(self) -> None:
        driver, self._driver = self._driver, None
        if driver is not None:
            try:
                driver.close()
            except Exception:  # noqa: BLE001 - cleaning up a dead browser
                pass
        with self._lock:
            self.session.tabs.clear()
            self.session.active = None

    def _recover_from_crash(self) -> bool:
        """Detect -> clean up stale state -> relaunch -> reopen the page that was active (a plain GET of its public URL). Never replays an action."""
        self._set_state(BrowserState.RECOVERING)
        url = self._last_url
        try:
            self._worker.submit(self._teardown).result(timeout=20)
            if self.closed_for_good:
                return False

            def relaunch() -> None:
                self._ensure_open()
                if url and validate_url(url, allow_private=self.config.allow_private_hosts, resolver=None).ok:
                    try:
                        page = self._new_tab()
                        page.goto(url, int(self.config.navigation_timeout_s * 1000))
                    except Exception:  # noqa: BLE001 - the browser is back even if the page is not
                        pass

            self._worker.submit(relaunch).result(timeout=self.config.navigation_timeout_s * 2 + 30)
        except Exception as exc:  # noqa: BLE001
            logger.error("Browser recovery failed (%s)", type(exc).__name__)
            self._set_state(BrowserState.ERROR)
            return False
        self.recoveries += 1
        metrics.incr("browser.recoveries")
        self._set_state(BrowserState.READY)
        return True

    def _check_tab_health(self) -> None:
        """One tab crashing (not the whole browser): replace it with a fresh tab at the same address."""
        tab = self._active_tab(required=False)
        if tab is not None and tab.page.is_crashed:
            url = self._last_url
            self.crashes += 1
            metrics.incr("browser.crashes")
            self._drop_tab(tab.tab_id)
            page = self._new_tab()
            if url and validate_url(url, allow_private=self.config.allow_private_hosts, resolver=None).ok:
                try:
                    page.goto(url, int(self.config.navigation_timeout_s * 1000))
                except Exception:  # noqa: BLE001
                    pass
            self.recoveries += 1
            metrics.incr("browser.recoveries")

    # ---- tabs (browser thread) -----------------------------------------------------------------------------------------------------

    def _new_tab(self) -> PageDriver:
        if len(self.session.tabs) >= self.config.max_tabs:
            raise BrowserError(f"I already have {self.config.max_tabs} tabs open. Close one first.")
        page = self._driver.new_page()
        tab = Tab(self.session.next_id(), page)
        with self._lock:
            self.session.tabs[tab.tab_id] = tab
            self.session.active = tab.tab_id
        return page

    def _drop_tab(self, tab_id: str) -> None:
        with self._lock:
            tab = self.session.tabs.pop(tab_id, None)
            if self.session.active == tab_id:
                self.session.active = next(reversed(self.session.tabs), None) if self.session.tabs else None
        if tab is not None:
            tab.page.close()

    def _active_tab(self, required: bool = True) -> Tab | None:
        tab = self.session.tabs.get(self.session.active or "")
        if tab is None and required:
            raise BrowserError("No page is open yet.")
        return tab

    def _page(self) -> PageDriver:
        return self._active_tab().page  # type: ignore[union-attr]

    def _adopt_popups(self) -> int:
        adopted = 0
        for page in self._driver.take_new_pages():
            if page.url.startswith(("edge:", "chrome:", "devtools:", "chrome-extension:", "file:", "view-source:")):
                continue  # a browser-internal page (e.g. the downloads panel) is not something to operate on; it is left alone, not adopted
            if len(self.session.tabs) >= self.config.max_tabs:
                page.close()
                continue
            tab = Tab(self.session.next_id(), page)
            with self._lock:
                self.session.tabs[tab.tab_id] = tab
                self.session.active = tab.tab_id
            adopted += 1
        return adopted

    def _remember(self, page: PageDriver) -> None:
        url = page.url
        if url.startswith(("http://", "https://")):
            self._last_url = url

    # ---- navigation ----------------------------------------------------------------------------------------------------------------

    def open_url(self, url: str, *, new_tab: bool = False, expect_host: str | None = None) -> BrowserResult:
        decision = validate_url(url, allow_private=self.config.allow_private_hosts, resolver=self.config.resolver)
        if not decision.ok:
            return self._reject("open_url", url, decision.reason)
        return self._run("open_url", decision.host, lambda: self._open(decision.url, decision.host, decision.suspicious, new_tab, expect_host), safe=True)

    def _open(self, url: str, host: str, suspicious: str, new_tab: bool, expect_host: str | None) -> BrowserResult:
        began = time.perf_counter()
        from urllib.parse import urlsplit

        root = urlsplit(url).path in ("", "/") and not urlsplit(url).query
        if not new_tab and root:  # idempotent: "open YouTube" when YouTube is already open reuses that tab
            for tab in self.session.tabs.values():
                cur = tab.page.url
                if same_site(cur, url) and urlsplit(cur).path in ("", "/") and not tab.page.is_crashed:
                    self.session.active = tab.tab_id
                    tab.page.bring_to_front()
                    snap = self._snapshot_safe(tab.page)
                    return self._page_result("open_url", url, tab.page, snap, f"{host} is already open.", host, expect_host)
        current = self._active_tab(required=False)
        # Opening a DIFFERENT site keeps the page the user was on (a playing video, a form): it goes to a new tab when there is room.
        elsewhere = current is not None and current.page.url.startswith(("http://", "https://")) and not same_site(current.page.url, url) and len(self.session.tabs) < self.config.max_tabs
        page = self._new_tab() if (new_tab or current is None or elsewhere) else self._page()
        self._set_state(BrowserState.NAVIGATING)
        status = page.goto(url, int(self.config.navigation_timeout_s * 1000))
        self._set_state(BrowserState.WAITING)
        page.wait_ready(int(self.config.default_timeout_s * 1000))
        self._adopt_popups()
        metrics.observe("browser.navigation_ms", (time.perf_counter() - began) * 1000)
        final = page.url
        check = validate_url(final, allow_private=self.config.allow_private_hosts, resolver=None) if final and not final.startswith("about:") else None
        if check is not None and not check.ok:  # a redirect led somewhere forbidden
            page.goto("about:blank", 5000)
            return BrowserResult(False, "open_url", host, error="That page redirected somewhere I won't open, so I stopped.")
        if status == 0:
            return BrowserResult(False, "open_url", host, public_url(final), error="The website answered with an error.")
        if status is not None and status >= 400:
            return BrowserResult(False, "open_url", host, public_url(final), error=f"The website answered with an error ({status}).")
        self._remember(page)
        snap = self._snapshot_safe(page)
        note = suspicious + " " if suspicious else ""
        return self._page_result("open_url", url, page, snap, f"{note}Opened {host}.", host, expect_host)

    def _snapshot_safe(self, page: PageDriver) -> PageSnapshot | None:
        began = time.perf_counter()
        try:
            snap = page.snapshot()
        except BrowserCrashed:
            raise
        except Exception:  # noqa: BLE001 - reading state is best effort
            return None
        metrics.observe("browser.verify_ms", (time.perf_counter() - began) * 1000)
        return snap

    def _page_result(self, action: str, requested: str, page: PageDriver, snap: PageSnapshot | None, message: str, host: str, expect_host: str | None) -> BrowserResult:
        final = page.url
        expected = expect_host or host
        # A site with a port (a local server) is only the same site with the same port, so compare against the address that was asked for when there is one.
        target = requested if (not expect_host and requested.startswith(("http://", "https://"))) else f"https://{expected}/"
        domain_ok = same_site(final, target) if final.startswith("http") else False
        verified = domain_ok and bool(snap and (snap.title or snap.text))
        data: dict[str, Any] = {"title": sanitize_external(snap.title if snap else page.title, 120)}
        extra = ""
        if snap is not None:
            if snap.has_password_field:
                data["login_required"] = True
                extra = " It is asking you to sign in; please complete the sign-in yourself."
            if snap.captcha:
                data["captcha"] = True
                extra += " It is showing a human-check that only you can complete."
            if snap.has_dialog:
                data["dialog"] = True
        if not domain_ok:
            return BrowserResult(False, action, host, public_url(final), False, error=f"I asked for {expected} but the browser shows {public_url(final) or 'a different page'}.", data=data)
        if not verified:
            return BrowserResult(True, action, host, public_url(final), False, message=f"{message} I couldn't confirm the page loaded.{extra}", data=data, untrusted=True)
        return BrowserResult(True, action, host, public_url(final), True, message=message + extra, data=data, untrusted=True)

    def _reject(self, action: str, target: str, reason: str) -> BrowserResult:
        result = BrowserResult(False, action, public_url(target) or target[:60], error=reason)
        self._record(result)
        return result

    def go_back(self) -> BrowserResult:
        return self._history("go_back", lambda p, ms: p.back(ms))

    def go_forward(self) -> BrowserResult:
        return self._history("go_forward", lambda p, ms: p.forward(ms))

    def _history(self, action: str, step: Callable[[PageDriver, int], bool]) -> BrowserResult:
        def run() -> BrowserResult:
            page = self._page()
            before = page.url
            self._set_state(BrowserState.NAVIGATING)
            moved = step(page, int(self.config.navigation_timeout_s * 1000))
            page.wait_ready(int(self.config.default_timeout_s * 1000))
            if not moved or page.url == before:
                return BrowserResult(False, action, "", public_url(page.url), False, error="There is nothing to go " + ("back" if action == "go_back" else "forward") + " to.")
            self._remember(page)
            return BrowserResult(True, action, "", public_url(page.url), True, message=f"Went {'back' if action == 'go_back' else 'forward'} to {public_url(page.url)}.",
                                 data={"title": sanitize_external(page.title, 120)}, untrusted=True)

        return self._run(action, "", run, safe=False)

    def refresh_page(self) -> BrowserResult:
        def run() -> BrowserResult:
            page = self._page()
            self._set_state(BrowserState.NAVIGATING)
            page.reload(int(self.config.navigation_timeout_s * 1000))
            page.wait_ready(int(self.config.default_timeout_s * 1000))
            snap = self._snapshot_safe(page)
            ok = bool(snap and not snap.loading)
            return BrowserResult(True, "refresh_page", "", public_url(page.url), ok, message="Refreshed the page." if ok else "Reloaded, but the page is still loading.", untrusted=True)

        return self._run("refresh_page", "", run, safe=True)

    def open_new_tab(self, url: str | None = None) -> BrowserResult:
        if url:
            return self.open_url(url, new_tab=True)

        def run() -> BrowserResult:
            before = len(self.session.tabs)
            self._new_tab()
            ok = len(self.session.tabs) == before + 1
            return BrowserResult(ok, "open_new_tab", self.session.active or "", "", ok, message="Opened a new tab." if ok else "", error="" if ok else "The tab did not open.",
                                 data={"tab_id": self.session.active})

        return self._run("open_new_tab", "", run, safe=False)

    def switch_tab(self, tab_id: str) -> BrowserResult:
        def run() -> BrowserResult:
            tab = self.session.tabs.get(tab_id)
            if tab is None:
                return BrowserResult(False, "switch_tab", tab_id, error=f"There is no tab {tab_id}.")
            self.session.active = tab_id
            tab.page.bring_to_front()
            return BrowserResult(True, "switch_tab", tab_id, public_url(tab.page.url), self.session.active == tab_id, message=f"Switched to {tab_id}.", data={"title": sanitize_external(tab.page.title, 80)}, untrusted=True)

        return self._run("switch_tab", tab_id, run, safe=True)

    def close_tab(self, tab_id: str | None = None) -> BrowserResult:
        def run() -> BrowserResult:
            tid = tab_id or self.session.active
            if tid not in self.session.tabs:
                return BrowserResult(False, "close_tab", tid or "", error="There is no such tab.")
            self._drop_tab(tid)
            gone = tid not in self.session.tabs
            return BrowserResult(gone, "close_tab", tid, "", gone, message=f"Closed {tid}.", error="" if gone else "The tab is still open.")

        return self._run("close_tab", tab_id or "", run, safe=False)

    def close_tabs_matching(self, host: str) -> BrowserResult:
        def run() -> BrowserResult:
            ids = [t.tab_id for t in list(self.session.tabs.values()) if same_site(t.page.url, f"https://{host}/")]
            if not ids:
                return BrowserResult(True, "close_tabs", host, "", True, message=f"{host} wasn't open.")
            for tid in ids:
                self._drop_tab(tid)
            left = [t for t in self.session.tabs.values() if same_site(t.page.url, f"https://{host}/")]
            return BrowserResult(not left, "close_tabs", host, "", not left, message=f"Closed {host}.", error="" if not left else "A tab is still open.")

        return self._run("close_tabs", host, run, safe=False, start=self._driver is not None)

    def close_browser(self) -> BrowserResult:
        def run() -> BrowserResult:
            self._set_state(BrowserState.CLOSING)
            self._teardown()
            self._set_state(BrowserState.CLOSED)
            return BrowserResult(True, "close_browser", "", "", True, message="Closed the browser.")

        if self._driver is None:
            result = BrowserResult(True, "close_browser", "", "", True, message="The browser wasn't open.")
            self._record(result)
            return result
        return self._run("close_browser", "", run, safe=False, start=False)

    def shutdown(self) -> None:
        """Application exit: close the browser and stop the worker. Safe to call repeatedly."""
        self.closed_for_good = True
        try:
            self._worker.submit(self._teardown).result(timeout=20)
        except Exception:  # noqa: BLE001
            pass
        self._set_state(BrowserState.CLOSED)
        self._worker.stop()

    # ---- state and reading ------------------------------------------------------------------------------------------------------

    def get_page_state(self) -> BrowserResult:
        def run() -> BrowserResult:
            page = self._page()
            snap = page.snapshot()
            data = {"title": sanitize_external(snap.title, 120), "tabs": [t.__dict__ for t in self._tab_infos()], "loading": snap.loading, "dialog": snap.has_dialog,
                    "login_required": snap.has_password_field, "captcha": snap.captcha, "headings": [sanitize_external(h, 80) for h in snap.headings[:5]],
                    "links": len(snap.links), "buttons": len(snap.buttons), "scroll": [snap.scroll_y, snap.scroll_max]}
            return BrowserResult(True, "get_page_state", "", public_url(snap.url), True, message=f"On {sanitize_external(snap.title, 80) or public_url(snap.url)}.", data=data, untrusted=True)

        return self._run("get_page_state", "", run, safe=True)

    def get_page_title(self) -> BrowserResult:
        r = self.get_page_state()
        if r.success:
            r.action, r.message = "get_page_title", r.data.get("title", "")
        return r

    def get_current_url(self) -> BrowserResult:
        def run() -> BrowserResult:
            page = self._page()
            return BrowserResult(True, "get_current_url", "", public_url(page.url), True, message=public_url(page.url))

        return self._run("get_current_url", "", run, safe=True)

    def read_page(self) -> BrowserResult:
        """Structured, bounded, sanitized page content. All of it is untrusted data; injection-looking text is flagged, never obeyed."""
        def run() -> BrowserResult:
            page = self._page()
            snap = page.snapshot()
            text = sanitize_external(snap.text, 3000)
            scan = scan_for_injection(snap.text)
            data: dict[str, Any] = {
                "title": sanitize_external(snap.title, 120), "headings": [sanitize_external(h, 100) for h in snap.headings],
                "text": text, "links": [{"name": sanitize_external(e.name, 80), "href": public_url(e.href)} for e in snap.links[:15]],
                "buttons": [sanitize_external(e.name, 60) for e in snap.buttons[:15]],
                "forms": [{"label": sanitize_external(e.name, 60), "type": e.input_type or e.tag} for e in snap.fields[:15]],  # labels only, never values
                "tables": [[[sanitize_external(c, 40) for c in row] for row in table[:6]] for table in snap.tables[:1]],
                "injection_suspected": scan.flagged, "untrusted_fields": ["title", "headings", "text", "links", "buttons", "forms", "tables"],
            }
            if scan.flagged:
                data["injection_reasons"] = list(scan.reasons)
            return BrowserResult(True, "read_page", "", public_url(snap.url), True, message="Read the page.", data=data, untrusted=True)

        return self._run("read_page", "", run, safe=True)

    def find_text(self, text: str) -> BrowserResult:
        def run() -> BrowserResult:
            snap = self._page().snapshot()
            hay = " ".join([snap.text, *snap.headings, *(e.name for e in snap.links), *(e.name for e in snap.buttons)])
            needle = text.strip().lower()
            count = hay.lower().count(needle)
            snippets = []
            for m in re.finditer(re.escape(needle), hay.lower()):
                snippets.append(sanitize_external(hay[max(0, m.start() - 30): m.end() + 30], 100))
                if len(snippets) >= 3:
                    break
            found = count > 0
            return BrowserResult(found, "find_text", text[:60], public_url(snap.url), found, message=f"Found '{text[:40]}' {count} time{'s' if count != 1 else ''} on the page.",
                                 error="" if found else f"I couldn't find '{text[:40]}' on this page.", data={"count": count, "snippets": snippets}, untrusted=True)

        return self._run("find_text", text[:60], run, safe=True)

    def find_element(self, description: str) -> BrowserResult:
        """Rank the page's visible controls against a description ("the login link", "download button"). Candidates only; nothing is clicked."""
        def run() -> BrowserResult:
            began = time.perf_counter()
            snap = self._page().snapshot()
            ranked = rank_elements(description, [*snap.buttons, *snap.links, *snap.fields])
            metrics.observe("browser.lookup_ms", (time.perf_counter() - began) * 1000)
            cands = [{"role": e.role, "name": sanitize_external(e.name, 80), "score": round(s, 2)} for s, e in ranked[:5]]
            ok = bool(cands)
            ambiguous = len(cands) > 1 and cands[0]["score"] - cands[1]["score"] < 0.15
            return BrowserResult(ok, "find_element", description[:60], public_url(snap.url), ok, message=f"Found {len(cands)} match{'es' if len(cands) != 1 else ''}." if ok else "",
                                 error="" if ok else f"I couldn't find '{description[:40]}' on this page.", data={"candidates": cands, "ambiguous": ambiguous}, untrusted=True)

        return self._run("find_element", description[:60], run, safe=True)

    def wait_for_element(self, target: Target, timeout_s: float | None = None) -> BrowserResult:
        def run() -> BrowserResult:
            self._set_state(BrowserState.WAITING)
            ms = int((timeout_s or self.config.default_timeout_s) * 1000)
            found = self._page().wait_for(target, ms)
            return BrowserResult(found, "wait_for_element", target.describe(), public_url(self._page().url), found, message=f"{target.describe()} is on the page.",
                                 error="" if found else f"{target.describe()} did not appear.")

        return self._run("wait_for_element", target.describe(), run, safe=True, timeout_s=(timeout_s or self.config.default_timeout_s) + 15)

    # ---- interaction -----------------------------------------------------------------------------------------------------------------

    def click_element(self, target: Target) -> BrowserResult:
        return self._run("click_element", target.describe(), lambda: self._click(target), safe=False)

    def _click(self, target: Target) -> BrowserResult:
        page = self._page()
        began = time.perf_counter()
        matches = page.match(target)
        if not matches:
            self._set_state(BrowserState.WAITING)
            if page.wait_for(target, int(min(3.0, self.config.default_timeout_s) * 1000)):
                matches = page.match(target)
        metrics.observe("browser.lookup_ms", (time.perf_counter() - began) * 1000)
        if not matches:
            return BrowserResult(False, "click_element", target.describe(), public_url(page.url), False, error="Element not found.")
        if len(matches) > 1 and target.index is None:
            names = {(m.role, m.name.lower()) for m in matches}
            exact = [i for i, m in enumerate(matches) if target.name and m.name.lower() == target.name.lower()]
            if len(names) > 1 and len(exact) != 1:
                return BrowserResult(False, "click_element", target.describe(), public_url(page.url), False, error="More than one element matches.",
                                     data={"ambiguous": True, "candidates": [{"role": m.role, "name": sanitize_external(m.name, 60)} for m in matches[:5]]}, untrusted=True)
            if len(exact) == 1:
                target = Target(target.role, target.name, target.text, target.label, target.placeholder, target.css, exact[0])
        chosen = matches[target.index or 0] if (target.index or 0) < len(matches) else matches[0]
        if not chosen.enabled:
            return BrowserResult(False, "click_element", target.describe(), public_url(page.url), False, error="That control is disabled.")
        before = page.snapshot()
        tabs_before = len(self.session.tabs)
        self._set_state(BrowserState.ACTION)
        page.click(target, int(self.config.default_timeout_s * 1000))
        changed, downloads = self._await_change(page, before)
        popups = self._adopt_popups()
        after_page = self._page()
        self._remember(after_page)
        if downloads:
            names = ", ".join(d.filename for d in downloads)
            saved = [d for d in downloads if d.saved_path]
            if saved:
                return BrowserResult(True, "click_element", target.describe(), public_url(after_page.url), True, message=f"Downloaded {names}.",
                                     data={"downloads": [d.__dict__ for d in downloads]})
            return BrowserResult(False, "click_element", target.describe(), public_url(after_page.url), False, error="The download was not saved: " + "; ".join(d.reason for d in downloads),
                                 data={"downloads": [d.__dict__ for d in downloads]})
        if changed or popups or len(self.session.tabs) != tabs_before:
            return BrowserResult(True, "click_element", target.describe(), public_url(after_page.url), True, message=f"Clicked {target.describe()}.",
                                 data={"title": sanitize_external(after_page.title, 100)}, untrusted=True)
        return BrowserResult(False, "click_element", target.describe(), public_url(after_page.url), False, error="I clicked it, but nothing on the page changed.")

    def _await_change(self, page: PageDriver, before: PageSnapshot) -> tuple[bool, list[DownloadRecord]]:
        """Poll (bounded) until the page differs from `before`, a tab opens or a file downloads."""
        end = self._clock() + self.config.settle_seconds
        base, url = before.signature(), before.url
        downloads: list[DownloadRecord] = []
        while True:
            self._check_cancel()
            for event in page.pop_downloads(0):
                downloads.append(self.downloads.handle(event, self.session.session_id))
            if downloads:
                return True, downloads
            try:
                now = self._active_tab().page
                snap = now.snapshot()
            except BrowserCrashed:
                raise
            except Exception:  # noqa: BLE001 - mid-navigation: the old context is gone, which means the page changed
                return True, downloads
            if snap.signature() != base or snap.url != url:
                return True, downloads
            if self._adopt_popups():  # the click opened a new tab
                return True, downloads
            if self._clock() >= end:
                return False, downloads
            self._sleep(0.15)

    def type_text(self, target: Target, text: str, submit: bool = False) -> BrowserResult:
        if len(text) > MAX_TYPED_CHARS:
            return self._reject("type_text", target.describe(), f"That's too much text to type ({MAX_TYPED_CHARS} characters at most).")
        return self._run("type_text", target.describe(), lambda: self._type(target, text, submit), safe=False)

    def _type(self, target: Target, text: str, submit: bool) -> BrowserResult:
        page = self._page()
        if not page.match(target):
            return BrowserResult(False, "type_text", target.describe(), public_url(page.url), False, error="Field not found.")
        kind = page.field_type(target)
        label = (target.label or target.name or target.placeholder or "").lower()
        if kind == "password" or "password" in label or "passcode" in label:
            return BrowserResult(False, "type_text", target.describe(), public_url(page.url), False, error="I never type into password fields. Please enter it yourself.")
        before = page.snapshot()
        ok = page.fill(target, text, int(self.config.default_timeout_s * 1000))
        if not ok:
            return BrowserResult(False, "type_text", target.describe(), public_url(page.url), False, error="The text did not appear in the field.")
        if submit:
            page.press("Enter")
            changed, _ = self._await_change(page, before)
            self._adopt_popups()
            self._remember(self._page())
            return BrowserResult(changed, "type_text", target.describe(), public_url(self._page().url), changed, message="Typed it and pressed Enter.",
                                 error="" if changed else "I typed it and pressed Enter, but nothing changed.", untrusted=True)
        return BrowserResult(True, "type_text", target.describe(), public_url(page.url), True, message=f"Typed {len(text)} characters into {target.describe()}.")

    def press_key(self, key: str) -> BrowserResult:
        if key not in ALLOWED_KEYS:
            return self._reject("press_key", key, "I only press simple keys such as Enter, Escape, Tab, Space or the arrow keys.")

        def run() -> BrowserResult:
            page = self._page()
            before = page.snapshot().signature()
            page.press(key)
            changed = page.snapshot().signature() != before
            return BrowserResult(True, "press_key", key, public_url(page.url), changed, message=f"Pressed {key}." + ("" if changed else " Nothing visibly changed."))

        return self._run("press_key", key, run, safe=False)

    def scroll(self, direction: str, amount: int = 600) -> BrowserResult:
        if direction not in ("up", "down", "top", "bottom"):
            return self._reject("scroll", direction, "I can scroll up, down, to the top or to the bottom.")
        amount = max(50, min(int(amount), MAX_SCROLL_PIXELS))

        def run() -> BrowserResult:
            page = self._page()
            y0, mx0 = page.scroll("down", 0)
            y1, mx = page.scroll(direction, amount)
            moved = y1 != y0
            at_edge = (direction in ("down", "bottom") and y1 >= mx) or (direction in ("up", "top") and y1 <= 0)
            if moved:
                return BrowserResult(True, "scroll", direction, public_url(page.url), True, message=(f"Scrolled to the {direction}." if direction in ("top", "bottom") else f"Scrolled {direction}."), data={"position": [y1, mx]})
            if at_edge:
                return BrowserResult(True, "scroll", direction, public_url(page.url), True, message=f"Already at the {'bottom' if direction in ('down', 'bottom') else 'top'} of the page.", data={"position": [y1, mx]})
            return BrowserResult(False, "scroll", direction, public_url(page.url), False, error="The page did not scroll.")

        return self._run("scroll", direction, run, safe=False)

    def take_screenshot(self) -> BrowserResult:
        mode = self.config.screenshot_mode

        def run() -> BrowserResult:
            page = self._page()
            snap = page.snapshot()
            if snap.has_password_field:
                return BrowserResult(False, "take_screenshot", "", public_url(page.url), False, error="This looks like a sign-in page, so I won't capture it.")
            png = page.screenshot()
            width, height = (int.from_bytes(png[16:20], "big"), int.from_bytes(png[20:24], "big")) if png[:8] == b"\x89PNG\r\n\x1a\n" else (0, 0)
            data: dict[str, Any] = {"width": width, "height": height, "bytes": len(png), "stored": False}
            if mode == "disk":
                self.config.screenshot_dir.mkdir(parents=True, exist_ok=True)
                path = self.config.screenshot_dir / f"screenshot-{datetime.now(timezone.utc):%Y%m%d-%H%M%S}.png"
                path.write_bytes(png)
                data.update(path=str(path), stored=True)
            return BrowserResult(True, "take_screenshot", "", public_url(page.url), len(png) > 0, message=f"Took a {width}x{height} screenshot" + (" and saved it." if mode == "disk" else " (not kept)."), data=data)

        if mode == "off":
            return self._reject("take_screenshot", "", "Screenshots are turned off.")
        return self._run("take_screenshot", "", run, safe=True)

    def upload_file(self, target: Target, filename: str) -> BrowserResult:
        """Upload one file from the approved upload folder. Needs a spoken confirmation upstream (browser.tools); the page cannot ask for it."""
        name = safe_filename(filename)
        path = (self.config.upload_dir / name)
        if name != filename or not path.is_file() or path.suffix.lower() in UPLOAD_BLOCKED or path.stat().st_size > MAX_UPLOAD_BYTES:
            return self._reject("upload_file", filename, "That file isn't in the approved uploads folder, or isn't a file I will upload.")
        return self._run("upload_file", name, lambda: self._upload(target, path), safe=False)

    def _upload(self, target: Target, path: Path) -> BrowserResult:
        page = self._page()
        names = page.set_files(target, str(path))
        ok = names == [path.name]
        return BrowserResult(ok, "upload_file", path.name, public_url(page.url), ok, message=f"Attached {path.name}. It is not submitted yet." if ok else "",
                             error="" if ok else "The page did not accept the file.")

    # ---- media (used by browser.youtube) -----------------------------------------------------------------------------------------------

    def media(self, command: str, value: float = 0.0, *, expect: Callable[[MediaState], bool] | None = None, wait_s: float = 4.0) -> tuple[bool, MediaState | None]:
        """Issue a media command and poll until `expect(state)` holds. Returns (verified, last state)."""
        holder: dict[str, Any] = {}

        def run() -> BrowserResult:
            page = self._page()
            if command != "state" and not page.media_command(command, value):
                holder["state"] = page.media_state()
                return BrowserResult(False, "media", command, "", False, error="There is no video on this page.")
            end = self._clock() + wait_s
            state = page.media_state()
            while expect is not None and not expect(state) and self._clock() < end:
                self._wait(0.25)
                state = page.media_state()
            holder["state"] = state
            ok = expect(state) if expect is not None else state.present
            return BrowserResult(ok, "media", command, public_url(page.url), ok)

        result = self._run(f"media_{command}", command, run, safe=command == "state", record=command != "state")
        return result.success, holder.get("state")

    def page_url(self) -> str:
        """The active page's full address, for internal comparisons only (it can hold query strings: never show or log it)."""
        holder: dict[str, str] = {}

        def run() -> BrowserResult:
            holder["url"] = self._page().url
            return BrowserResult(True, "page_url", "", "", True)

        result = self._run("page_url", "", run, safe=True, timeout_s=15, record=False)
        return holder.get("url", "") if result.success else ""

    def peek(self, target: Target) -> list[ElementInfo]:
        """The visible elements that match `target`, without touching them (used to classify a click by what is really on the page)."""
        holder: dict[str, Any] = {}

        def run() -> BrowserResult:
            holder["rows"] = self._page().match(target)
            return BrowserResult(True, "peek", target.describe(), "", True)

        result = self._run("peek", target.describe(), run, safe=True, timeout_s=20, record=False)
        return holder.get("rows", []) if result.success else []

    def run_on_page(self, action: str, fn: Callable[[PageDriver], BrowserResult], *, safe: bool = False) -> BrowserResult:
        """For trusted workflow modules (browser.youtube): run `fn(active_page)` inside the guarded runner."""
        return self._run(action, "", lambda: fn(self._page()), safe=safe)

    def extract(self, name: str, *, safe: bool = True) -> list[dict[str, Any]] | None:
        holder: dict[str, Any] = {}

        def run() -> BrowserResult:
            holder["rows"] = self._page().extract(name)
            return BrowserResult(True, f"extract_{name}", "", "", True)

        result = self._run(f"extract_{name}", name, run, safe=safe, record=False)
        return holder.get("rows") if result.success else None


def rank_elements(description: str, elements: list[ElementInfo]) -> list[tuple[float, ElementInfo]]:
    """Score visible controls against a spoken description: exact name > all words present > word overlap. A role word in the description
    ("button", "link", "field") narrows to that role. Deterministic; no model."""
    want = _tokens(description)
    text = description.lower()
    role_hint = "button" if "button" in text else "link" if "link" in text else "textbox" if any(w in text for w in ("field", "box", "input")) else ""
    scored = []
    for e in elements:
        if role_hint and e.role != role_hint and not (role_hint == "button" and e.tag == "button"):
            continue
        name = e.name.lower()
        have = _tokens(name)
        if not want or not have:
            continue
        overlap = len(want & have) / len(want)
        score = overlap
        if name == description.strip().lower() or name == " ".join(sorted(want, key=text.find)):
            score = 1.0
        elif want <= have:
            score = 0.9 - 0.02 * (len(have) - len(want))
        if score >= 0.5:
            scored.append((score, e))
    return sorted(scored, key=lambda p: -p[0])
