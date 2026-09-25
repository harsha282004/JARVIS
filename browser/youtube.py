"""YouTube as the first complete browser workflow, built only on the BrowserEngine (no OS commands, no scripts of its own).

    open / search / play / pause / resume / skip / seek / volume / close

Result selection is scored, not "first hit": exact title, query words, official channels (verified / "Official Artist Channel" / VEVO /
"- Topic" / "Official" in the title), and penalties for covers, remixes, karaoke, reactions, slowed/sped-up versions, mixes and shorts
unless the query asks for them. If no result is clearly better than the rest it asks "Which one do you mean?" instead of guessing.
Every playback command is verified against the page's <video> state; a UI change that breaks it produces a failure, never a false "Playing".
"""

import re
from typing import Any
from urllib.parse import quote_plus, urlsplit

from backend.core.security.trust import sanitize_external
from browser.engine import BrowserEngine
from browser.models import BrowserResult, MediaState, Target, public_url
from browser.urlsafe import same_site

WATCH_HOST = "youtube.com"
_NOISE = frozenset({"the", "a", "an", "song", "video", "music", "official", "by", "on", "youtube", "play", "version", "one", "of", "please", "audio"})
_BAD = ("cover", "remix", "karaoke", "reaction", "slowed", "sped up", "speed up", "8d", "nightcore", "instrumental", "tutorial", "#shorts", "full album", "compilation",
        "1 hour", "10 hours", "loop", "reverb", "parody", "mashup", "live", "lyrics", "lyric video", "mix", "shorts", "type beat", "how to")
_OFFICIAL_BADGE = re.compile(r"official artist|verified", re.I)
AUTO_PICK_SCORE = 0.7
AUTO_PICK_MARGIN = 0.1


def _tokens(text: str) -> set[str]:
    return {t for t in re.findall(r"[a-z0-9]+", text.lower()) if t not in _NOISE}


def _norm(text: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", text.lower()))


def is_official(item: dict[str, Any]) -> bool:
    channel, title = item.get("channel", ""), item.get("title", "")
    badges = " ".join(item.get("badges", []))
    return bool(_OFFICIAL_BADGE.search(badges) or re.search(r"vevo$|- topic$", channel, re.I) or re.search(r"\bofficial\b", title, re.I))


def score(query: str, item: dict[str, Any]) -> float:
    q, title = _tokens(query), item.get("title", "")
    t = _tokens(title) | _tokens(item.get("channel", ""))
    if not q:
        return 0.0
    value = 0.6 * len(q & t) / len(q)
    nq, nt = _norm(re.sub(r"\bofficial\b", "", query, flags=re.I)), _norm(title)
    if nq and (nt == nq or nt.startswith(nq + " ") or nt.startswith(nq)):
        value += 0.2
    if is_official(item):
        value += 0.25
    lower = title.lower()
    asked = query.lower()
    if re.search(r"official (?:music )?video|\(official video\)", lower) and "audio" not in asked:
        value += 0.12  # a tie between an official video and an official audio goes to the video unless the user asked for audio
    elif "audio" in asked and re.search(r"official audio", lower):
        value += 0.12
    for word in _BAD:
        if word in lower and word not in asked:
            value -= 0.35
            break
    return round(value, 3)


def rank(query: str, results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    scored = [dict(r, score=score(query, r), official=is_official(r)) for r in results]
    return sorted(scored, key=lambda r: -r["score"])


def choose(query: str, results: list[dict[str, Any]], official_only: bool = False) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    """(the pick, or None with the candidates to ask about)."""
    ranked = rank(query, results)
    pool = [r for r in ranked if r["official"]] if official_only else ranked
    if not pool:
        return None, ranked[:4]
    top = pool[0]
    second = pool[1]["score"] if len(pool) > 1 else -1.0
    if top["score"] >= AUTO_PICK_SCORE and (top["score"] - second) >= AUTO_PICK_MARGIN:
        return top, []
    if len(pool) == 1 and top["score"] >= 0.55:
        return top, []
    return None, pool[:4]


def _safe_result(r: dict[str, Any], n: int) -> dict[str, Any]:
    return {"n": n, "title": sanitize_external(r.get("title", ""), 120), "channel": sanitize_external(r.get("channel", ""), 60), "official": r.get("official", False),
            "duration": sanitize_external(r.get("duration", ""), 12), "score": r.get("score")}


class YouTube:
    def __init__(self, engine: BrowserEngine):
        self.engine = engine
        self.last_query = ""
        self.last_results: list[dict[str, Any]] = []   # what the user can refer to: "the second one", "the official one" (in memory only)
        self.now_playing: dict[str, Any] | None = None

    # ---- helpers -------------------------------------------------------------------------------------------------------------------

    def _on_youtube(self) -> bool:
        url = self.engine.status().get("url", "")
        return bool(url) and same_site(url, "https://www.youtube.com/")

    def _fail(self, action: str, error: str, **kw: Any) -> BrowserResult:
        return BrowserResult(False, action, kw.pop("target", ""), kw.pop("url", ""), False, error=error, **kw)

    def _state(self) -> MediaState | None:
        if not self._on_youtube():
            return None
        ok, state = self.engine.media("state", expect=lambda s: True, wait_s=0.1)
        return state if ok and state is not None else state

    # ---- workflows ---------------------------------------------------------------------------------------------------------------------

    def open(self) -> BrowserResult:
        r = self.engine.open_url("https://www.youtube.com/", expect_host=WATCH_HOST)
        r.action = "open_youtube"
        if r.success and "consent." in r.url:
            r.verified = False
            r.message = "YouTube is asking for cookie consent. Please choose an option yourself, then ask me again."
        elif r.success and r.verified and "youtube" not in r.data.get("title", "").lower():
            r.verified = False
            r.message = "The page opened, but it doesn't look like YouTube's home page."
        elif r.success and r.verified:
            r.message = "YouTube is open."
        return r

    def search(self, query: str) -> BrowserResult:
        opened = self.engine.open_url(f"https://www.youtube.com/results?search_query={quote_plus(query)}", expect_host=WATCH_HOST)
        if not opened.success:
            opened.action = "search_youtube"
            return opened
        if "consent." in opened.url:
            return self._fail("search_youtube", "YouTube is asking for cookie consent. Please choose an option yourself, then ask me again.", url=opened.url)
        rows: list[dict[str, Any]] = []
        for _ in range(6):  # results render after the page loads
            rows = self.engine.extract("youtube_results") or []
            if rows:
                break
            self.engine._sleep(0.7)
        watch = [r for r in rows if same_site(r.get("href", ""), "https://www.youtube.com/") and urlsplit(r["href"]).path == "/watch"]
        if not watch:
            self.last_results, self.last_query = [], query
            return self._fail("search_youtube", "The search showed no videos I could read." if not rows else "None of the results were videos.", target=query[:60], url=opened.url)
        for r in watch:
            r["url"] = r["href"]
        self.last_query = query
        self.last_results = rank(query, watch)
        return BrowserResult(True, "search_youtube", query[:60], opened.url, True, message=f"Found {len(watch)} results for {query[:40]}.",
                             data={"query": query[:100], "results": [_safe_result(r, i + 1) for i, r in enumerate(self.last_results[:8])]}, untrusted=True)

    def play(self, query: str | None = None, choice: int | None = None, official: bool = False) -> BrowserResult:
        if query and (query != self.last_query or not self.last_results):
            found = self.search(query)
            if not found.success:
                found.action = "play_youtube"
                return found
        elif not self.last_results and not query:
            return self._fail("play_youtube", "What would you like me to play?")
        results = self.last_results
        if choice is not None:
            if not 1 <= choice <= len(results):
                return self._fail("play_youtube", f"There are only {len(results)} results.")
            pick = results[choice - 1]
        else:
            pick, candidates = choose(query or self.last_query, results, official_only=official)
            if pick is None:
                shown = [_safe_result(r, results.index(r) + 1) for r in candidates]
                msg = ("None of them are clearly official. " if official and not any(r["official"] for r in results) else "") + "Which one do you mean?"
                return BrowserResult(False, "play_youtube", (query or self.last_query)[:60], "", False, error=msg,
                                     data={"ambiguous": True, "candidates": shown}, untrusted=True)
        return self._start(pick)

    def _start(self, pick: dict[str, Any]) -> BrowserResult:
        opened = self.engine.open_url(pick["url"], expect_host=WATCH_HOST)
        if not opened.success:
            opened.action = "play_youtube"
            return opened
        title = sanitize_external(pick.get("title", ""), 120)
        ok, state = self.engine.media("state", expect=lambda s: s.present and not s.paused and (s.ad or s.current_time > 0), wait_s=8.0)
        if not ok and state is not None and state.present and state.paused:
            ok, state = self.engine.media("play", expect=lambda s: s.present and not s.paused, wait_s=6.0)
        if not ok or state is None or not state.present:
            return self._fail("play_youtube", f"I opened {title}, but I couldn't confirm that it started playing.", target=title, url=opened.url)
        self.now_playing = {"title": title, "channel": pick.get("channel", "")}
        page_title = opened.data.get("title", "")
        matches = bool(_tokens(title) & _tokens(page_title)) or not page_title
        by = f" by {sanitize_external(pick.get('channel', ''), 60)}" if pick.get("channel") else ""
        if state.ad:
            return BrowserResult(True, "play_youtube", title, opened.url, matches, message=f"An ad is playing first; {title}{by} follows.", data={"ad": True, "skippable_ad": state.skippable_ad}, untrusted=True)
        return BrowserResult(True, "play_youtube", title, opened.url, matches, message=f"Playing {title}{by}." if matches else f"Something is playing, but the page title doesn't match {title}.", untrusted=True)

    def _need_video(self, action: str) -> tuple[MediaState | None, BrowserResult | None]:
        if not self._on_youtube():
            return None, self._fail(action, "YouTube isn't open.")
        state = self._state()
        if state is None or not state.present:
            return None, self._fail(action, "There's no video on this page.")
        return state, None

    def pause(self) -> BrowserResult:
        state, err = self._need_video("pause_youtube")
        if err:
            return err
        if state.paused:
            return BrowserResult(True, "pause_youtube", "", "", True, message="It's already paused.")
        ok, after = self.engine.media("pause", expect=lambda s: s.present and s.paused, wait_s=3.0)
        return BrowserResult(ok, "pause_youtube", "", "", ok, message="Paused." if ok else "", error="" if ok else "I pressed pause, but the video is still playing.")

    def resume(self) -> BrowserResult:
        state, err = self._need_video("resume_youtube")
        if err:
            return err
        if not state.paused and not state.ended:
            return BrowserResult(True, "resume_youtube", "", "", True, message="It's already playing.")
        ok, after = self.engine.media("play", expect=lambda s: s.present and not s.paused, wait_s=4.0)
        return BrowserResult(ok, "resume_youtube", "", "", ok, message="Resumed." if ok else "", error="" if ok else "I tried to resume, but the video is still paused.")

    def skip(self) -> BrowserResult:
        state, err = self._need_video("skip_youtube")
        if err:
            return err
        if state.ad and state.skippable_ad:
            self.engine.click_element(Target(css=".ytp-skip-ad-button, .ytp-ad-skip-button, .ytp-ad-skip-button-modern"))
            ok, after = self.engine.media("state", expect=lambda s: not s.ad, wait_s=4.0)
            return BrowserResult(ok, "skip_youtube", "ad", "", ok, message="Skipped the ad." if ok else "", error="" if ok else "I pressed skip, but the ad is still playing.")
        if state.ad:  # the skip button appears a few seconds into an ad
            ok, state = self.engine.media("state", expect=lambda s: s.skippable_ad or not s.ad, wait_s=7.0)
            if state is not None and state.ad and state.skippable_ad:
                self.engine.click_element(Target(css=".ytp-skip-ad-button, .ytp-ad-skip-button, .ytp-ad-skip-button-modern"))
                ok, after = self.engine.media("state", expect=lambda s: not s.ad, wait_s=4.0)
                return BrowserResult(ok, "skip_youtube", "ad", "", ok, message="Skipped the ad." if ok else "", error="" if ok else "I pressed skip, but an ad is still playing.")
            if state is not None and state.ad:
                return self._fail("skip_youtube", "The ad can't be skipped.")
            return BrowserResult(True, "skip_youtube", "ad", "", True, message="The ad finished on its own.")
        before = self.engine.page_url()
        r = self.engine.click_element(Target(css=".ytp-next-button"))
        if not r.success:
            return self._fail("skip_youtube", "There's no next video to skip to." if "not found" in r.error.lower() else r.error)
        now = self.engine.page_url()
        ok = now != before
        return BrowserResult(ok, "skip_youtube", "next", public_url(now), ok, message="Skipped to the next video." if ok else "", error="" if ok else "I pressed next, but the video didn't change.")

    def seek(self, seconds: int) -> BrowserResult:
        state, err = self._need_video("seek_youtube")
        if err:
            return err
        if state.ad:
            return self._fail("seek_youtube", "An ad is playing, and ads can't be skipped through.")
        goal = max(0.0, state.current_time + seconds)
        if state.duration:
            goal = min(goal, state.duration)
        ok, after = self.engine.media("seek", goal, expect=lambda s: abs(s.current_time - goal) < 3.0 or s.ended, wait_s=3.0)
        word = f"{abs(seconds)} seconds {'forward' if seconds >= 0 else 'back'}"
        return BrowserResult(ok, "seek_youtube", word, "", ok, message=f"Jumped {word}." if ok else "", error="" if ok else "The video didn't move to that point.")

    def volume(self, percent: int) -> BrowserResult:
        state, err = self._need_video("volume_youtube")
        if err:
            return err
        goal = percent / 100
        ok, after = self.engine.media("volume", goal, expect=lambda s: abs(s.volume - goal) < 0.03, wait_s=3.0)
        return BrowserResult(ok, "volume_youtube", f"{percent}%", "", ok, message=f"Volume is {percent} percent." if ok else "", error="" if ok else "The volume didn't change.")

    def close(self) -> BrowserResult:
        r = self.engine.close_tabs_matching(WATCH_HOST)
        r.action = "close_youtube"
        if r.success:
            self.now_playing = None
            self.last_results = []
            r.message = "Closed YouTube." if "wasn't open" not in r.message else "YouTube wasn't open."
        return r
