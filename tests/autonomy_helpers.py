"""Test rig for the Phase 21 autonomous agent: REAL planner, tool router, runner, verifier, ConfirmationEngine, permission manager, Integration Hub (GitHub over an
httpx MockTransport) and BrowserEngine, over the deterministic fake web from `tests.browser_helpers`. Nothing here contacts a live site."""

from pathlib import Path

from agent.intelligence.hub_router import HubRouter
from agent.intelligence.router import IntelligenceRouter
from autonomy.manager import AutonomyManager, AutonomyRouter
from autonomy.observe import Observer, Verifier
from autonomy.planner import PlanContext, Planner
from autonomy.runner import AutonomyConfig
from autonomy.toolrouter import ToolRouter
from browser.tools import BrowserTools
from browser.voice import BrowserRouter
from tests.browser_helpers import El, FakeWeb, Spec, make_engine, standard_web, yt_item
from tests.hub_helpers import build_hub_harness

README = """# Virtual Campus

An AI-assisted campus platform built with FastAPI, PostgreSQL and React.

## Requirements

- Python 3.11 or newer
- PostgreSQL 15 with the pgvector extension
- Node.js 20 for the frontend
- Ollama running locally

## Installation

```bash
pip install -r requirements.txt
alembic upgrade head
```

## Usage

Start the API with uvicorn.
"""

SONGS = [
    yt_item("The Weeknd - Blinding Lights (Official Video)", "The Weeknd", "official1", badges=["Official Artist Channel"]),
    yt_item("Blinding Lights - The Weeknd (Lyrics)", "Lyric Vault", "lyrics1"),
    yt_item("Blinding Lights (Cover) by Some Guy", "Some Guy", "cover1"),
]


def rig_web() -> FakeWeb:
    web = standard_web()
    web.add("https://github.com/harsh/virtual-campus", Spec("harsh/virtual-campus: An AI-assisted campus platform", "Virtual Campus README", ["virtual-campus", "README"]))
    web.add("https://github.com/harsh/jarvis", Spec("harsh/jarvis", "JARVIS", ["jarvis"]))
    web.add("https://github.com/search?q=Virtual+Campus&type=repositories", Spec("Repository search", "results", ["Results"], [
        El("link", "harsh/virtual-campus", "a", action=("goto", "https://github.com/harsh/virtual-campus"))]))
    web.add("https://github.com/?tab=repositories", Spec("Your repositories", "repos"))
    web.add("https://www.youtube.com/results?search_query=Blinding+Lights", Spec("Blinding Lights - YouTube", "results", yt_results=SONGS))
    for r in SONGS:
        web.add(r["href"], Spec(f"{r['title']} - YouTube", "watch", video=True))
    web.add("https://portfolio.example.dev/", Spec("Harsh - Portfolio", "Welcome. Projects: Virtual Campus, JARVIS", ["About", "Projects", "Contact"], [
        El("link", "Virtual Campus", "a"), El("link", "JARVIS", "a")]))
    web.add("https://portfolio.example.dev/empty", Spec("Other Portfolio", "Nothing yet", ["About", "Projects"]))
    web.add("https://docs.postgresql.org/", Spec("PostgreSQL Documentation", "docs", ["Documentation"]))
    web.add("https://www.bing.com/search?q=PostgreSQL+documentation", Spec("PostgreSQL documentation - Search", "results", web_results=[
        {"title": "PostgreSQL: Documentation", "url": "https://www.postgresql.org/docs/", "snippet": "Official PostgreSQL documentation"},
        {"title": "PostgreSQL tutorial for beginners", "url": "https://www.w3schools.com/postgresql/", "snippet": "learn"},
        {"title": "Top 10 PostgreSQL tips", "url": "https://medium.com/pg-tips", "snippet": "blog"}]))
    web.add("https://www.postgresql.org/docs/", Spec("PostgreSQL: Documentation", "PostgreSQL documentation", ["Documentation"]))
    web.add("https://reports.example.org/", Spec("Reports", "Reports", ["Reports"], [
        El("link", "Satellite report 2025", "a", action=("download", "satellite-2025.pdf", b"%PDF-1.4 sat")),
        El("link", "Weather report 2025", "a", action=("download", "weather-2025.pdf", b"%PDF-1.4 wx")),
        El("link", "Economy report 2025", "a", action=("download", "economy-2025.pdf", b"%PDF-1.4 eco"))]))
    web.add("https://jobs.example.com/apply", Spec("Apply", "Job application", ["Apply"], [
        El("textbox", "Resume", "input", "file"), El("button", "Submit", "button", action=("text", "application submitted"))]))
    return web


class AutoRig:
    def __init__(self, tmp_path: Path, web: FakeWeb | None = None, *, threaded: bool = False, readme: str = README, cfg: AutonomyConfig | None = None, github: bool = True,
                 confirmation_timeout_s: float = 5.0):
        self.tmp = tmp_path
        self.web = web or rig_web()
        self.h = build_hub_harness(tmp_path)
        self.h.github.repos.append({"full_name": "harsh/virtual-campus", "description": "Virtual Campus platform", "private": False, "default_branch": "main", "language": "Python",
                                   "pushed_at": "2026-09-23T07:00:00Z", "updated_at": "2026-09-23T07:00:00Z", "open_issues_count": 1, "archived": False})
        self.h.github.readme = readme
        if github:
            self.h.sync("github")
        self.engine = make_engine(self.web, tmp_path)
        self.browser_tools = BrowserTools(self.engine)
        self.router = ToolRouter(self.browser_tools, self.h.hub if github else None)
        self.planner = Planner(self.router)
        self.observer = Observer(self.engine)
        self.announced: list[tuple[str, str]] = []
        self.cfg = cfg or AutonomyConfig(inline_wait_s=0.3 if threaded else 5.0, confirmation_timeout_s=confirmation_timeout_s)
        self.confirmations = self.h.base.service.confirmations

        def ctx() -> PlanContext:
            st = self.engine.status()
            yt = self.browser_tools.youtube
            return PlanContext(host=(st["url"].split("/")[2] if st["url"].startswith("http") else ""), url=st["url"], browser_open=st["state"] == "ready", yt_query=yt.last_query,
                               yt_results=bool(yt.last_results))

        self.manager = AutonomyManager(self.planner, self.router, self.observer, self.cfg, confirmations=self.confirmations, announce=lambda t, p: self.announced.append((t, p)),
                                       context_provider=ctx, browser_stop=self.engine.stop_current_action, history_path=tmp_path / "autonomy_history.json", threaded=threaded)
        self.autonomy_router = AutonomyRouter(self.manager)
        hub_router = HubRouter(self.h.hub, self.h.base.service, remember=lambda x: None)
        self.browser_router = BrowserRouter(self.browser_tools, self.confirmations, hub_router=hub_router)
        self.intel = IntelligenceRouter(self.h.base.service, hub_router, self.browser_router, self.autonomy_router)

    def run_plan(self, steps: list[dict], goal: str = "custom plan", session: str = "s1"):
        """Run a proposed plan (validated like any other) to completion; returns the finished Task or the refusal outcome."""
        outcome = self.planner.from_proposal(goal, steps, PlanContext(), session)
        if outcome.kind != "plan":
            return outcome
        self.manager._launch(outcome.task)
        return outcome.task

    def wait(self, predicate, timeout: float = 10.0) -> bool:
        import time

        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if predicate():
                return True
            time.sleep(0.02)
        return False

    def say(self, text: str, session: str = "s1") -> str | None:
        reply = self.intel.handle(text, session)
        return None if reply is None else reply.text

    def close(self) -> None:
        self.manager.shutdown()
        self.engine.shutdown()
