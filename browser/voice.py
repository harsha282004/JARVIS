"""BrowserRouter: spoken/typed browser requests -> browser tools -> a reply built only from verified results.

Deterministic patterns (like the Phase 17/18 routers), so it works offline and cannot be steered by page text. It never touches the
engine or a page itself: every action goes through `BrowserTools.call` (schema, category, PermissionManager). Anything consequential comes
back as `needs_confirmation`; it is registered with the shared ConfirmationEngine and runs only after the user's own spoken yes.

Conversation context ("Open YouTube" ... "Search for X" ... "Play the official one") is the browser's real state (which site the active tab
shows, what the last YouTube search returned), read from `BrowserEngine.status()` and the YouTube workflow. It is in memory only and is not
saved as personal memory.

Page text reaches the reply only as quoted data ("The page is titled '...'"), sanitized, and a page whose text looks like instructions
to an assistant is announced as such and otherwise ignored.
"""

import re
from typing import Any

from agent.intelligence.confirmation import ActionReport
from backend.core.logging import get_logger
from backend.core.security.approval import ApprovalClass
from browser.models import BrowserResult, Category
from browser.tools import BrowserTools
from browser.urlsafe import KNOWN_SITES, same_site, validate_url

logger = get_logger(__name__)

_ORD = {"first": 1, "1st": 1, "one": 1, "second": 2, "2nd": 2, "two": 2, "third": 3, "3rd": 3, "three": 3, "fourth": 4, "4th": 4, "four": 4, "fifth": 5, "5th": 5, "five": 5}
_SITE_NAMES = "|".join(sorted(map(re.escape, KNOWN_SITES), key=len, reverse=True))

_OPEN_YT = re.compile(r"^open youtube(?: and (?:play|search(?: for)?) (?P<q>.+))?$")
_SEARCH_YT = re.compile(r"^(?:search|look)(?: on)? youtube(?: for)? (?P<q>.+)$|^youtube search (?P<q2>.+)$|^search for (?P<q3>.+?) on youtube$")
_PLAY_OFFICIAL = re.compile(r"^play (?:the )?official(?: (?:one|song|version|video|music video|track))?$")
_PLAY_NTH = re.compile(rf"^play (?:the )?(?:(?P<w>{'|'.join(_ORD)}) (?:one|result|video)|(?:number|result|video) (?P<n>\d))$|^play (?:number )?(?P<n2>\d)$")
_PLAY_THIS = re.compile(r"^play (?:this|that|it)$")
_PLAY_ON_YT = re.compile(r"^play (?P<q>.+?) on youtube$")
_PLAY = re.compile(r"^play (?:the )?(?:song |video |music )?(?P<q>.+)$")
_RESUME = re.compile(r"^(?:resume|continue|unpause|keep playing|play)(?: (?:the )?(?:video|music|song|playback|it))?$")
_PAUSE = re.compile(r"^(?:pause|hold|stop)(?: (?:the )?(?:video|music|song|playback|youtube|it))$|^pause$")
_SKIP = re.compile(r"^(?:skip|next)(?: (?:the )?(?:ad|video|song|track|this|this one))?$")
_SEEK = re.compile(r"^(?:skip|jump|go|fast forward|forward) (?:forward |ahead )?(?:by )?(?P<n>\d{1,3}) seconds?$|^(?P<back>rewind|go back|skip back|back) (?:by )?(?P<m>\d{1,3}) seconds?$")
_VOLUME = re.compile(r"^(?:set )?(?:the )?volume (?:to )?(?P<p>\d{1,3})(?: percent| %)?$|^(?:turn )?(?:the )?volume (?P<d>up|down)$|^(?:turn it|make it) (?P<d2>up|down)$")
_CLOSE_YT = re.compile(r"^close youtube$")
_CLOSE_BROWSER = re.compile(r"^(?:close|quit|exit) (?:the )?(?:web )?browser$")
_CLOSE_TAB = re.compile(r"^close (?:this|the|the current) tab$")
_OPEN_SITE = re.compile(r"^(?:open|go to|navigate to|visit|launch|take me to)(?: the)?(?: website| site| page)? (?P<s>.+)$")
_OPEN_THIS = re.compile(r"^open (?:this|that|the) (?:url|link|website|address|page)$")
_WEB_SEARCH = re.compile(r"^(?:search the web for|search online for|web search|google|look up|search the internet for) (?P<q>.+?)(?: online)?$")
_GH_SEARCH = re.compile(r"^search github for (?P<q>.+)$")
_SEARCH_BARE = re.compile(r"^search(?: for)? (?P<q>.+)$")
_BACK = re.compile(r"^(?:go back|back|previous page|go to the previous page)$")
_FORWARD = re.compile(r"^(?:go forward|forward|next page)$")
_REFRESH = re.compile(r"^(?:refresh|reload)(?: the)?(?: page)?$")
_SCROLL = re.compile(r"^scroll (?P<d>down|up)(?: (?:a bit|a little|some|more|a lot))?$|^scroll to (?:the )?(?P<e>top|bottom)(?: of the page)?$|^(?:go to the )(?P<e2>top|bottom) of the page$")
_CLICK = re.compile(r"^(?:click|press|tap|select)(?: on)?(?: the)? (?P<t>.+?)(?: (?P<r>button|link|tab))?$")
_FIND = re.compile(r"^(?:find|locate|look for|show me)(?: the)? (?P<d>.+?)(?: (?:on the page|here))?$")
_READ = re.compile(r"^(?:read|summari[sz]e|describe)(?: this| the)?(?: current)? page$|^what(?:'s| is) on (?:this|the) page$|^what page (?:is this|am i on)$|^what(?:'s| is) (?:this|the) page$")
_TYPE = re.compile(r"^type (?P<x>.+?) (?:into|in) (?:the )?(?P<f>.+?)(?: (?:field|box))?$")
_PRESS_ENTER = re.compile(r"^press (?:the )?enter(?: key)?$|^hit enter$")
_GH_REPO = re.compile(r"^open (?:my|the) (?P<n>.+?) (?:repo|repository|project)(?: on github)?$|^open (?:the )?(?:repo|repository) (?P<n2>.+)$")
_GH_SECTION = re.compile(r"^open (?:the |my )?(?P<s>pull requests?|prs|issues|latest issue|newest issue|recent issue)$")
_SHOW_TABS = re.compile(r"^(?:what|which) tabs? (?:are )?(?:open|do i have)$|^how many tabs(?: are open)?$")
_CTX_RESULT_WORDS = re.compile(r"\b(official|first|second|third)\b")

MAX_SPOKEN_RESULTS = 3


def _clean(text: str) -> str:
    return " ".join(text.strip().strip(".!?,").split())


class BrowserRouter:
    def __init__(self, tools: BrowserTools, confirmations, *, hub_router=None):
        self._tools = tools
        self._confirmations = confirmations
        self._hub_router = hub_router
        self._yt = tools.youtube

    # ---- context ----------------------------------------------------------------------------------------------------------------

    def _kind(self) -> str | None:
        """Which site the browser is actually on, from real state: 'youtube', 'github', 'web', or None when no page is open."""
        st = self._tools.engine.status()
        url = st.get("url", "")
        if st["state"] in ("closed", "error") or not url.startswith("http"):
            return None
        if same_site(url, "https://www.youtube.com/"):
            return "youtube"
        if same_site(url, "https://github.com/"):
            return "github"
        return "web"

    def _has_video(self) -> bool:
        return self._kind() == "youtube"

    # ---- entry -------------------------------------------------------------------------------------------------------------------

    def handle(self, t: str, original: str, session_id: str) -> str | None:
        try:
            return self._handle(_clean(t), original, session_id)
        except Exception as exc:  # noqa: BLE001 - the conversation must survive; say we could not
            logger.error("Browser request failed (%s)", type(exc).__name__)
            return "Sorry, I couldn't do that in the browser."

    def _call(self, name: str, args: dict[str, Any] | None, session_id: str) -> str:
        result = self._tools.call(name, args or {}, session_id=session_id)
        if result.needs_confirmation:
            return self._ask(name, args or {}, result, session_id)
        return self.speak(result)

    def _ask(self, name: str, args: dict[str, Any], result: BrowserResult, session_id: str) -> str:
        cls = ApprovalClass.SENSITIVE_DESKTOP if result.category == Category.SENSITIVE_ACTION.value else ApprovalClass.EXTERNAL_MESSAGE

        def run() -> ActionReport:
            done = self._tools.call(name, args, session_id=session_id, confirmed=True)  # only ever reached after the user's own yes
            return ActionReport(done.success and done.verified, self.speak(done), done.verified)

        return self._confirmations.request(action_class=cls, tool=f"browser.{name}", summary=result.message, params={"tool": name, "args": args}, run=run, session_id=session_id,
                                           source="browser")

    # ---- wording: only verified results are reported as done --------------------------------------------------------------------------

    def speak(self, r: BrowserResult) -> str:
        if r.success and r.verified:
            return self._decorate(r)
        if r.success:
            base = r.message.rstrip(".") or "I did that"
            return f"{base}, but I couldn't verify that it worked."
        if r.data.get("ambiguous"):
            names = [c.get("name") or c.get("title") for c in r.data.get("candidates", [])][:4]
            listed = "; ".join(f"{i + 1}, {n}" for i, n in enumerate(names) if n)
            return (r.error if r.error and "match" not in r.error else "Which one do you mean?") + (f" I see: {listed}." if listed else "")
        return (r.error or "That didn't work.").rstrip(".") + "."

    @staticmethod
    def _decorate(r: BrowserResult) -> str:
        message = r.message
        if r.action == "search_youtube":
            rows = r.data.get("results", [])[:MAX_SPOKEN_RESULTS]
            listed = "; ".join(f"{x['n']}, {x['title']} by {x['channel']}" + (" (official)" if x["official"] else "") for x in rows)
            return f"{message} Top results: {listed}. Say which one to play, or 'play the official one'."
        if r.action == "web_search":
            rows = r.data.get("results", [])[:MAX_SPOKEN_RESULTS]
            listed = "; ".join(f"{i + 1}, {x['title']} on {x['url'].split('/')[2]}" for i, x in enumerate(rows))
            flag = " Some of those pages contain text that looks like instructions; I'm ignoring it." if any(x.get("injection_suspected") for x in rows) else ""
            return f"{message} {listed}.{flag}"
        if r.action == "find_element":
            rows = r.data.get("candidates", [])[:3]
            return f"{message} " + "; ".join(f"{c['role']} '{c['name']}'" for c in rows) + "."
        if r.action == "read_page":
            d = r.data
            if d.get("injection_suspected"):  # say nothing of what the page says: only that it is trying to instruct an assistant
                return (f"The page is titled '{d['title']}'. " if d.get("title") else "") + "This page contains text that looks like instructions to an assistant. I'm treating it as page content and ignoring it."
            head = f"The page is titled '{d['title']}'." if d.get("title") else "The page has no title."
            heads = f" Headings: {'; '.join(d['headings'][:4])}." if d.get("headings") else ""
            warn = " This page contains text that looks like instructions to an assistant. I'm treating it as page content and ignoring it." if d.get("injection_suspected") else ""
            return head + heads + warn
        if r.action == "get_page_state":
            return message
        return message

    # ---- routing ----------------------------------------------------------------------------------------------------------------------

    def _handle(self, t: str, original: str, sid: str) -> str | None:  # noqa: C901 - a flat list of patterns
        kind = self._kind()

        def cq(text: str) -> str:  # the user's own capitalization, not the lower-cased routing text
            text = _clean(text)
            i = original.lower().find(text.lower())
            return original[i:i + len(text)] if i >= 0 else text

        m = _OPEN_YT.match(t)
        if m:
            opened = self._tools.call("open_youtube", {}, session_id=sid)
            if not (opened.success and opened.verified) or not m.group("q"):
                return self.speak(opened)
            play = "play" in t.split(" and ", 1)[1].split()[0]
            return self.speak(opened) + " " + (self._call("play_youtube", {"query": cq(m.group("q"))}, sid) if play else self._call("search_youtube", {"query": cq(m.group("q"))}, sid))
        m = _SEARCH_YT.match(t)
        if m:
            return self._call("search_youtube", {"query": cq(m.group("q") or m.group("q2") or m.group("q3"))}, sid)
        if _PLAY_OFFICIAL.match(t):
            if not self._yt.last_results:
                return "There are no search results to choose from. Tell me what to play."
            return self._call("play_youtube", {"official": True}, sid)
        m = _PLAY_NTH.match(t)
        if m and self._yt.last_results:
            n = _ORD.get(m.group("w") or "") or int(m.group("n") or m.group("n2"))
            return self._call("play_youtube", {"choice": n}, sid)
        if _PLAY_THIS.match(t):
            if kind == "youtube" and self._yt.last_results and len(self._yt.last_results) > 1 and self._yt.now_playing is None:
                names = [f"{i + 1}, {r['title']}" for i, r in enumerate(self._yt.last_results[:MAX_SPOKEN_RESULTS])]
                return "Which one do you mean? " + "; ".join(names) + "."
            if kind == "youtube" and self._yt.now_playing is not None:
                return self._call("resume_youtube", {}, sid)
            return "What would you like me to play?"
        m = _PLAY_ON_YT.match(t)
        if m:
            return self._call("play_youtube", {"query": cq(m.group("q"))}, sid)
        if kind == "youtube" and (m := _PLAY.match(t)) and not _RESUME.match(t):
            return self._call("play_youtube", {"query": cq(m.group("q"))}, sid)
        if kind == "youtube":
            if _RESUME.match(t):
                return self._call("resume_youtube", {}, sid)
            if _PAUSE.match(t):
                return self._call("pause_youtube", {}, sid)
            if _SKIP.match(t):
                return self._call("skip_youtube", {}, sid)
            m = _SEEK.match(t)
            if m:
                seconds = int(m.group("n") or m.group("m")) * (-1 if m.group("back") else 1)
                return self._call("seek_youtube", {"seconds": seconds}, sid)
            m = _VOLUME.match(t)
            if m:
                if m.group("p"):
                    return self._call("volume_youtube", {"percent": min(100, int(m.group("p")))}, sid)
                return self._relative_volume(m.group("d") or m.group("d2"), sid)
        if _CLOSE_YT.match(t):
            return self._call("close_youtube", {}, sid)
        if _CLOSE_BROWSER.match(t):
            return self._call("close_browser", {}, sid)
        if _CLOSE_TAB.match(t):
            return self._call("close_tab", {}, sid)
        if _SHOW_TABS.match(t):
            st = self._tools.engine.status()
            if st["state"] == "closed":
                return "The browser isn't open."
            titles = "; ".join(f"{x['tab_id']}: {x['title'] or x['url']}" for x in st["tabs"])
            return f"{st['tab_count']} tab{'s' if st['tab_count'] != 1 else ''} open. {titles}"
        if _OPEN_THIS.match(t):
            return "Tell me the address, for example 'open example dot com', or type it."
        m = _GH_REPO.match(t)
        if m:
            return self._open_repo(cq(m.group("n") or m.group("n2")), sid)
        m = _GH_SECTION.match(t)
        if m:
            return self._github_section(m.group("s"), sid)
        m = _GH_SEARCH.match(t)
        if m:
            return self._github_search(cq(m.group("q")), sid)
        m = _WEB_SEARCH.match(t)
        if m:
            return self._call("web_search", {"query": cq(m.group("q"))}, sid)
        m = _OPEN_SITE.match(t)
        if m and not _CLOSE_BROWSER.match(t):
            return self._open_site(cq(m.group("s")), sid)
        if _BACK.match(t) and kind:
            return self._call("go_back", {}, sid)
        if _FORWARD.match(t) and kind:
            return self._call("go_forward", {}, sid)
        if _REFRESH.match(t) and kind:
            return self._call("refresh_page", {}, sid)
        m = _SCROLL.match(t)
        if m and kind:
            end = m.group("e") or m.group("e2")
            return self._call("scroll", {"direction": end or m.group("d")}, sid)
        if _READ.match(t) and kind:
            return self._call("read_page", {}, sid)
        if _PRESS_ENTER.match(t) and kind:
            return self._call("press_key", {"key": "Enter"}, sid)
        m = _TYPE.match(t)
        if m and kind:
            return self._call("type_text", {"name": cq(m.group("f")), "text": m.group("x").strip()}, sid)
        m = _SEARCH_BARE.match(t)
        if m and kind and not t.startswith("search my") and " my " not in f" {t} ":
            q = cq(m.group("q"))
            if kind == "youtube":
                return self._call("search_youtube", {"query": q}, sid)
            if kind == "github":
                return self._github_search(re.sub(r"^(?:for )?(?:my )?", "", re.sub(r"\s+(?:repo|repository|project)$", "", q)), sid)
            return self._call("web_search", {"query": q}, sid)
        if m and kind == "github" and " my " in f" {t} ":
            return self._github_search(re.sub(r"^(?:my )", "", re.sub(r"\s+(?:repo|repository|project)$", "", cq(m.group("q")))), sid)
        m = _CLICK.match(t)
        if m and kind and not _CLOSE_TAB.match(t):
            role = {"button": "button", "link": "link", "tab": "tab"}.get(m.group("r") or "")
            args: dict[str, Any] = {"name": cq(m.group("t"))}
            if role:
                args["role"] = role
            return self._call("click_element", args, sid)
        m = _FIND.match(t)
        if m and kind:
            return self._find(cq(m.group("d")), sid)
        return None

    # ---- helpers -----------------------------------------------------------------------------------------------------------------------

    def _relative_volume(self, direction: str, sid: str) -> str:
        ok, state = self._tools.engine.media("state", expect=lambda s: True, wait_s=0.1)
        if not ok or state is None or not state.present:
            return "There's no video to change the volume of."
        goal = max(0, min(100, round(state.volume * 100) + (20 if direction == "up" else -20)))
        return self._call("volume_youtube", {"percent": goal}, sid)

    def _open_site(self, name: str, sid: str) -> str:
        key = re.sub(r"^(?:my )", "", name.lower()).strip()
        key = re.sub(r"\s+(?:website|site|page|homepage)$", "", key)
        if key in KNOWN_SITES:
            if key == "youtube":
                return self._call("open_youtube", {}, sid)
            return self._call("open_url", {"url": KNOWN_SITES[key]}, sid)
        spoken = re.sub(r"\s+dot\s+", ".", key)
        if validate_url(spoken.replace(" ", ""), resolver=None).ok and "." in spoken:
            return self._call("open_url", {"url": spoken.replace(" ", "")}, sid)
        return f"I don't know a website called {name}. Tell me its address, like example dot com."

    def _find(self, description: str, sid: str) -> str:
        found = self._tools.call("find_element", {"description": description}, session_id=sid)
        if not found.success:
            return self.speak(found)
        top = found.data["candidates"][0]
        if top["role"] == "link" and not found.data["ambiguous"] and re.search(r"\b(page|link)\b|login|log in|sign in|sign up|contact|pricing|docs", description, re.I):
            return self._call("click_element", {"role": "link", "name": top["name"]}, sid)
        return self.speak(found)

    def _github_search(self, query: str, sid: str) -> str:
        if self._hub_router is not None and query:
            repo, problem = self._hub_router._repo_for(query)  # noqa: SLF001 - resolves through the Integration Hub (API), not the browser
            if repo:
                return self._call("open_url", {"url": f"https://github.com/{repo}"}, sid)
            if problem and problem.startswith("Which repository"):
                return problem
        from urllib.parse import quote_plus

        return self._call("open_url", {"url": f"https://github.com/search?q={quote_plus(query)}&type=repositories"}, sid)

    def _open_repo(self, name: str, sid: str) -> str:
        if self._hub_router is not None:
            repo, problem = self._hub_router._repo_for(name)  # noqa: SLF001
            if repo:
                return self._call("open_url", {"url": f"https://github.com/{repo}"}, sid)
            if problem and problem.startswith("Which repository"):
                return problem
        return self._github_search(name, sid)

    def _github_section(self, section: str, sid: str) -> str:
        url = self._tools.engine.status().get("url", "")
        m = re.match(r"https://github\.com/([\w.\-]+/[\w.\-]+)", url)
        if not m:
            return "Open a repository first, then I can open its pull requests or issues."
        repo = m.group(1)
        if section.startswith(("pull", "pr")):
            return self._call("open_url", {"url": f"https://github.com/{repo}/pulls"}, sid)
        if section == "issues":
            return self._call("open_url", {"url": f"https://github.com/{repo}/issues"}, sid)
        return self._call("open_url", {"url": f"https://github.com/{repo}/issues?q=is%3Aissue+sort%3Acreated-desc"}, sid) + " That's the issue list, newest first."
