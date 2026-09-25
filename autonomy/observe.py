"""Observation and verification: what the world looks like after an action, and whether that matches what the step expected.

`Observer` reads only what the current task needs: the browser's cached address/title/tabs (free: no browser call) and, for media steps, the
`<video>` state. `Verifier` evaluates a step's explicit `Check`s against the tool outcome AND an independent observation, so completion is never
"the tool said so". A verified-by-tool success that the observation contradicts is a failure.
"""

import time
from urllib.parse import urlsplit

from autonomy.models import Check, Observation, Step, Verdict
from autonomy.toolrouter import ToolOutcome
from backend.core.metrics import metrics
from browser.engine import BrowserEngine
from browser.urlsafe import registrable


class Observer:
    def __init__(self, engine: BrowserEngine | None, clock=time.monotonic):
        self._engine = engine
        self._clock = clock

    def observe(self, *, media: bool = False) -> Observation:
        began = time.perf_counter()
        obs = Observation(at=self._clock())
        if self._engine is not None:
            st = self._engine.status()
            url = st.get("url", "")
            obs.url, obs.title, obs.tab_count, obs.active_tab = url, st.get("title", ""), st.get("tab_count", 0), st.get("active_tab")
            obs.browser_state = st.get("state", "closed")
            obs.host = (urlsplit(url).hostname or "") if url.startswith("http") else ""
            obs.netloc = (urlsplit(url).netloc or "").lower() if url.startswith("http") else ""
            if media and obs.browser_state == "ready" and url.startswith("http"):
                ok, state = self._engine.media("state", expect=lambda s: True, wait_s=0.1)
                if state is not None and state.present:
                    obs.playing, obs.volume, obs.ad = (not state.paused and not state.ended), round(state.volume, 2), state.ad
                elif state is not None:
                    obs.playing = False
        metrics.observe("autonomy.observe_ms", (time.perf_counter() - began) * 1000)
        return obs

    def observe_dialogs(self, obs: Observation, outcome: ToolOutcome) -> Observation:
        """Fold flags a tool result already reported (sign-in form, CAPTCHA, dialog) into the observation without another page read."""
        d = outcome.data or {}
        obs.login_required = bool(d.get("login_required")) or obs.login_required
        obs.captcha = bool(d.get("captcha")) or obs.captcha
        obs.dialog = bool(d.get("dialog")) or obs.dialog
        return obs


class Verifier:
    def verify(self, step: Step, outcome: ToolOutcome, before: Observation, after: Observation, board: dict) -> Verdict:
        began = time.perf_counter()
        changed = before.diff(after)
        try:
            if not outcome.success:
                return Verdict(False, outcome.error or "the step failed", changed)
            for c in step.verification:
                ok, why = self._check(c, outcome, before, after, board)
                if not ok:
                    return Verdict(False, why, changed)
            return Verdict(True, "", changed)
        finally:
            metrics.observe("autonomy.verify_ms", (time.perf_counter() - began) * 1000)

    def holds(self, c: Check | None, after: Observation, board: dict) -> bool:
        """Is `c` already true of the current state (used to skip steps that are already satisfied)?"""
        if c is None:
            return False
        return self._check(c, ToolOutcome(True, True), after, after, board)[0]

    def _check(self, c: Check, outcome: ToolOutcome, before: Observation, after: Observation, board: dict) -> tuple[bool, str]:
        k = c.kind
        if k == "outcome_verified":
            return (outcome.verified, "the tool could not confirm it worked") if not outcome.verified else (True, "")
        if k == "url_host":
            if c.get("netloc"):  # an address with a port (or an IP): exact match, never "same registrable domain"
                return after.netloc == str(c.get("netloc")).lower(), f"the browser is on {after.netloc or 'no page'}, not {c.get('netloc')}"
            want = registrable(str(c.get("host")))
            got = registrable(after.host) if after.host else ""
            return got == want, f"the browser is on {after.host or 'no page'}, not {c.get('host')}"
        if k == "url_is":
            return after.url.rstrip("/").lower() == str(c.get("url")).rstrip("/").lower(), f"the browser isn't on {c.get('url')}"
        if k == "url_path_contains":
            path = urlsplit(after.url).path.lower() if after.url else ""
            need = str(c.get("text")).lower()
            return need in path, f"the address doesn't look like {c.get('text')}"
        if k == "url_repo":
            repo = str(c.get("repo") or board.get(str(c.get("key", "repo")), "")).lower()
            path = urlsplit(after.url).path.lower().strip("/") if after.url else ""
            return bool(repo) and path.startswith(repo) and (urlsplit(after.url).hostname or "").endswith("github.com"), f"the browser isn't on the repository {repo}"
        if k == "url_matches_board":
            from urllib.parse import urlsplit as _u

            want = _u(str(board.get(str(c.get("key")), ""))).hostname or ""
            return bool(want) and registrable(after.host) == registrable(want), f"the browser isn't on {want or 'the chosen page'}"
        if k == "title_contains":
            return str(c.get("text")).lower() in after.title.lower(), f"the page title doesn't mention {c.get('text')}"
        if k == "page_changed":
            return bool(before.diff(after)), "nothing on the page changed"
        if k == "data_key":
            v = outcome.data.get(str(c.get("key")))
            return (bool(v) if not isinstance(v, (int, float)) else True), f"the step produced no {c.get('key')}"
        if k == "board_has":
            v = board.get(str(c.get("key")))
            return bool(v), f"I don't have {c.get('key')} yet"
        if k == "playing":
            return after.playing is True, "the video is not playing"
        if k == "volume":
            want = float(c.get("percent", 0)) / 100
            return after.volume is not None and abs(after.volume - want) <= 0.03, f"the volume is {round((after.volume or 0) * 100)}%, not {c.get('percent')}%"
        if k == "results_exist":
            rows = outcome.data.get(str(c.get("key", "results")))
            return bool(rows), "there were no results"
        if k == "tab_count_at_least":
            return after.tab_count >= int(c.get("n", 1)), "no browser tab is open"
        if k == "browser_open":
            return after.browser_state == "ready" and after.tab_count > 0, "the browser isn't open"
        return False, f"unknown check {k}"
