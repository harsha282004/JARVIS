"""The browser tool registry and router: the ONLY way anything asks the browser to do something.

    request -> schema validation (pydantic, extra keys forbidden) -> risk category -> PermissionManager
            -> [EXTERNAL / SENSITIVE: needs the user's spoken yes, never given by the caller] -> BrowserEngine -> normalized BrowserResult

Categories (each is a registered tool with the PermissionManager, so policy decides, not this module):
  BROWSER_NAVIGATION, BROWSER_READ, BROWSER_INTERACTION   LOW risk, allowed by policy (open a public site, search, scroll, read, play/pause)
  BROWSER_EXTERNAL_ACTION                                 MEDIUM: submit / send / post / publish / apply / register ... needs a yes
  BROWSER_SENSITIVE_ACTION                                HIGH: buy / pay / delete / change a password or security setting / grant access / upload ...

The category of a click or a typed submit is decided from the words of the target AND from the names of the elements actually on the
page that match it, so a button that says "Pay now" cannot be clicked under the name "Continue". There is deliberately no tool that runs
JavaScript, a shell command or a program, reads a cookie, a password or a file, or takes an arbitrary path: schemas forbid extra keys.
"""

import hashlib
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from agent.tools.base import EXECUTE_ACTION, ToolDescriptor
from backend.core.logging import get_logger
from backend.core.security import PermissionManager, PermissionScope, PermissionStatus, RiskLevel, ToolSecurityInfo, check_authorization
from browser.engine import BrowserEngine
from browser.models import BrowserResult, Category, Target
from browser.urlsafe import validate_url

logger = get_logger(__name__)

Role = Literal["button", "link", "textbox", "searchbox", "checkbox", "radio", "tab", "menuitem", "combobox", "option", "switch", "heading", "img"]
_CONTROL = re.compile(r"[\x00-\x1f\x7f<>]")

CATEGORY_SECURITY: dict[Category, ToolSecurityInfo] = {
    Category.NAVIGATION: ToolSecurityInfo(Category.NAVIGATION.value, False, RiskLevel.LOW, (PermissionScope.ONE_TIME,)),
    Category.READ: ToolSecurityInfo(Category.READ.value, False, RiskLevel.LOW, (PermissionScope.ONE_TIME,)),
    Category.INTERACTION: ToolSecurityInfo(Category.INTERACTION.value, False, RiskLevel.LOW, (PermissionScope.ONE_TIME,)),
    Category.EXTERNAL_ACTION: ToolSecurityInfo(Category.EXTERNAL_ACTION.value, True, RiskLevel.MEDIUM, (PermissionScope.ONE_TIME,)),
    Category.SENSITIVE_ACTION: ToolSecurityInfo(Category.SENSITIVE_ACTION.value, True, RiskLevel.HIGH, (PermissionScope.ONE_TIME,)),
}

# Words that make a control externally visible or consequential. Matched as whole words in the target and in the matched element names.
_SENSITIVE = re.compile(r"\b(buy|purchase|pay|payment|checkout|check out|place (?:your )?order|order now|confirm order|donate|transfer|withdraw|delete|erase|remove account|"
                        r"close account|deactivate|terminate|unsubscribe|change password|reset password|security|two[- ]factor|2fa|revoke|authori[sz]e|grant|allow access|"
                        r"api key|access token|make public|transfer ownership)\b", re.I)
_EXTERNAL = re.compile(r"\b(submit|send|post|publish|reply|comment|share|tweet|apply|register|book|reserve|subscribe|follow|sign up|create account|save changes|"
                       r"update profile|confirm|accept|agree|invite|message|email|upload|schedule|merge|commit|create (?:issue|pull request|repository))\b", re.I)
_SEARCHY = re.compile(r"\b(search|find|query|filter|look ?up)\b", re.I)


# ---- argument schemas -------------------------------------------------------------------------------------------------------------

class _Args(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    @field_validator("*", mode="before")
    @classmethod
    def _no_control_chars(cls, value: Any) -> Any:
        if isinstance(value, str) and _CONTROL.search(value):
            raise ValueError("control characters are not allowed")
        return value


class NoArgs(_Args):
    pass


class OpenUrlArgs(_Args):
    url: str = Field(min_length=1, max_length=2048)
    new_tab: bool = False


class OpenNewTabArgs(_Args):
    url: str | None = Field(default=None, max_length=2048)


class TabArgs(_Args):
    tab_id: str = Field(pattern=r"^t\d{1,4}$")


class CloseTabArgs(_Args):
    tab_id: str | None = Field(default=None, pattern=r"^t\d{1,4}$")


class TextArgs(_Args):
    text: str = Field(min_length=1, max_length=200)


class DescribeArgs(_Args):
    description: str = Field(min_length=1, max_length=120)


class TargetArgs(_Args):
    role: Role | None = None
    name: str | None = Field(default=None, max_length=120)
    text: str | None = Field(default=None, max_length=120)
    index: int | None = Field(default=None, ge=0, le=20)

    def target(self) -> Target:
        if not (self.name or self.text):
            raise ValueError("give the element's name or visible text")
        return Target(role=self.role, name=self.name, text=self.text, index=self.index)


class ClickArgs(TargetArgs):
    pass


class TypeArgs(_Args):
    role: Role | None = None
    name: str | None = Field(default=None, max_length=120)
    label: str | None = Field(default=None, max_length=120)
    placeholder: str | None = Field(default=None, max_length=120)
    text: str = Field(min_length=1, max_length=500)
    submit: bool = False

    def target(self) -> Target:
        if not (self.name or self.label or self.placeholder or self.role):
            raise ValueError("say which field")
        return Target(role=self.role, name=self.name, label=self.label, placeholder=self.placeholder)


class KeyArgs(_Args):
    key: str = Field(min_length=1, max_length=12)


class ScrollArgs(_Args):
    direction: Literal["up", "down", "top", "bottom"] = "down"
    amount: int = Field(default=600, ge=50, le=5000)


class WaitArgs(TargetArgs):
    timeout_seconds: float = Field(default=10.0, gt=0, le=30)


class UploadArgs(_Args):
    label: str | None = Field(default=None, max_length=120)
    filename: str = Field(min_length=1, max_length=120, pattern=r"^[\w][\w .\-]{0,118}$")  # a bare file name: no path separators


class QueryArgs(_Args):
    query: str = Field(min_length=1, max_length=200)


class PlayArgs(_Args):
    query: str | None = Field(default=None, max_length=200)
    choice: int | None = Field(default=None, ge=1, le=15)  # "the second one" after an ambiguous search
    official: bool = False


class SeekArgs(_Args):
    seconds: int = Field(ge=-600, le=600)


class VolumeArgs(_Args):
    percent: int = Field(ge=0, le=100)


# ---- registry ----------------------------------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    args: type[_Args]
    category: Category
    timeout_s: float
    idempotent: bool          # safe to retry after a timeout/crash
    handler: Callable[["BrowserTools", _Args], BrowserResult]


def _t(e: BrowserEngine, a: Any) -> Target:
    return a.target()


SPECS: dict[str, ToolSpec] = {}


def _register(name: str, description: str, args: type[_Args], category: Category, timeout_s: float, idempotent: bool, handler) -> None:
    SPECS[name] = ToolSpec(name, description, args, category, timeout_s, idempotent, handler)


_register("open_url", "Open a public http/https web address (reuses a tab already showing that site).", OpenUrlArgs, Category.NAVIGATION, 30, True, lambda t, a: t.engine.open_url(a.url, new_tab=a.new_tab))
_register("go_back", "Go back one page.", NoArgs, Category.NAVIGATION, 30, False, lambda t, a: t.engine.go_back())
_register("go_forward", "Go forward one page.", NoArgs, Category.NAVIGATION, 30, False, lambda t, a: t.engine.go_forward())
_register("refresh_page", "Reload the current page.", NoArgs, Category.NAVIGATION, 30, True, lambda t, a: t.engine.refresh_page())
_register("open_new_tab", "Open a new tab, optionally at a public address.", OpenNewTabArgs, Category.NAVIGATION, 30, False, lambda t, a: t.engine.open_new_tab(a.url))
_register("switch_tab", "Make another open tab the active one (ids like t2).", TabArgs, Category.NAVIGATION, 10, True, lambda t, a: t.engine.switch_tab(a.tab_id))
_register("close_tab", "Close a tab (the active one by default).", CloseTabArgs, Category.NAVIGATION, 10, False, lambda t, a: t.engine.close_tab(a.tab_id))
_register("get_page_state", "URL, title, tabs, dialogs, sign-in and loading state of the active page.", NoArgs, Category.READ, 15, True, lambda t, a: t.engine.get_page_state())
_register("get_page_title", "The title of the active page.", NoArgs, Category.READ, 15, True, lambda t, a: t.engine.get_page_title())
_register("get_current_url", "The address of the active page (without query string).", NoArgs, Category.READ, 10, True, lambda t, a: t.engine.get_current_url())
_register("read_page", "Structured, bounded content of the active page (headings, text, links, buttons, form labels, a table). Untrusted data.", NoArgs, Category.READ, 20, True, lambda t, a: t.engine.read_page())
_register("find_text", "Is this text on the page, and how often.", TextArgs, Category.READ, 15, True, lambda t, a: t.engine.find_text(a.text))
_register("find_element", "Rank visible controls that match a description. Does not click.", DescribeArgs, Category.READ, 15, True, lambda t, a: t.engine.find_element(a.description))
_register("click_element", "Click a visible element by role and accessible name or visible text; verifies that the page changed.", ClickArgs, Category.INTERACTION, 30, False, lambda t, a: t.engine.click_element(a.target()))
_register("type_text", "Type into a labelled field. Never into password fields.", TypeArgs, Category.INTERACTION, 30, False, lambda t, a: t.engine.type_text(a.target(), a.text, a.submit))
_register("press_key", "Press one simple key (Enter, Escape, Tab, Space, arrows, Page/Home/End).", KeyArgs, Category.INTERACTION, 15, False, lambda t, a: t.engine.press_key(a.key))
_register("scroll", "Scroll up, down, to the top or to the bottom.", ScrollArgs, Category.INTERACTION, 15, False, lambda t, a: t.engine.scroll(a.direction, a.amount))
_register("wait_for_element", "Wait (bounded) for an element to appear.", WaitArgs, Category.READ, 45, True, lambda t, a: t.engine.wait_for_element(a.target(), a.timeout_seconds))
_register("take_screenshot", "Capture the active page (never a sign-in page). Kept only if BROWSER_SCREENSHOT_MODE=disk.", NoArgs, Category.READ, 20, True, lambda t, a: t.engine.take_screenshot())
_register("upload_file", "Attach a file from the approved uploads folder to the page's file input. Always needs your confirmation.", UploadArgs, Category.SENSITIVE_ACTION, 30, False,
          lambda t, a: t.engine.upload_file(Target(label=a.label), a.filename))
_register("close_browser", "Close the browser.", NoArgs, Category.NAVIGATION, 30, False, lambda t, a: t.engine.close_browser())
_register("web_search", "Search the web (result titles, addresses and snippets; nothing is opened).", QueryArgs, Category.NAVIGATION, 45, True, lambda t, a: t.web_search(a.query))
_register("open_youtube", "Open YouTube.", NoArgs, Category.NAVIGATION, 45, True, lambda t, a: t.youtube.open())
_register("search_youtube", "Search YouTube and list the results.", QueryArgs, Category.NAVIGATION, 45, True, lambda t, a: t.youtube.search(a.query))
_register("play_youtube", "Play a YouTube video: picks a result only when it is unambiguous, otherwise asks.", PlayArgs, Category.INTERACTION, 60, False, lambda t, a: t.youtube.play(a.query, a.choice, a.official))
_register("pause_youtube", "Pause the video.", NoArgs, Category.INTERACTION, 20, True, lambda t, a: t.youtube.pause())
_register("resume_youtube", "Resume the video.", NoArgs, Category.INTERACTION, 20, True, lambda t, a: t.youtube.resume())
_register("skip_youtube", "Skip the ad, or go to the next video.", NoArgs, Category.INTERACTION, 30, False, lambda t, a: t.youtube.skip())
_register("seek_youtube", "Jump forward or back by some seconds.", SeekArgs, Category.INTERACTION, 20, False, lambda t, a: t.youtube.seek(a.seconds))
_register("volume_youtube", "Set the video volume (0-100).", VolumeArgs, Category.INTERACTION, 20, True, lambda t, a: t.youtube.volume(a.percent))
_register("close_youtube", "Close the YouTube tab(s).", NoArgs, Category.NAVIGATION, 20, False, lambda t, a: t.youtube.close())

FORBIDDEN_CAPABILITIES = ("execute_shell", "run_powershell", "run_command", "evaluate_javascript", "run_script", "read_cookies", "read_password", "delete_file", "read_file")


def unwrap_redirect(url: str) -> str:
    """Bing wraps result links in a tracking redirect (bing.com/ck/a?...&u=a1<base64 of the real address>): recover the real address."""
    import base64
    from urllib.parse import parse_qs, urlsplit

    try:
        parts = urlsplit(url)
        if (parts.hostname or "").endswith("bing.com") and parts.path.startswith("/ck/"):
            token = parse_qs(parts.query).get("u", [""])[0]
            if token.startswith("a1"):
                raw = token[2:]
                return base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)).decode("utf-8", "replace")
    except Exception:  # noqa: BLE001 - keep the original if it cannot be decoded
        pass
    return url


def classify_words(text: str, role: str | None = None) -> Category | None:
    """The category implied by words, or None when they are unremarkable. Links only escalate on sensitive words (a link navigates)."""
    if _SENSITIVE.search(text):
        return Category.SENSITIVE_ACTION
    if role != "link" and _EXTERNAL.search(text):
        return Category.EXTERNAL_ACTION
    return None


def _order(c: Category) -> int:
    return [Category.NAVIGATION, Category.READ, Category.INTERACTION, Category.EXTERNAL_ACTION, Category.SENSITIVE_ACTION].index(c)


class BrowserTools:
    def __init__(self, engine: BrowserEngine, permissions: PermissionManager | None = None, *, youtube=None):
        self.engine = engine
        self.permissions = permissions or PermissionManager(tools=CATEGORY_SECURITY.values())
        for info in CATEGORY_SECURITY.values():
            self.permissions.register_tool(info)
        if youtube is None:
            from browser.youtube import YouTube

            youtube = YouTube(engine)
        self.youtube = youtube

    # ---- description ----------------------------------------------------------------------------------------------------------------

    def descriptors(self) -> list[ToolDescriptor]:
        return [ToolDescriptor(name=s.name, description=s.description, input_schema=s.args.model_json_schema().get("properties", {}),
                               requires_permission=CATEGORY_SECURITY[s.category].requires_permission, risk=CATEGORY_SECURITY[s.category].risk)
                for s in SPECS.values()]

    # ---- helpers used by the workflows -------------------------------------------------------------------------------------------------

    def web_search(self, query: str) -> BrowserResult:
        from urllib.parse import quote_plus

        url = self.engine.config.search_url.format(query=quote_plus(query))
        opened = self.engine.open_url(url)
        if not opened.success:
            return opened
        rows: list = []
        for _ in range(6):  # results render just after the page loads
            rows = self.engine.extract("web_results") or []
            if rows:
                break
            self.engine._sleep(0.7)
        from backend.core.security.trust import sanitize_external, scan_for_injection

        if not rows:
            state = self.engine.get_page_state()
            if state.success and state.data.get("captcha"):
                return BrowserResult(False, "web_search", query[:60], opened.url, False, error="The search engine is asking for a human check, which only you can complete. I won't try to get past it.",
                                     data={"captcha": True}, untrusted=True)

        results = []
        for r in rows or []:
            r["url"] = unwrap_redirect(r.get("url", ""))
            decision = validate_url(r.get("url", ""), allow_private=self.engine.config.allow_private_hosts, resolver=self.engine.config.resolver)
            if not decision.ok:
                continue  # a result pointing somewhere forbidden is dropped, not offered
            results.append({"title": sanitize_external(r.get("title", ""), 120), "url": decision.url.split("?")[0], "snippet": sanitize_external(r.get("snippet", ""), 200),
                            "suspicious": decision.suspicious or None, "injection_suspected": scan_for_injection(r.get("snippet", "") + " " + r.get("title", "")).flagged})
        ok = bool(results)
        return BrowserResult(ok, "web_search", query[:60], opened.url, ok, message=f"Found {len(results)} results for {query[:40]}." if ok else "",
                             error="" if ok else "The search returned no results I could read.", data={"results": results[:8], "query": query[:100]}, untrusted=True)

    # ---- classification -------------------------------------------------------------------------------------------------------------------

    def category_of(self, name: str, args: _Args) -> Category:
        spec = SPECS[name]
        category = spec.category
        if name in ("click_element", "type_text"):
            words = " ".join(str(v) for v in (getattr(args, "name", None), getattr(args, "text", None) if name == "click_element" else None,
                                              getattr(args, "label", None), getattr(args, "placeholder", None)) if v)
            role = getattr(args, "role", None)
            found = classify_words(words, role)
            if name == "type_text" and getattr(args, "submit", False) and not _SEARCHY.search(words):
                found = found if found is not None and _order(found) > _order(Category.EXTERNAL_ACTION) else Category.EXTERNAL_ACTION
            if found is not None and _order(found) > _order(category):
                category = found
            if name == "click_element":  # what is really on the page under that description
                try:
                    for el in self.engine.peek(args.target()):
                        deeper = classify_words(el.name, el.role if el.role else role)
                        if deeper is not None and _order(deeper) > _order(category):
                            category = deeper
                except Exception:  # noqa: BLE001 - if the page cannot be inspected the click will fail on its own
                    pass
        elif name == "press_key" and getattr(args, "key", "") == "Enter":
            category = Category.EXTERNAL_ACTION  # Enter can submit a form
        return category

    # ---- the boundary ---------------------------------------------------------------------------------------------------------------------------

    def call(self, name: str, arguments: dict[str, Any] | None = None, *, session_id: str = "voice", confirmed: bool = False) -> BrowserResult:
        """Validate, classify, authorize and run one tool. Never raises. `confirmed=True` is passed only by the confirmation callback that
        runs AFTER the user's spoken yes (agent/voice code cannot construct it: it is a closure inside `BrowserRouter`)."""
        spec = SPECS.get(name)
        if spec is None:
            return BrowserResult(False, name, error="That isn't a browser action I have.")
        try:
            args = spec.args(**(arguments or {}))
            if hasattr(args, "target") and name in ("click_element", "wait_for_element"):
                args.target()
            elif hasattr(args, "target") and name == "type_text":
                args.target()
        except (ValidationError, ValueError, TypeError):
            return BrowserResult(False, name, error="Those details aren't valid for this browser action.")
        category = self.category_of(name, args)
        params = self._bind_params(name, args)
        request = self.permissions.request_permission(tool_name=category.value, action=EXECUTE_ACTION, description=f"Browser action {name}", parameters=params,
                                                      session_id=session_id, requested_by="agent")
        if request.status is PermissionStatus.PENDING:
            if not confirmed:
                return BrowserResult(False, name, self._describe(name, args), needs_confirmation=True, category=category.value,
                                     error="This needs your confirmation.", message=self._confirm_text(name, args, category))
            request = self.permissions.approve(request, actor="user")
        if request.status is not PermissionStatus.APPROVED:
            return BrowserResult(False, name, error="I'm not allowed to do that.", category=category.value)
        check = check_authorization(self.permissions, request.request_id, tool_name=category.value, action=EXECUTE_ACTION, parameters=params, session_id=session_id)
        if not check.allowed:
            return BrowserResult(False, name, error="I'm not allowed to do that.", category=category.value)
        result = spec.handler(self, args)
        result.category = category.value
        return result

    @staticmethod
    def _bind_params(name: str, args: _Args) -> dict[str, Any]:
        """What the permission is bound to. Typed text is bound by length and hash only, so it never sits in an audit record."""
        out: dict[str, Any] = {"tool": name}
        for key, value in args.model_dump().items():
            if key == "text" and name == "type_text":
                out["text_len"] = len(value)
                out["text_sha"] = hashlib.sha256(value.encode()).hexdigest()[:16]
            elif value is not None:
                out[key] = value
        return out

    def _describe(self, name: str, args: _Args) -> str:
        return " ".join(str(v) for v in (getattr(args, "name", None), getattr(args, "text", None) if name != "type_text" else None, getattr(args, "filename", None), getattr(args, "url", None)) if v)[:80]

    def _confirm_text(self, name: str, args: _Args, category: Category) -> str:
        page = self.engine.status().get("url") or "this page"
        what = {"click_element": f"click '{self._describe(name, args)}'", "type_text": f"type into '{getattr(args, 'label', None) or getattr(args, 'name', None) or 'the field'}' and submit it",
                "press_key": "press Enter, which can submit the form", "upload_file": f"upload '{getattr(args, 'filename', '')}'"}.get(name, name.replace("_", " "))
        risk = "This can't easily be undone." if category is Category.SENSITIVE_ACTION else "This will be visible to the website or other people."
        return f"I'm about to {what} on {page}. {risk} Shall I go ahead?"
