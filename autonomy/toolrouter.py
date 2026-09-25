"""The autonomy Tool Router: the ONLY way a planned step reaches anything.

    step (tool name + arguments) -> registry lookup (unknown tools are refused) -> argument schema (unknown keys refused)
        -> risk/permission computed by code -> [BrowserTools: PermissionManager, per-category gate] | [integration hub tool: registry gate] | [local analysis]
        -> ToolOutcome

Three families, in the order the planner should prefer them:
    api        the Integration Hub (GitHub repositories, README): specialised, reliable, no browser
    local      deterministic analysis of text already fetched (summaries, technologies, section checks, official-result ranking, the final report)
    browser    every Phase 20 browser tool, through `BrowserTools.call` (which applies schemas, categories, the PermissionManager and confirmations)

There is no shell, no file, no credential and no arbitrary-script tool here or reachable from here (`FORBIDDEN` names are refused outright and
tests scan for them). A planner (or a model proposing a plan) cannot invent a tool: it can only name one that is in this registry.
"""

import re
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from autonomy import analysis
from autonomy.models import Ref, Risk
from backend.core.logging import get_logger
from backend.core.metrics import metrics
from browser.tools import SPECS as BROWSER_SPECS
from browser.tools import BrowserTools, classify_words
from browser.models import Category

logger = get_logger(__name__)

FORBIDDEN = frozenset({"execute_shell", "run_powershell", "run_command", "execute", "exec", "eval", "shell", "cmd", "powershell", "delete_file", "read_file", "write_file",
                       "read_cookies", "read_password", "get_credentials", "evaluate_javascript", "run_script", "install", "download_url", "os_command"})
_DESTRUCTIVE = re.compile(r"\b(delete|erase|remove|deactivate|terminate|close account|wipe|destroy)\b", re.I)
_DOWNLOAD = re.compile(r"\bdownload\b", re.I)
_ROLES = frozenset({"button", "link", "textbox", "searchbox", "checkbox", "radio", "tab", "menuitem", "combobox", "option", "switch", "heading", "img"})
_REPO = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})/[A-Za-z0-9._-]{1,100}$")


class _A(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class QueryA(_A):
    query: str = Field(min_length=1, max_length=120)


class NoA(_A):
    pass


class RepoA(_A):
    repo: str = Field(pattern=r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})/[A-Za-z0-9._-]{1,100}$")


class SummarizeA(_A):
    source: str = Field(min_length=1, max_length=60_000)
    focus: Literal["setup", "overview"] = "setup"
    name: str = Field(default="the repository", max_length=120)


class TechA(_A):
    source: str = Field(min_length=1, max_length=60_000)


class SectionA(_A):
    page: dict[str, Any]
    section: str = Field(min_length=1, max_length=60)
    needle: str = Field(min_length=1, max_length=120)


class OfficialA(_A):
    results: list[dict[str, Any]] = Field(max_length=15)
    query: str = Field(min_length=1, max_length=120)


class ReportA(_A):
    kind: Literal["done", "repo_found", "readme_summary", "technologies", "section_check", "youtube", "official_result", "download", "page"]


@dataclass
class ToolOutcome:
    success: bool
    verified: bool = False
    message: str = ""
    error: str = ""
    data: dict[str, Any] = field(default_factory=dict)
    needs_confirmation: bool = False
    category: str = ""
    recovered: bool = False
    untrusted: bool = False
    url: str = ""
    family: str = ""

    @property
    def ambiguous(self) -> bool:
        return bool(self.data.get("ambiguous"))


@dataclass(frozen=True)
class LocalSpec:
    name: str
    family: str
    args: type[_A]
    description: str


LOCAL_SPECS: dict[str, LocalSpec] = {s.name: s for s in [
    LocalSpec("github_find_repo", "api", QueryA, "Find one of the user's GitHub repositories by name words (GitHub integration)"),
    LocalSpec("github_latest_repo", "api", NoA, "The user's most recently pushed repository (GitHub integration)"),
    LocalSpec("github_read_readme", "api", RepoA, "Read a repository's README through the GitHub integration (untrusted text)"),
    LocalSpec("summarize_readme", "local", SummarizeA, "Summarize README sections about setup or overview (deterministic)"),
    LocalSpec("find_technologies", "local", TechA, "Technologies mentioned in a README (deterministic)"),
    LocalSpec("check_page_section", "local", SectionA, "Does a page have a section heading, and mention a name (from read_page data)"),
    LocalSpec("pick_official_result", "local", OfficialA, "Rank web search results by how likely they are the official documentation"),
    LocalSpec("compose_report", "local", ReportA, "Compose the final answer from what the task learned"),
]}


class ToolRouter:
    def __init__(self, browser: BrowserTools | None, hub=None):
        self.browser = browser
        self.hub = hub
        self.allow_private = bool(browser is not None and browser.engine.config.allow_private_hosts)  # follows BROWSER_ALLOW_PRIVATE_HOSTS

    # ---- registry ---------------------------------------------------------------------------------------------------------------

    def names(self) -> set[str]:
        return set(BROWSER_SPECS) | set(LOCAL_SPECS)

    def family(self, tool: str) -> str | None:
        if tool in LOCAL_SPECS:
            return LOCAL_SPECS[tool].family
        return "browser" if tool in BROWSER_SPECS else None

    def available(self, tool: str) -> tuple[bool, str]:
        fam = self.family(tool)
        if fam is None or tool.lower() in FORBIDDEN:
            return False, "That isn't an action I have."
        if fam == "browser" and self.browser is None:
            return False, "The browser agent is turned off."
        if fam == "api":
            if self.hub is None:
                return False, "The GitHub integration isn't available."
            ok, reason = self.hub.registry.allowed("github", _perm())
            if not ok:
                return False, reason
        return True, ""

    def schema(self, tool: str):
        return LOCAL_SPECS[tool].args if tool in LOCAL_SPECS else BROWSER_SPECS[tool].args

    # ---- validation (before anything runs) -----------------------------------------------------------------------------------------

    def validate(self, tool: str, arguments: dict[str, Any]) -> str | None:
        """None if the arguments are acceptable for the tool's schema; else a reason. `Ref`s are checked against a typed placeholder."""
        if tool.lower() in FORBIDDEN or tool not in self.names():
            return "That isn't an action I have."
        concrete = {k: _placeholder(v) if isinstance(v, Ref) else v for k, v in arguments.items()}
        try:
            args = self.schema(tool)(**concrete)
            if tool in BROWSER_SPECS and hasattr(args, "target") and tool in ("click_element", "wait_for_element", "type_text"):
                args.target()
        except (ValidationError, ValueError, TypeError) as exc:
            return f"Invalid arguments for {tool}: {type(exc).__name__}."
        return None

    # ---- risk and permission (computed by code, never taken from a plan) -----------------------------------------------------------

    def risk_of(self, tool: str, arguments: dict[str, Any], description: str = "") -> tuple[Risk, str]:
        if tool in LOCAL_SPECS:
            return Risk.READ_ONLY, "local_analysis" if LOCAL_SPECS[tool].family == "local" else "integration_read"
        spec = BROWSER_SPECS[tool]
        cat = spec.category
        risk = {Category.NAVIGATION: Risk.READ_ONLY, Category.READ: Risk.READ_ONLY, Category.INTERACTION: Risk.LOW_RISK,
                Category.EXTERNAL_ACTION: Risk.EXTERNAL_EFFECT, Category.SENSITIVE_ACTION: Risk.SENSITIVE}[cat]
        words = " ".join(str(v) for k, v in arguments.items() if isinstance(v, str) and k in ("name", "text", "label", "placeholder", "description", "filename")) if tool != "type_text" else \
            " ".join(str(arguments.get(k, "")) for k in ("name", "label", "placeholder"))
        if tool == "click_element":
            words = f"{words} {description}"  # the plan's own wording counts too: a Ref-filled name is unknown until the page has been read
        if tool in ("click_element", "type_text", "find_element"):
            found = classify_words(words, arguments.get("role") if isinstance(arguments.get("role"), str) else None)
            if found is Category.SENSITIVE_ACTION:
                risk, cat = max(risk, Risk.SENSITIVE), found
            elif found is Category.EXTERNAL_ACTION:
                risk, cat = max(risk, Risk.EXTERNAL_EFFECT), found
            if tool == "click_element":
                if _DESTRUCTIVE.search(words):
                    risk = Risk.DESTRUCTIVE
                elif _DOWNLOAD.search(words):
                    risk = max(risk, Risk.EXTERNAL_EFFECT)  # a download writes a file to disk
            if tool == "type_text" and arguments.get("submit") and risk < Risk.EXTERNAL_EFFECT and not re.search(r"\b(search|find|query|filter)\b", words, re.I):
                risk = Risk.EXTERNAL_EFFECT
        if tool == "press_key" and arguments.get("key") == "Enter":
            risk = max(risk, Risk.EXTERNAL_EFFECT)
        if tool == "upload_file":
            risk = max(risk, Risk.SENSITIVE)
        return risk, cat.value

    # ---- execution ------------------------------------------------------------------------------------------------------------------------

    def call(self, tool: str, arguments: dict[str, Any], *, session_id: str, blackboard: dict[str, Any] | None = None, confirmed: bool = False) -> ToolOutcome:
        """Run one validated step. Never raises."""
        ok, reason = self.available(tool)
        if not ok:
            return ToolOutcome(False, error=reason)
        try:
            with metrics.timer("autonomy.action_ms"):
                if tool in BROWSER_SPECS:
                    return self._browser(tool, arguments, session_id, confirmed)
                return self._local(tool, arguments, blackboard or {})
        except Exception as exc:  # noqa: BLE001 - the loop must survive any tool failure and report it truthfully
            logger.error("Autonomy tool %s failed (%s)", tool, type(exc).__name__)
            return ToolOutcome(False, error=f"That step failed ({type(exc).__name__}).")

    def _browser(self, tool: str, arguments: dict[str, Any], session_id: str, confirmed: bool) -> ToolOutcome:
        r = self.browser.call(tool, arguments, session_id=session_id, confirmed=confirmed)
        return ToolOutcome(r.success, r.verified, r.message, r.error, dict(r.data), r.needs_confirmation, r.category, r.recovered, r.untrusted, r.url, "browser")

    # ---- api tools (Integration Hub) -----------------------------------------------------------------------------------------------

    def _local(self, tool: str, a: dict[str, Any], board: dict[str, Any]) -> ToolOutcome:
        args = LOCAL_SPECS[tool].args(**a)
        if tool == "github_find_repo":
            return self._find_repo(args.query)
        if tool == "github_latest_repo":
            r = self.hub.tools.call("search_github", {"limit": 5})
            if not r.success:
                return ToolOutcome(False, error=r.error["message"] if r.error else "GitHub isn't reachable.", family="api")
            if not r.data:
                return ToolOutcome(False, error="I couldn't find any repositories on your GitHub account.", family="api")
            top = r.data[0]
            return ToolOutcome(True, True, f"Your latest repository is {top['source_id']}.", data={"repo": top["source_id"], "description": _safe_desc(top.get("summary", ""))}, untrusted=True, family="api")
        if tool == "github_read_readme":
            r = self.hub.tools.call("read_repository_readme", {"repo": args.repo})
            if not r.success:
                return ToolOutcome(False, error=r.error["message"] if r.error else "I couldn't read that README.", family="api")
            d = r.data
            return ToolOutcome(True, bool(d["readme_untrusted"]), f"Read the README for {args.repo}.", data={"readme": d["readme_untrusted"], "injection_suspected": d["injection_suspected"]},
                               untrusted=True, family="api")
        if tool == "summarize_readme":
            data = analysis.summarize_readme(args.source, args.focus)
            text = analysis.format_summary(args.name, data, args.focus)
            return ToolOutcome(True, not data["empty"], text, data={"summary": text, **data}, untrusted=True, family="local")
        if tool == "find_technologies":
            techs = analysis.find_technologies(args.source)
            text = ("It uses " + ", ".join(techs) + ".") if techs else "I couldn't identify any specific technologies in it."
            return ToolOutcome(bool(techs), bool(techs), text, error="" if techs else text, data={"technologies": techs, "summary": text}, untrusted=True, family="local")
        if tool == "check_page_section":
            res = analysis.check_page_section(args.page, args.section, args.needle)
            if res["mentioned"]:
                text = f"The page has {'a' if res['has_section'] else 'no'} '{args.section}' heading, and '{args.needle}' appears on the page."
                if res["has_section"]:
                    text += f" (I read the page as flat text, so I can't prove it sits inside the {args.section} section.)"
            elif res["has_section"]:
                text = f"The page has a '{args.section}' heading, but I don't see '{args.needle}' anywhere on it."
            else:
                text = f"I don't see a '{args.section}' heading or '{args.needle}' on that page."
            return ToolOutcome(True, True, text, data={**res, "summary": text}, untrusted=True, family="local")
        if tool == "pick_official_result":
            ranked = analysis.rank_official(args.results, args.query)
            if not ranked:
                return ToolOutcome(False, error="There were no results to choose from.", family="local")
            from browser.urlsafe import registrable
            from urllib.parse import urlsplit

            top = ranked[0]
            top_site = registrable(urlsplit(top["url"]).hostname or "")
            # other pages of the SAME site are not competitors: a docs site with several matching pages is still one clear answer
            rivals = [r["score"] for r in ranked[1:] if registrable(urlsplit(r["url"]).hostname or "") != top_site]
            second = max(rivals) if rivals else -1
            if top["score"] >= 0.5 and top["score"] - second >= 0.2:
                return ToolOutcome(True, True, f"The most likely official result is {top['title']}.", data={"url": top["url"], "title": top["title"], "score": top["score"]}, untrusted=True, family="local")
            cands = [{"name": r["title"][:80], "url": r["url"], "title": r["title"]} for r in ranked[:4]]  # (data below: ranked candidates for the user)
            return ToolOutcome(False, error="I'm not sure which result is the official one.", data={"ambiguous": True, "candidates": cands}, untrusted=True, family="local")
        if tool == "compose_report":
            return ToolOutcome(True, True, compose(args.kind, board), data={}, family="local")
        return ToolOutcome(False, error="That isn't an action I have.")

    def _find_repo(self, query: str) -> ToolOutcome:
        adapter = self.hub.registry.adapter("github")
        linked = adapter.projects.repos_of(query) if hasattr(adapter, "projects") else []
        if len(linked) == 1:
            return ToolOutcome(True, True, f"Found {linked[0]}.", data={"repo": linked[0]}, family="api")
        r = self.hub.tools.call("search_github", {"query": query, "limit": 8})
        if not r.success:
            return ToolOutcome(False, error=r.error["message"] if r.error else "GitHub isn't reachable.", family="api", data={"integration_failed": True})
        hits = r.data
        if not hits:
            return ToolOutcome(False, error=f"I couldn't find a repository matching '{query}' on your GitHub.", family="api", data={"not_found": True})
        if len(hits) > 1:
            norm = re.sub(r"[^a-z0-9]", "", query.lower())
            exact = [h for h in hits if re.sub(r"[^a-z0-9]", "", h["source_id"].split("/")[-1].lower()) == norm]
            if len(exact) == 1:
                hits = exact
        if len(hits) == 1:
            return ToolOutcome(True, True, f"Found {hits[0]['source_id']}.", data={"repo": hits[0]["source_id"], "description": _safe_desc(hits[0].get("summary", ""))}, untrusted=True, family="api")
        cands = [{"name": h["source_id"], "repo": h["source_id"], "title": h["source_id"]} for h in hits[:5]]
        return ToolOutcome(False, error="I found more than one repository that matches.", data={"ambiguous": True, "candidates": cands}, family="api")


def _safe_desc(text: str) -> str:
    """A repository description is spoken aloud: keep it out if it reads like instructions to an assistant."""
    from backend.core.security.trust import sanitize_external, scan_for_injection

    return "" if scan_for_injection(text or "").flagged else sanitize_external(text or "", 160)


def _perm():
    from integrations.hub.models import Permission

    return Permission.READ_REPOSITORIES


def _placeholder(ref: Ref) -> Any:
    return {"repo": "owner/name", "url": "https://example.com/", "int": 1, "data": {}, "list": [], "role": "button"}.get(ref.kind, "x")


def resolve(arguments: dict[str, Any], board: dict[str, Any], allow_private: bool = False) -> tuple[dict[str, Any], str | None]:
    """Replace `Ref`s by blackboard values of the declared type. Text/data come from earlier tool *outputs* only; a value that does not fit its
    declared type (a repo that is not owner/name, a URL that fails policy, a non-integer) is refused, so page text cannot smuggle a different kind of argument."""
    from browser.urlsafe import validate_url

    out: dict[str, Any] = {}
    for key, value in arguments.items():
        if not isinstance(value, Ref):
            out[key] = value
            continue
        if value.key not in board:
            return {}, f"missing earlier result: {value.key}"
        got = board[value.key]
        if value.kind == "repo":
            if not isinstance(got, str) or not _REPO.match(got) or ".." in got:
                return {}, "that isn't a valid repository name"
        elif value.kind == "url":
            decision = validate_url(got if isinstance(got, str) else "", allow_private=allow_private, resolver=None)
            if not decision.ok:
                return {}, "that address isn't allowed"
            got = decision.url
        elif value.kind == "int":
            if isinstance(got, bool) or not isinstance(got, int):
                return {}, "that isn't a number"
        elif value.kind == "role":
            if got not in _ROLES:
                got = "button" if got in (None, "") else got
            if got not in _ROLES:
                return {}, "that isn't a control type I can click"
        elif value.kind == "data":
            if not isinstance(got, dict):
                return {}, "that isn't page data"
        elif value.kind == "list":
            if not isinstance(got, list):
                return {}, "that isn't a list"
        else:
            got = str(got)[:60_000]
        out[key] = got
    return out, None


def compose(kind: str, board: dict[str, Any]) -> str:
    """The final answer, built from what the task actually learned (never from a step that did not run)."""
    g = board.get
    if kind == "repo_found" and g("repo_source") == "browser_search":
        return (f"GitHub isn't connected, so I searched GitHub publicly. The top match for that name is {g('repo', '')}. "
                "I can't tell from a public search whether it's yours, so please check.")
    if kind == "repo_found":
        return f"I found your repository {g('repo', '')}" + (f": {g('repo_desc')}" if g("repo_desc") else "") + "."
    if kind == "readme_summary":
        return str(g("summary", "I read the README but have nothing to report."))
    if kind == "technologies":
        return f"{g('repo', 'The repository')}: {g('tech_summary', 'I could not identify technologies.')}"
    if kind == "section_check":
        return str(g("section_summary", "I checked the page."))
    if kind == "youtube":
        return " ".join(x for x in (g("play_message"), g("volume_message")) if x) or "Done."
    if kind == "official_result":
        return f"I opened {g('opened_title', 'the result')}."
    if kind == "download":
        return str(g("download_message", "The download finished."))
    if kind == "page":
        return str(g("page_summary", "Here is what the page shows."))
    return "Done."
