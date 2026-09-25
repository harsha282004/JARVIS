"""A deterministic fake web and browser driver for the Phase 20 tests: no network, no real browser, no YouTube uptime.

`FakeWeb` holds pages by URL (title, elements with click behavior, YouTube results, a <video>), plus failure switches (offline, crash, slow
timeout, broken media UI). `FakeBrowserDriver` implements the same `BrowserDriver`/`PageDriver` interfaces as the Playwright driver."""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

from browser.driver import BrowserDriver, DownloadEvent, PageDriver
from browser.engine import BrowserConfig, BrowserEngine
from browser.downloads import BrowserLog
from browser.models import BrowserCrashed, BrowserUnavailable, ElementInfo, MediaState, PageSnapshot, Target


class TimeoutError(Exception):  # noqa: A001 - named like Playwright's so the engine's translation is exercised
    pass


@dataclass
class El:
    role: str
    name: str
    tag: str = "div"
    input_type: str = ""
    href: str = ""
    enabled: bool = True
    action: tuple | None = None   # ("goto", url) | ("popup", url) | ("download", filename, bytes) | ("toggle",) | ("text", "new page text")


@dataclass
class Spec:
    title: str = ""
    text: str = ""
    headings: list[str] = field(default_factory=list)
    elements: list[El] = field(default_factory=list)
    yt_results: list[dict] = field(default_factory=list)
    web_results: list[dict] = field(default_factory=list)
    video: bool = False
    ad: bool = False
    status: int = 200
    redirect: str | None = None
    password: bool = False
    captcha: bool = False
    dialog: bool = False
    scroll_max: int = 3000
    tables: list = field(default_factory=list)


def yt_item(title: str, channel: str, vid: str, badges=(), duration="3:20") -> dict:
    return {"title": title, "href": f"https://www.youtube.com/watch?v={vid}", "channel": channel, "badges": list(badges), "meta": ["1M views"], "duration": duration}


class FakeWeb:
    def __init__(self) -> None:
        self.pages: dict[str, Spec] = {}
        self.offline = False
        self.slow = False
        self.media_broken = False
        self.crash_on_next = False
        self.browser_dead = False
        self.launch_error: Exception | None = None
        self.launches = 0
        self.visits: list[str] = []
        self.clicks: list[str] = []
        self.typed: list[tuple[str, str]] = []
        self.keys: list[str] = []
        self.uploads: list[str] = []
        self.scripts_run: list[str] = []
        self.crash_on_click = False   # the browser dies during the next click itself
        self.fail_goto = 0   # the next N navigations time out (transient network trouble)
        self.on_visit: dict[str, object] = {}   # url -> callable(web), run when that page loads (page changes, blocking, mid-task events)

    def add(self, url: str, spec: Spec) -> None:
        self.pages[url] = spec

    def lookup(self, url: str) -> Spec | None:
        if url in self.pages:
            return self.pages[url]
        parts = urlsplit(url)
        for key, spec in self.pages.items():
            k = urlsplit(key)
            if k.netloc == parts.netloc and k.path == parts.path and (not k.query or k.query == parts.query):
                return spec
        return None


class FakePage(PageDriver):
    def __init__(self, web: FakeWeb, driver: "FakeBrowserDriver") -> None:
        self.web, self.driver = web, driver
        self._url = "about:blank"
        self.spec = Spec()
        self.history: list[str] = []
        self.forward_stack: list[str] = []
        self.crashed = False
        self.closed = False
        self.values: dict[str, str] = {}
        self.y = 0
        self.paused = True
        self.t = 0.0
        self.volume = 1.0
        self.ad_left = 0
        self.downloads: list[DownloadEvent] = []
        self.extra_text = ""

    # -- state -----------------------------------------------------------------------------------------------------------------
    def _alive(self) -> None:
        if self.web.browser_dead or self.driver.dead:
            raise BrowserCrashed("gone")
        if self.crashed:
            raise BrowserCrashed("page crashed")
        if self.web.crash_on_next:
            self.web.crash_on_next = False
            self.web.browser_dead = True
            self.driver.dead = True
            raise BrowserCrashed("gone")

    @property
    def url(self):
        return self._url

    @property
    def title(self):
        return self.spec.title

    @property
    def is_crashed(self):
        return self.crashed or self.closed or self.driver.dead

    def _load(self, url: str) -> int | None:
        spec = self.web.lookup(url)
        if spec is None:
            self._url, self.spec = url, Spec(title="Not found", text="404")
            return 404
        if spec.redirect:
            return self._load(spec.redirect)
        self._url, self.spec, self.y = url, spec, 0
        self.paused = True
        self.t = 0.0
        self.ad_left = 20 if spec.ad else 0
        self.web.visits.append(url)
        hook = self.web.on_visit.get(url)
        if hook is not None:
            hook(self.web)
        return spec.status

    def goto(self, url, timeout_ms):
        self._alive()
        if self.web.fail_goto > 0:
            self.web.fail_goto -= 1
            raise TimeoutError("Timeout exceeded")
        if self.web.offline or self.web.slow:
            raise TimeoutError("Timeout exceeded")
        if url != "about:blank" and self._url != "about:blank":
            self.history.append(self._url)
            self.forward_stack.clear()
        if url == "about:blank":
            self._url, self.spec = url, Spec()
            return 200
        return self._load(url)

    def back(self, timeout_ms):
        self._alive()
        if not self.history:
            return False
        self.forward_stack.append(self._url)
        self._load(self.history.pop())
        return True

    def forward(self, timeout_ms):
        self._alive()
        if not self.forward_stack:
            return False
        self.history.append(self._url)
        self._load(self.forward_stack.pop())
        return True

    def reload(self, timeout_ms):
        self._alive()
        if self.web.offline:
            raise TimeoutError("Timeout exceeded")
        self._load(self._url)

    def wait_ready(self, timeout_ms):
        self._alive()

    def snapshot(self):
        self._alive()
        s = self.spec
        conv = lambda els, role: [ElementInfo(e.role, e.name, e.tag, e.input_type, e.href, True, e.enabled) for e in els if e.role in role]  # noqa: E731
        return PageSnapshot(url=self._url, title=s.title, headings=s.headings, links=conv(s.elements, ("link",)), buttons=conv(s.elements, ("button",)),
                            fields=conv(s.elements, ("textbox", "searchbox", "combobox")), tables=s.tables, text=(s.text + " " + self.extra_text).strip(),
                            has_password_field=s.password, has_dialog=s.dialog, captcha=s.captcha, scroll_y=self.y, scroll_max=s.scroll_max)

    def _matches(self, t: Target) -> list[El]:
        out = []
        for e in self.spec.elements:
            if t.role and e.role != t.role:
                continue
            want = (t.name or t.text or t.label or t.placeholder or "").lower()
            if t.css:
                if t.css.split(",")[0].strip() in e.name or e.name == t.css:
                    out.append(e)
                continue
            if want and want in e.name.lower():
                out.append(e)
        return out

    def match(self, target):
        self._alive()
        return [ElementInfo(e.role, e.name, e.tag, e.input_type, e.href, True, e.enabled) for e in self._matches(target)]

    def click(self, target, timeout_ms):
        self._alive()
        if self.web.crash_on_click:
            self.web.crash_on_click = False
            self.web.browser_dead = True
            self.driver.dead = True
            raise BrowserCrashed("gone")
        found = self._matches(target)
        if not found:
            raise TimeoutError("element not found")
        el = found[target.index or 0]
        self.web.clicks.append(el.name)
        act = el.action
        if act is None:
            return
        if act[0] == "goto":
            self.history.append(self._url)
            self._load(act[1])
        elif act[0] == "popup":
            page = FakePage(self.web, self.driver)
            page._load(act[1])
            self.driver.popups.append(page)
        elif act[0] == "download":
            path_bytes = act[2]
            self.downloads.append(DownloadEvent(act[1], "https://example.com/files/" + act[1] + "?token=secret", lambda p, b=path_bytes: Path(p).write_bytes(b)))
        elif act[0] == "endad":
            self.ad_left = 0
        elif act[0] == "toggle":
            self.extra_text += " toggled"
        elif act[0] == "text":
            self.extra_text += " " + act[1]

    def fill(self, target, text, timeout_ms):
        self._alive()
        found = self._matches(target)
        if not found:
            raise TimeoutError("field not found")
        self.values[found[0].name] = text
        self.web.typed.append((found[0].name, text))
        return True

    def field_type(self, target):
        self._alive()
        found = self._matches(target)
        return found[0].input_type if found else ""

    def press(self, key):
        self._alive()
        self.web.keys.append(key)

    def scroll(self, direction, amount):
        self._alive()
        mx = self.spec.scroll_max
        if direction == "top":
            self.y = 0
        elif direction == "bottom":
            self.y = mx
        elif direction == "down":
            self.y = min(mx, self.y + amount)
        elif direction == "up":
            self.y = max(0, self.y - amount)
        return self.y, mx

    def wait_for(self, target, timeout_ms):
        self._alive()
        return bool(self._matches(target))

    def media_state(self):
        self._alive()
        if not self.spec.video:
            return MediaState(present=False)
        if not self.paused and not self.web.media_broken:
            self.t += 1.0
            if self.ad_left > 0:
                self.ad_left -= 1
        return MediaState(True, self.paused, self.t, 200.0, False, self.volume, False, self.ad_left > 0 and self.spec.ad, self.ad_left > 0 and self.spec.ad)

    def media_command(self, cmd, value=0.0):
        self._alive()
        if not self.spec.video:
            return False
        if self.web.media_broken:
            return True  # the page ignores it: the UI changed
        if cmd == "play":
            self.paused = False
        elif cmd == "pause":
            self.paused = True
        elif cmd == "seek":
            self.t = value
        elif cmd == "volume":
            self.volume = value
        return True

    def extract(self, name):
        self._alive()
        self.web.scripts_run.append(name)
        if name == "youtube_results":
            return list(self.spec.yt_results)
        if name == "web_results":
            return list(self.spec.web_results)
        raise ValueError(name)

    def screenshot(self):
        self._alive()
        return b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\x0d" + b"IHDR" + (1280).to_bytes(4, "big") + (800).to_bytes(4, "big") + b"\x00" * 50

    def set_files(self, target, path):
        self._alive()
        self.web.uploads.append(path)
        return [Path(path).name]

    def pop_downloads(self, wait_ms):
        out, self.downloads = self.downloads, []
        return out

    def bring_to_front(self):
        self._alive()

    def close(self):
        self.closed = True


class FakeBrowserDriver(BrowserDriver):
    def __init__(self, web: FakeWeb) -> None:
        self.web = web
        self.dead = False
        self.popups: list[FakePage] = []
        self.pages: list[FakePage] = []

    def launch(self):
        if self.web.launch_error is not None:
            raise self.web.launch_error
        self.web.launches += 1
        self.web.browser_dead = False

    def new_page(self):
        page = FakePage(self.web, self)
        self.pages.append(page)
        return page

    def take_new_pages(self):
        out, self.popups = self.popups, []
        return out

    @property
    def alive(self):
        return not self.dead and not self.web.browser_dead

    def close(self):
        self.dead = True


def make_engine(web: FakeWeb, tmp_path: Path, **overrides: Any) -> BrowserEngine:
    config = BrowserConfig(download_dir=tmp_path / "downloads", upload_dir=tmp_path / "uploads", screenshot_dir=tmp_path / "shots", resolver=lambda host: ["93.184.216.34"],
                           settle_seconds=0.05, default_timeout_s=1.0, navigation_timeout_s=2.0, retries=2, **overrides)
    virtual = {"t": 0.0}

    def sleep(seconds: float) -> None:  # virtual time: waits cost nothing but still elapse for the engine's deadlines
        virtual["t"] += seconds

    return BrowserEngine(lambda: FakeBrowserDriver(web), config, BrowserLog(tmp_path / "browser_log.jsonl"), sleep=sleep, clock=lambda: virtual["t"])


def standard_web() -> FakeWeb:
    web = FakeWeb()
    web.add("https://www.youtube.com/", Spec("YouTube", "Home", ["Home"], [El("searchbox", "Search", "input", "text")]))
    web.add("https://github.com/", Spec("GitHub", "Where the world builds software", ["Build and ship"], [
        El("link", "Sign in", "a", href="https://github.com/login", action=("goto", "https://github.com/login")),
        El("link", "Pull requests", "a", action=("goto", "https://github.com/pulls")), El("button", "Sign up", "button", action=("goto", "https://github.com/signup"))]))
    web.add("https://github.com/login", Spec("Sign in to GitHub", "Sign in", ["Sign in to GitHub"], [El("textbox", "Username or email address", "input", "text"),
            El("textbox", "Password", "input", "password"), El("button", "Sign in", "button")], password=True))
    web.add("https://github.com/pulls", Spec("Pull requests", "Pull requests", ["Pull requests"]))
    web.add("https://example.com/", Spec("Example Domain", "This domain is for use in illustrative examples.", ["Example Domain"], [
        El("link", "More information", "a", action=("goto", "https://example.com/more")), El("button", "Download report", "button", action=("download", "report.pdf", b"%PDF-1.4 test")),
        El("button", "Do nothing", "button"), El("button", "Buy now", "button", action=("text", "purchased")), El("button", "Open docs", "button", action=("popup", "https://example.com/docs")),
        El("button", "Toggle", "button", action=("toggle",)), El("button", "Run installer", "button", action=("download", "setup.exe", b"MZ")),
        El("textbox", "Search", "input", "text"), El("textbox", "Comment", "textarea", "text"), El("button", "Post comment", "button", action=("text", "posted")),
        El("button", "Disabled", "button", enabled=False), El("button", "Save draft", "button"), El("button", "Save final", "button", action=("toggle",))]))
    web.add("https://example.com/more", Spec("More", "More info", ["More"]))
    web.add("https://example.com/docs", Spec("Docs", "Documentation", ["Docs"]))
    return web
