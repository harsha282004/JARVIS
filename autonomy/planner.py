"""The planner: a natural-language goal -> a validated multi-step plan (or a clarifying question, or a refusal).

Deterministic on purpose. Goals are split into clauses, each clause is recognised by a small grammar (open a site, find a repository, read/summarise a
README, YouTube search/play/volume, web search + official result, portfolio section check, find-and-download, upload), and the clauses are compiled into
steps with explicit expected states and verification. Context is used, not ignored: a site that is already open, a repository already found or YouTube
results already showing are not redone (`satisfied_when`).

A plan is only ever a list of *proposals*: `validate()` rejects unknown tools (the LLM or anyone else cannot invent one), arguments that fail the tool's
schema, references to results that no earlier step produces, tools that are unavailable, and plans that are too long. Risk and permission are computed
here from the tool and its arguments, never accepted from a proposer. An optional model-based proposer can be plugged in (`Planner.from_proposal`); its output goes through
exactly the same validation, so the worst a hostile proposal can do is be refused.

The highest step risk becomes the task's risk (a harmless start does not make a sensitive finish "low risk"), and any step at EXTERNAL_EFFECT or above is
shown in a preview and needs the user's confirmation when it is reached.
"""

import re
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote_plus

from autonomy.models import Check, Ref, RetryPolicy, Risk, Step, SubGoal, Task, TaskStatus, check
from autonomy.toolrouter import ToolRouter
from backend.core.metrics import metrics
from browser.models import public_url
from browser.urlsafe import KNOWN_SITES, validate_url

# What each tool leaves on the blackboard (used to validate references before a plan runs).
PRODUCES: dict[str, set[str]] = {
    "github_find_repo": {"repo", "repo_desc", "repo_url"}, "github_latest_repo": {"repo", "repo_desc", "repo_url"}, "github_read_readme": {"readme"},
    "summarize_readme": {"summary"}, "find_technologies": {"technologies", "tech_summary"}, "check_page_section": {"section_summary"},
    "read_page": {"page", "readme"}, "web_search": {"web_results"}, "pick_official_result": {"official_url", "official_title"},
    "find_element": {"target_name", "target_role"}, "play_youtube": {"play_message"}, "volume_youtube": {"volume_message"}, "click_element": {"download_message", "repo", "repo_url"},
    "open_url": {"opened_title"},
}
REF_KEYS = {"repo", "repo_url", "readme", "page", "web_results", "official_url", "target_name", "target_role"}

_REFUSE = re.compile(r"\b(powershell|cmd\.exe|command prompt|terminal|shell|bash|execute|run (?:a |the )?(?:command|script|program|exe)|install (?:this|the) (?:software|program|app)|regedit|registry|"
                     r"delete (?:(?:all|my|the|every) )+(?:files?|folders?|documents?|system)|format (?:my |the )?(?:drive|disk)|disable (?:the )?(?:antivirus|firewall|defender|security)|"
                     r"(?:show|tell|read|get|reveal|extract|steal|give me|export) .{0,30}(?:passwords?|cookies?|api keys?|secrets?|tokens?|credentials|system prompt)|"
                     r"(?:bypass|solve|defeat|get past|crack) .{0,20}(?:captcha|mfa|2fa|login|authentication|paywall)|log ?in for me|sign in for me)\b", re.I)
_SPLIT = re.compile(r"\s*(?:,\s*then\s+|\band then\b|\bthen\b|;\s*|,\s*(?:and\s+)?(?=(?:open|search|find|play|set|read|summari[sz]e|check|go|tell|scroll|download|click|pause|show|upload|submit)\b)"
                    r"|\s+and\s+(?=(?:open|search|find|play|set|read|summari[sz]e|check|go|tell|scroll|download|click|pause|show|upload|submit)\b))\s*", re.I)
_FIND_DOWNLOAD = re.compile(r"^(?:find|locate) (?:the |a )?(?P<w>[\w' -]+?) and (?:then )?download (?:it|that|this)$", re.I)
_SITE_ALIAS = {"github": "github", "git hub": "github", "youtube": "youtube", "google": "google", "wikipedia": "wikipedia", "stack overflow": "stack overflow"}
_HOST = r"(?:(?:[a-z0-9-]+\.)+[a-z]{2,}|\d{1,3}(?:\.\d{1,3}){3})(?::\d{2,5})?"
_DOMAIN = re.compile(rf"^(?:https?://)?{_HOST}(?:[/?#]\S*)?$", re.I)
_TECH_WORDS = re.compile(r"\b(?:what|which) (?:technolog(?:y|ies)|tech(?: stack)?|languages?|frameworks?|libraries|stack)\b|\bthe tech stack\b|\btechnolog(?:y|ies) (?:it|the (?:project|repo(?:sitory)?)) uses\b|\buses?\b.*\btechnolog")


@dataclass
class PlanContext:
    """The state the plan starts from (all read from real state by the manager)."""

    host: str = ""
    url: str = ""
    browser_open: bool = False
    last_repo: str | None = None
    yt_query: str = ""
    yt_results: bool = False
    github_available: bool = True
    browser_available: bool = True
    known: dict[str, str] = field(default_factory=dict)   # slots the user already gave: portfolio_url ...


@dataclass
class PlanOutcome:
    kind: str                       # plan | clarify | refuse | none
    task: Task | None = None
    question: str = ""
    missing: str = ""
    reason: str = ""


@dataclass
class Fragment:
    kind: str
    p: dict[str, Any] = field(default_factory=dict)


def _clause_norm(text: str) -> str:
    t = re.sub(r"^(?:hey |hi |ok |okay )?jarvis[, ]*", "", text.strip(), flags=re.I)
    return " ".join(t.replace("’", "'").strip(" .!?,;").split())


class Planner:
    def __init__(self, router: ToolRouter, *, max_steps: int = 25):
        self._router = router
        self._max_steps = max_steps

    # ---- entry ------------------------------------------------------------------------------------------------------------------------

    def plan(self, goal: str, ctx: PlanContext, session_id: str = "") -> PlanOutcome:
        began = time.perf_counter()
        try:
            return self._plan(goal, ctx, session_id)
        finally:
            metrics.observe("autonomy.plan_ms", (time.perf_counter() - began) * 1000)

    def _plan(self, goal: str, ctx: PlanContext, session_id: str) -> PlanOutcome:
        text = _clause_norm(goal)
        if not text:
            return PlanOutcome("none")
        if _REFUSE.search(text):
            return PlanOutcome("refuse", reason="I can't do that. I only work through the browser and your connected accounts, never a shell, your passwords or your files.")
        m = _FIND_DOWNLOAD.match(text)
        clauses = [text] if m else _SPLIT.split(text)
        if m:
            text_clause = f"find and download {m.group('w')}"
            clauses = [text_clause]
        else:
            m2 = re.match(r"^(.*?)[,;]?\s*(?:and\s+)?(?:then\s+)?find (?:the |a )?(?P<w>[\w' -]+?) and (?:then )?download (?:it|that|this)$", text, re.I)
            if m2 and m2.group(1).strip():
                clauses = [c for c in _SPLIT.split(m2.group(1)) if c.strip()] + [f"find and download {m2.group('w')}"]
        fragments = [f for clause in clauses if clause.strip() and (f := self._fragment(clause.strip()))]
        if not fragments:
            return PlanOutcome("none")
        if not self._is_autonomous(fragments):
            return PlanOutcome("none")
        built = self._compile(fragments, ctx, text)
        if isinstance(built, PlanOutcome):
            return built
        steps, subgoals, ack = built
        task = Task(goal=text[:300], session_id=session_id, steps=steps, subgoals=subgoals, ack=ack)
        problems = self.validate(task, ctx)
        if problems:
            return PlanOutcome("refuse", reason="I couldn't build a safe plan for that: " + problems[0])
        self._finalize(task)
        return PlanOutcome("plan", task)

    @staticmethod
    def _is_autonomous(fragments: list[Fragment]) -> bool:
        """Single browser commands stay with the Phase 20 router; multi-step goals and research/analysis tasks are ours."""
        kinds = [f.kind for f in fragments]
        research = {"readme", "summarize", "technologies", "find_repo", "check_section", "web_search", "official_result", "download", "upload", "find_official", "portfolio"}
        return len(fragments) >= 2 or any(k in research for k in kinds)

    # ---- grammar --------------------------------------------------------------------------------------------------------------------------

    def _fragment(self, clause: str) -> Fragment | None:  # noqa: C901 - a flat grammar
        c = clause.lower()
        m = re.match(r"^(?:open|go to|visit|launch|take me to) (?:my )?(?:the )?(?P<s>.+?)(?: (?:website|site|page|homepage))?$", c)
        if m and not re.search(r"\b(?:repo|repository|readme|result|documentation|docs|video|first|latest|official)\b", m.group("s")):
            s = m.group("s").strip()
            if s == "portfolio" or s == "my portfolio":
                return Fragment("portfolio", {"slot": "portfolio_url"})
            url_in = re.search(rf"((?:https?://)?{_HOST}(?:/\S*)?)", clause)
            if s in KNOWN_SITES or s in _SITE_ALIAS:
                return Fragment("open_site", {"site": _SITE_ALIAS.get(s, s)})
            if url_in and _DOMAIN.match(url_in.group(1).strip()):
                return Fragment("open_site", {"url": url_in.group(1).strip()})
        if re.match(r"^open (?:my )?portfolio\b", c):
            url_in = re.search(rf"((?:https?://)?{_HOST}(?:/\S*)?)", clause)
            return Fragment("portfolio", {"slot": "portfolio_url", "url": url_in.group(1) if url_in else None})
        m = re.match(r"^find (?:me )?(?:my|the) (?P<n>latest|newest|most recent|.+?) (?:github )?(?:repo|repository|project)(?: (?:on|in) github)?$", c)
        if m:
            name = m.group("n")
            name_orig = _orig(clause, name)
            return Fragment("find_repo", {"latest": name in ("latest", "newest", "most recent"), "name": name_orig})
        m = re.match(r"^open (?:my |the )?(?P<n>.+?) (?:repo|repository|project)$", c)
        if m and m.group("n") not in ("this", "that", "the"):
            n = m.group("n")
            return Fragment("find_repo", {"latest": n in ("latest", "newest", "most recent"), "name": _orig(clause, n), "open": True})
        if re.match(r"^open (?:the |that |this |it |my )?(?:repo|repository|project|latest repository)$", c):
            return Fragment("open_repo")
        if "readme" in c:
            focus = "setup" if re.search(r"setup|requirement|install|prerequisite", c) else "overview"
            if re.search(r"summari[sz]e|summary|tell me|what|explain|describe", c):
                return Fragment("summarize", {"focus": focus})
            return Fragment("readme")
        if re.match(r"^(?:summari[sz]e|tell me (?:about|the)|what (?:are|is) the) (?:the )?(?:setup|requirements?|installation|overview)", c) or re.match(r"^summari[sz]e (?:it|that|this)$", c):
            return Fragment("summarize", {"focus": "setup" if re.search(r"setup|requirement|install", c) else "overview"})
        if _TECH_WORDS.search(c):
            return Fragment("technologies")
        m = re.match(r"^search (?:youtube|you tube) for (?P<q>.+)$|^search for (?P<q2>.+?) on youtube$", c)
        if m:
            return Fragment("yt_search", {"query": _orig(clause, m.group("q") or m.group("q2"))})
        if re.match(r"^find (?:the )?official(?: (?:video|song|one|version|music video))?$", c):
            return Fragment("find_official")
        if re.match(r"^play (?:it|that|this|the official(?: (?:video|song|one|version|music video))?|the first(?: one| result)?|the official one)$", c):
            return Fragment("yt_play", {"official": "official" in c, "choice": 1 if "first" in c else None})
        m = re.match(r"^play (?P<q>.+?)(?: on youtube)?$", c)
        if m and "youtube" in c:
            return Fragment("yt_play", {"query": _orig(clause, m.group("q"))})
        m = re.match(r"^(?:set|turn|change|make)(?: the)?(?: youtube)? volume (?:to|at) (?P<p>\d{1,3})(?: ?%| percent)?(?: on youtube)?$|^set (?:it|the volume) to (?P<p2>\d{1,3})(?: ?%| percent)?$", c)
        if m:
            return Fragment("volume", {"percent": min(100, int(m.group("p") or m.group("p2")))})
        m = re.match(r"^search (?:the )?(?:web|internet|online) for (?P<q>.+?)$", c)
        if m:
            return Fragment("web_search", {"query": _orig(clause, m.group("q"))})
        if re.match(r"^open (?:the )?(?:most relevant|best|top)?\s*(?:official )?(?:result|link|page|documentation|docs)(?: for it)?$", c) or re.match(r"^open the (?:most relevant )?official", c):
            return Fragment("official_result")
        m = re.match(r"^check (?:whether|if) (?:the |my )?(?P<sec>[a-z][a-z ]*?) section (?:contains|has|includes|mentions|lists) (?:my |the )?(?P<n>.+?)(?: (?:project|repo|repository))?$", c)
        if m:
            return Fragment("check_section", {"section": _orig(clause, m.group("sec")).title(), "needle": _orig(clause, m.group("n"))})
        if re.match(r"^(?:tell me what you find|tell me|report|let me know|tell me the result)\b", c):
            return Fragment("report")
        m = re.match(r"^(?:find and download|download) (?:the |a |an )?(?P<w>.+)$", c) or re.match(r"^find (?:the )?(?P<w>.+?) and download it$", c)
        if m:
            return Fragment("download", {"what": _orig(clause, m.group("w"))})
        m = re.match(r"^upload (?:my |this |the )?(?P<f>[\w .\-]+?\.\w{2,5})(?: to (?:the )?(?:application|form|page|site|website))?$", c)
        if m:
            return Fragment("upload", {"file": _orig(clause, m.group("f"))})
        if re.match(r"^submit (?:it|the form|the application)$", c):
            return Fragment("submit")
        return None

    # ---- compilation -------------------------------------------------------------------------------------------------------------------

    def _compile(self, frags: list[Fragment], ctx: PlanContext, goal: str):  # noqa: C901
        steps: list[Step] = []
        subs: list[SubGoal] = []
        n = [0]
        state = {"opened": False, "github_open": ctx.host.endswith("github.com"), "repo_known": bool(ctx.last_repo), "readme": False, "yt_open": ctx.host.endswith("youtube.com"),
                 "results": ctx.yt_results, "official": False, "query": ctx.yt_query, "page_read": False, "last": "done", "needs_repo_open": False}
        need_open_repo = any(f.kind in ("open_repo",) or f.p.get("open") for f in frags) or any(f.kind == "find_repo" for f in frags) and any(g.kind == "open_site" and g.p.get("site") == "github" for g in frags)

        def add(description, tool, arguments, *, expected="", verify=(), safe=False, retries=0, satisfied=None, sub=None, fallback=None):
            n[0] += 1
            step = Step(id=f"s{n[0]}", description=description, tool=tool, arguments=arguments, expected_state=expected, verification=list(verify),
                        retry_policy=RetryPolicy(retries, safe), satisfied_when=satisfied, subgoal=sub, fallback=fallback)
            steps.append(step)
            return step

        def ensure_site(site: str | None, url: str | None = None):
            state["opened"] = True
            if site == "youtube":
                add("Open YouTube", "open_youtube", {}, expected="YouTube is open", verify=[check("outcome_verified"), check("url_host", host="youtube.com")], safe=True, retries=2,
                    satisfied=check("url_host", host="youtube.com"))
                state["yt_open"] = True
                return
            target = KNOWN_SITES.get(site or "") or url or ""
            if target and not target.startswith("http"):
                target = "https://" + target
            decision = validate_url(target, allow_private=self._router.allow_private, resolver=None)
            host = decision.host if decision.ok else (site or "")
            from urllib.parse import urlsplit as _split

            netloc = _split(decision.url).netloc.lower() if decision.ok and (_split(decision.url).port or re.fullmatch(r"[\d.]+", host)) else ""
            host_check = check("url_host", host=host, netloc=netloc) if netloc else check("url_host", host=host)
            path = _split(decision.url).path if decision.ok else ""
            # already-open means "on this very page" when the goal names a page, not just a site
            satisfied_check = check("url_is", url=public_url(decision.url)) if decision.ok and path not in ("", "/") else host_check
            label = {"github": "GitHub", "youtube": "YouTube", "google": "Google", "wikipedia": "Wikipedia", "stack overflow": "Stack Overflow"}.get(site or "", (site or host).title() if site else host)
            add(f"Open {label}", "open_url", {"url": target}, expected=f"{host} is open", verify=[check("outcome_verified"), host_check], safe=True, retries=2,
                satisfied=satisfied_check)
            if site == "github":
                state["github_open"] = True

        for i, f in enumerate(frags):
            k = f.kind
            later = frags[i + 1:]
            if k == "open_site":
                ensure_site(f.p.get("site"), f.p.get("url"))
                state["last"] = "page" if not any(g.kind in ("find_repo",) for g in later) else state["last"]
            elif k == "portfolio":
                url = f.p.get("url") or ctx.known.get("portfolio_url")
                if not url:
                    return PlanOutcome("clarify", question="What's the address of your portfolio website?", missing="portfolio_url")
                if not validate_url(url if url.startswith("http") else "https://" + url, allow_private=self._router.allow_private, resolver=None).ok:
                    return PlanOutcome("refuse", reason="I can't open that address.")
                ensure_site(None, url)
                if any(g.kind == "check_section" for g in later):
                    add("Read the page", "read_page", {}, expected="page content read", verify=[check("outcome_verified")], safe=True, retries=2, sub="page")
                    state["page_read"] = True
            elif k == "find_repo":
                if not ctx.github_available and not ctx.browser_available:
                    return PlanOutcome("refuse", reason="Neither GitHub nor the browser is available right now.")
                open_it = f.p.get("open") or need_open_repo
                if ctx.github_available:
                    if f.p.get("latest"):
                        add("Find your latest repository", "github_latest_repo", {}, expected="a repository is found", verify=[check("data_key", key="repo")], safe=True, retries=1, sub="repo",
                            fallback="github_browser")
                    else:
                        add(f"Find your {f.p['name']} repository", "github_find_repo", {"query": f.p["name"]}, expected="one matching repository", verify=[check("data_key", key="repo")],
                            safe=True, retries=1, sub="repo", fallback="github_browser")
                else:  # no API: search on GitHub through the browser
                    ensure_site("github")
                    self._browser_repo_steps(add, f.p["name"], f.p.get("latest"))
                    open_it = False  # the link click already opened it
                state["repo_known"] = True
                if open_it:
                    add("Open the repository", "open_url", {"url": Ref("repo_url", "url")}, expected="the repository page is open", verify=[check("outcome_verified"), check("url_repo", key="repo")],
                        safe=True, retries=2, satisfied=check("url_repo", key="repo"), sub="repo")
                    state["needs_repo_open"] = False
                state["last"] = "repo_found"
            elif k == "open_repo":
                if not state["repo_known"]:
                    return PlanOutcome("clarify", question="Which repository do you mean?", missing="repo_name")
                add("Open the repository", "open_url", {"url": Ref("repo_url", "url")}, expected="the repository page is open", verify=[check("outcome_verified"), check("url_repo", key="repo")],
                    safe=True, retries=2, satisfied=check("url_repo", key="repo"))
                state["last"] = "repo_found"
            elif k in ("readme", "summarize", "technologies"):
                if not state["repo_known"] and not ctx.last_repo:
                    return PlanOutcome("clarify", question="Which repository should I read the README of?", missing="repo_name")
                if not state["readme"]:
                    if ctx.github_available:
                        add("Read the README", "github_read_readme", {"repo": Ref("repo", "repo")}, expected="the README text", verify=[check("data_key", key="readme")], safe=True, retries=1,
                            sub="readme", fallback="readme_via_browser")
                    else:
                        self._readme_browser_steps(add)
                    state["readme"] = True
                if k == "summarize":
                    add("Summarize the README", "summarize_readme", {"source": Ref("readme", "text"), "focus": f.p["focus"], "name": Ref("repo", "text")},
                        expected="a summary", verify=[check("data_key", key="summary")], sub="summary")
                    state["last"] = "readme_summary"
                elif k == "technologies":
                    add("Identify the technologies", "find_technologies", {"source": Ref("readme", "text")}, expected="technologies found", verify=[check("data_key", key="technologies")], sub="tech")
                    state["last"] = "technologies"
                else:
                    state["last"] = "readme_summary" if False else state["last"]
            elif k == "yt_search":
                q = f.p["query"]
                if not (ctx.yt_results and ctx.yt_query.lower() == q.lower() and ctx.host.endswith("youtube.com")):
                    ensure_site("youtube")
                    add(f"Search YouTube for {q}", "search_youtube", {"query": q}, expected="search results appear", verify=[check("outcome_verified"), check("results_exist", key="results")],
                        safe=True, retries=2, sub="search")
                state["results"], state["query"] = True, q
                state["last"] = "youtube"
            elif k == "find_official":
                state["official"] = True
                state["last"] = "youtube"
            elif k == "yt_play":
                if not state["yt_open"]:
                    ensure_site("youtube")
                args: dict[str, Any] = {}
                if f.p.get("query"):
                    args["query"] = f.p["query"]
                elif f.p.get("choice"):
                    args["choice"] = f.p["choice"]
                else:
                    if not state["results"]:
                        return PlanOutcome("clarify", question="What should I search YouTube for?", missing="youtube_query")
                    args["official"] = bool(f.p.get("official") or state["official"])
                add("Play the video" + (" (official)" if args.get("official") else ""), "play_youtube", args, expected="the video is playing",
                    verify=[check("outcome_verified"), check("playing")], sub="play")
                state["last"] = "youtube"
            elif k == "volume":
                add(f"Set the volume to {f.p['percent']}%", "volume_youtube", {"percent": f.p["percent"]}, expected=f"volume is {f.p['percent']}%",
                    verify=[check("outcome_verified"), check("volume", percent=f.p["percent"])], safe=True, retries=1, sub="volume")
                state["last"] = "youtube"
            elif k == "web_search":
                add(f"Search the web for {f.p['query']}", "web_search", {"query": f.p["query"]}, expected="results appear", verify=[check("outcome_verified"), check("results_exist", key="results")],
                    safe=True, retries=2, sub="search")
                state["query"] = f.p["query"]
                state["last"] = "page"
            elif k == "official_result":
                if not state["query"]:
                    return PlanOutcome("clarify", question="What should I search for?", missing="search_query")
                add("Choose the official result", "pick_official_result", {"results": Ref("web_results", "list"), "query": state["query"]}, expected="one clearly official result",
                    verify=[check("data_key", key="url")], sub="official")
                add("Open the official result", "open_url", {"url": Ref("official_url", "url")}, expected="the result page is open", verify=[check("outcome_verified"), check("url_matches_board", key="official_url")],
                    safe=True, retries=2, sub="official")
                state["last"] = "official_result"
            elif k == "check_section":
                if not state["page_read"]:
                    if not ctx.browser_open and not state["opened"]:
                        return PlanOutcome("clarify", question="Which website should I check?", missing="site")
                    add("Read the page", "read_page", {}, expected="page content read", verify=[check("outcome_verified")], safe=True, retries=2, sub="page")
                add(f"Check the {f.p['section']} section", "check_page_section", {"page": Ref("page", "data"), "section": f.p["section"], "needle": f.p["needle"]},
                    expected="the section was checked", verify=[check("data_key", key="summary")], sub="section")
                state["last"] = "section_check"
            elif k == "download":
                if not ctx.browser_open and not state["opened"]:
                    return PlanOutcome("clarify", question="Which website should I look on?", missing="site")
                add(f"Find {f.p['what']}", "find_element", {"description": f.p["what"]}, expected="a matching control is found", verify=[check("data_key", key="candidates")], safe=True, retries=2, sub="find")
                add(f"Download {f.p['what']}", "click_element", {"role": Ref("target_role", "role"), "name": Ref("target_name", "text")}, expected="the file is downloaded",
                    verify=[check("outcome_verified")], sub="download")
                state["last"] = "download"
            elif k == "upload":
                add(f"Attach {f.p['file']}", "upload_file", {"filename": f.p["file"]}, expected="the file is attached (not submitted)", verify=[check("outcome_verified")], sub="upload")
                state["last"] = "done"
            elif k == "submit":
                add("Submit the form", "click_element", {"role": "button", "name": "Submit"}, expected="the form is submitted", verify=[check("outcome_verified")], sub="submit")
                state["last"] = "done"
            elif k == "report":
                pass
        if not steps:
            return PlanOutcome("none")
        # the final report is always a step: completion is decided from what the task actually learned
        add("Report the result", "compose_report", {"kind": _report_kind(state["last"], frags)}, expected="the answer is ready", verify=[])
        subs = _subgoals(steps)
        return steps, subs, _ack(frags)

    @staticmethod
    def _browser_repo_steps(add, name: str, latest: bool) -> None:
        add(f"Search GitHub for {name}", "open_url", {"url": f"https://github.com/search?q={quote_plus(name)}&type=repositories"}, expected="search results are open",
            verify=[check("outcome_verified"), check("url_host", host="github.com")], safe=True, retries=2, sub="repo")
        add("Find the repository link", "find_element", {"description": f"{name} repository"}, expected="the repository link", verify=[check("data_key", key="candidates")], safe=True, retries=2, sub="repo")
        s = add("Open the repository", "click_element", {"role": "link", "name": Ref("target_name", "text")}, expected="the repository page is open",
                verify=[check("outcome_verified"), check("url_path_contains", text="/")], sub="repo")
        s.note = "derive_repo_from_url"

    @staticmethod
    def _readme_browser_steps(add) -> None:
        add("Read the repository page", "read_page", {}, expected="the README as shown on the page", verify=[check("outcome_verified")], safe=True, retries=2, sub="readme")

    # ---- fallbacks (replanning) ---------------------------------------------------------------------------------------------------------

    def expand_fallback(self, task: Task, step: Step, outcome, obs) -> list[Step] | None:
        """Steps to insert in place of `step` when it cannot be completed as planned; None if there is no safe alternative."""
        name = step.fallback
        if self._router.browser is None:
            return None

        def mk(suffix: str, description: str, tool: str, arguments: dict[str, Any], *, expected: str = "", verify: tuple = (), safe: bool = False, retries: int = 0,
               satisfied: Check | None = None, note: str = "") -> Step:
            return Step(id=f"{step.id}{suffix}", description=description, tool=tool, arguments=arguments, expected_state=expected, verification=list(verify),
                        retry_policy=RetryPolicy(retries, safe), satisfied_when=satisfied, subgoal=step.subgoal, note=note)

        if name == "github_browser" and (outcome.data.get("integration_failed") or outcome.data.get("not_found")):
            if step.tool == "github_latest_repo":
                return self._finish([mk("a", "Open your GitHub repositories", "open_url", {"url": "https://github.com/?tab=repositories"}, expected="GitHub is open",
                                        verify=(check("outcome_verified"), check("url_host", host="github.com")), safe=True, retries=2)])
            query = str(step.arguments.get("query") or "")
            return self._finish([
                mk("a", f"Search GitHub for {query}", "open_url", {"url": f"https://github.com/search?q={quote_plus(query)}&type=repositories"}, expected="search results are open",
                   verify=(check("outcome_verified"), check("url_host", host="github.com")), safe=True, retries=2),
                mk("b", "Find the repository link", "find_element", {"description": f"{query} repository"[:100]}, expected="the repository link", verify=(check("data_key", key="candidates"),),
                   safe=True, retries=2),
                mk("c", "Open the repository", "click_element", {"role": "link", "name": Ref("target_name", "text")}, expected="the repository page is open",
                   verify=(check("outcome_verified"),), note="derive_repo_from_url")])
        if name == "readme_via_browser":
            return self._finish([
                mk("a", "Open the repository page", "open_url", {"url": Ref("repo_url", "url")}, expected="the repository page is open",
                   verify=(check("outcome_verified"), check("url_repo", key="repo")), safe=True, retries=2, satisfied=check("url_repo", key="repo")),
                mk("b", "Read the repository page", "read_page", {}, expected="the README as shown", verify=(check("outcome_verified"),), safe=True, retries=2)])
        if name == "retarget" and step.tool == "click_element":
            desc = step.arguments.get("name")
            desc = task.blackboard.get("target_name", "") if isinstance(desc, Ref) else str(desc or "")
            if not desc:
                return None
            return self._finish([
                mk("f", f"Look again for {desc[:40]}", "find_element", {"description": desc[:100]}, expected="a matching control", verify=(check("data_key", key="candidates"),), safe=True, retries=1),
                mk("g", step.description, "click_element", {"role": Ref("target_role", "role"), "name": Ref("target_name", "text")}, expected=step.expected_state, verify=tuple(step.verification))])
        return None

    def _finish(self, steps: list[Step], ctx=None) -> list[Step]:
        for s in steps:
            s.risk, s.permission = self._router.risk_of(s.tool, s.arguments, s.description)
        return steps

    # ---- validation and risk ---------------------------------------------------------------------------------------------------------------

    def validate(self, task: Task, ctx: PlanContext | None) -> list[str]:
        """Every reason this plan must not run (empty = acceptable). Also computes each step's risk/permission and the task's risk."""
        problems: list[str] = []
        if not task.steps:
            problems.append("the plan is empty")
        if len(task.steps) > self._max_steps:
            problems.append(f"the plan has more than {self._max_steps} steps")
        produced: set[str] = set()
        seen_ids: set[str] = set()
        for s in task.steps:
            if s.id in seen_ids:
                problems.append(f"duplicate step id {s.id}")
            seen_ids.add(s.id)
            ok, reason = self._router.available(s.tool)
            if not ok:
                problems.append(f"{s.tool}: {reason}")
                continue
            if (why := self._router.validate(s.tool, s.arguments)) is not None:
                problems.append(why)
                continue
            for v in s.arguments.values():
                if isinstance(v, Ref):
                    if v.key not in REF_KEYS:
                        problems.append(f"{s.tool} refers to an unknown result ({v.key})")
                    elif v.key not in produced and not (ctx and v.key in ("repo", "repo_url") and ctx.last_repo):
                        problems.append(f"{s.tool} needs {v.key}, which no earlier step produces")
            produced |= PRODUCES.get(s.tool, set())
            s.risk, s.permission = self._router.risk_of(s.tool, s.arguments, s.description)
        task.risk_level = max((s.risk for s in task.steps), default=Risk.READ_ONLY)
        return problems

    def _finalize(self, task: Task) -> None:
        task.preview = preview(task) if task.risk_level >= Risk.EXTERNAL_EFFECT else ""

    # ---- optional model-proposed plans -----------------------------------------------------------------------------------------------------

    def from_proposal(self, goal: str, proposal: list[dict[str, Any]], ctx: PlanContext, session_id: str = "") -> PlanOutcome:
        """A plan proposed by something else (an LLM). Only the tool name, arguments and description are taken; everything else is computed here.
        `{"$ref": "repo"}` values become typed references. The same validation as our own plans applies; anything unknown, malformed, over-long or
        referring to unavailable results makes the whole proposal refused."""
        if not isinstance(proposal, list) or not proposal:
            return PlanOutcome("refuse", reason="The proposed plan was empty or malformed.")
        steps: list[Step] = []
        for i, raw in enumerate(proposal[: self._max_steps + 1]):
            if not isinstance(raw, dict) or not isinstance(raw.get("tool"), str) or not isinstance(raw.get("arguments", {}), dict):
                return PlanOutcome("refuse", reason="The proposed plan was malformed.")
            args: dict[str, Any] = {}
            for k, v in raw.get("arguments", {}).items():
                if isinstance(v, dict) and set(v) == {"$ref"} and isinstance(v["$ref"], str):
                    args[k] = Ref(v["$ref"], _ref_kind(v["$ref"]))
                elif isinstance(v, (str, int, float, bool)) or v is None:
                    args[k] = v
                else:
                    return PlanOutcome("refuse", reason="The proposed plan had an argument I don't accept.")
            steps.append(Step(id=f"p{i + 1}", description=str(raw.get("description") or raw["tool"])[:120], tool=raw["tool"], arguments=args,
                              verification=[check("outcome_verified")] if self._router.family(raw["tool"]) == "browser" else [], expected_state=str(raw.get("expected", ""))[:120]))
        task = Task(goal=goal[:300], session_id=session_id, steps=steps, ack="Got it.")
        problems = self.validate(task, ctx)
        if problems:
            return PlanOutcome("refuse", reason="I rejected that plan: " + problems[0])
        task.subgoals = _subgoals(task.steps)
        self._finalize(task)
        return PlanOutcome("plan", task)


def _ref_kind(key: str) -> str:
    return {"repo": "repo", "repo_url": "url", "official_url": "url", "page": "data", "web_results": "list"}.get(key, "text")


def _orig(clause: str, fragment: str) -> str:
    i = clause.lower().find(fragment.lower())
    return clause[i:i + len(fragment)].strip() if i >= 0 else fragment.strip()


def _report_kind(last: str, frags: list[Fragment]) -> str:
    kinds = {f.kind for f in frags}
    if "summarize" in kinds:
        return "readme_summary"
    if "technologies" in kinds:
        return "technologies"
    return last if last in ("repo_found", "youtube", "section_check", "official_result", "download") else "done"


def _subgoals(steps: list[Step]) -> list[SubGoal]:
    out: dict[str, SubGoal] = {}
    for s in steps:
        if s.subgoal and s.subgoal not in out:
            names = {"repo": "Find the repository", "readme": "Read the README", "summary": "Summarize it", "tech": "Identify the technologies", "search": "Search", "play": "Play the video",
                     "volume": "Set the volume", "official": "Open the official result", "page": "Read the page", "section": "Check the section", "find": "Find the item",
                     "download": "Download it", "upload": "Attach the file", "submit": "Submit"}
            last = [x for x in steps if x.subgoal == s.subgoal][-1]
            out[s.subgoal] = SubGoal(s.subgoal, names.get(s.subgoal, s.subgoal), last.verification[0] if last.verification else check("outcome_verified"))
    return list(out.values())


def _ack(frags: list[Fragment]) -> str:
    k = frags[0].kind
    return {"open_site": "Got it. I'm opening it.", "find_repo": "Got it. I'm checking GitHub.", "yt_search": "Got it. I'm on YouTube.", "web_search": "Got it. I'm searching.",
            "portfolio": "Got it. I'm opening your portfolio.", "download": "Got it. I'm looking for it.", "upload": "Got it."}.get(k, "Got it.")


def preview(task: Task) -> str:
    lines = ["I can do this:"]
    gated = []
    for i, s in enumerate(t for t in task.steps if t.tool != "compose_report"):
        lines.append(f"{i + 1}. {s.description}")
        if s.risk >= Risk.EXTERNAL_EFFECT:
            gated.append(i + 1)
    if gated:
        lines.append(("Step " + " and ".join(map(str, gated)) + " require" + ("s" if len(gated) == 1 else "") + " your confirmation."))
    return "\n".join(lines)
