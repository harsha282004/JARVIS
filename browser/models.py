"""Data model of the browser agent: lifecycle states, targets, page snapshots, the normalized action result."""

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any
from urllib.parse import urlsplit


class BrowserState(StrEnum):
    CLOSED = "closed"
    OPENING = "opening"
    READY = "ready"
    NAVIGATING = "navigating"
    WAITING = "waiting"
    ACTION = "action"
    ERROR = "error"
    RECOVERING = "recovering"
    CLOSING = "closing"


class Category(StrEnum):
    """Permission categories. NAVIGATION/READ/INTERACTION are LOW risk (allowed by policy); EXTERNAL/SENSITIVE always need a spoken yes."""

    NAVIGATION = "browser_navigation"
    READ = "browser_read"
    INTERACTION = "browser_interaction"
    EXTERNAL_ACTION = "browser_external_action"
    SENSITIVE_ACTION = "browser_sensitive_action"


class BrowserError(Exception):
    """Base class. Messages are safe to say aloud and never contain page text."""


class BrowserCrashed(BrowserError):
    """The browser, the context or the page went away."""


class BrowserUnavailable(BrowserError):
    """No browser could be started (not installed, launch failed)."""


class ActionTimeout(BrowserError):
    pass


class ActionCancelled(BrowserError):
    pass


@dataclass(frozen=True)
class Target:
    """How to find an element. Semantic first (role + accessible name, visible text, label, placeholder); `css` is set only by trusted
    code (never accepted from the model or the user); coordinates are deliberately not supported."""

    role: str | None = None
    name: str | None = None
    text: str | None = None
    label: str | None = None
    placeholder: str | None = None
    css: str | None = None
    index: int | None = None  # which of several equal matches (0-based), chosen only after the user disambiguated

    def describe(self) -> str:
        for value in (self.name, self.text, self.label, self.placeholder):
            if value:
                return f"{value} {self.role}".strip() if self.role else value
        return self.role or self.css or "element"


@dataclass(frozen=True)
class ElementInfo:
    """One visible interactive element as read from the page (no values: passwords and typed text are never captured)."""

    role: str
    name: str
    tag: str = ""
    input_type: str = ""
    href: str = ""
    visible: bool = True
    enabled: bool = True


@dataclass
class MediaState:
    present: bool = False
    paused: bool = True
    current_time: float = 0.0
    duration: float = 0.0
    ended: bool = False
    volume: float = 1.0
    muted: bool = False
    ad: bool = False
    skippable_ad: bool = False


@dataclass
class PageSnapshot:
    """A bounded, structured reading of the current page. Every text field is untrusted data written by a website."""

    url: str = ""
    title: str = ""
    headings: list[str] = field(default_factory=list)
    links: list[ElementInfo] = field(default_factory=list)
    buttons: list[ElementInfo] = field(default_factory=list)
    fields: list[ElementInfo] = field(default_factory=list)  # form controls: type + label only, never values
    tables: list[list[list[str]]] = field(default_factory=list)
    text: str = ""
    has_password_field: bool = False
    has_dialog: bool = False
    captcha: bool = False
    loading: bool = False
    scroll_y: int = 0
    scroll_max: int = 0

    def signature(self) -> str:
        """Cheap fingerprint used to tell whether an action changed the page."""
        import hashlib

        raw = "|".join([self.url, self.title, str(len(self.links)), str(len(self.buttons)), str(self.has_dialog), self.text[:600]])
        return hashlib.sha1(raw.encode("utf-8", "replace")).hexdigest()  # noqa: S324 - not security relevant


@dataclass
class DownloadRecord:
    filename: str
    source_url: str
    saved_path: str | None
    timestamp: str
    size: int = 0
    sha256: str = ""
    blocked: bool = False
    reason: str = ""


@dataclass
class TabInfo:
    tab_id: str
    url: str
    title: str
    active: bool


@dataclass
class BrowserResult:
    """The only thing the agent ever sees of a browser action: no browser objects, no DOM."""

    success: bool
    action: str
    target: str = ""
    url: str = ""
    verified: bool = False
    message: str = ""
    error: str = ""
    data: dict[str, Any] = field(default_factory=dict)
    needs_confirmation: bool = False
    category: str = ""
    duration_ms: float = 0.0
    retried: int = 0
    recovered: bool = False
    untrusted: bool = False  # data carries text from a web page

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"success": self.success, "action": self.action, "target": self.target, "url": self.url, "verified": self.verified}
        if self.success:
            out["message"] = self.message
        else:
            out["error"] = self.error or self.message
        if self.data:
            out["data"] = self.data
        for name in ("needs_confirmation", "recovered", "untrusted"):
            if getattr(self, name):
                out[name] = True
        if self.category:
            out["category"] = self.category
        return out


def public_url(url: str) -> str:
    """A URL safe to show or log: no user info, no query string, no fragment (they can carry tokens)."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return ""
    if not parts.scheme or not parts.hostname:
        return url[:40] if url.startswith("about:") else ""
    port = f":{parts.port}" if parts.port else ""
    return f"{parts.scheme}://{parts.hostname}{port}{parts.path}"[:200]
