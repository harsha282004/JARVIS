"""REAL_WORLD check of the autonomous agent against the live internet (real headless Edge/Chrome, throw-away profile, no sign-in, no GitHub token).

    python scripts/autonomy_real_check.py [--headed]

Runs multi-step goals through the real planner, tool router, verifier and BrowserEngine on real YouTube, Bing and github.com (public pages; the GitHub *API*
is not connected here, so repository lookup uses the browser path). Prints every step with its status and timing, and the final answer. It asserts nothing:
live sites change. Exit code 0 if the script ran to the end. Not part of the automated suite.
"""

import argparse
import shutil
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from autonomy.manager import AutonomyManager  # noqa: E402
from autonomy.observe import Observer  # noqa: E402
from autonomy.planner import PlanContext, Planner  # noqa: E402
from autonomy.runner import AutonomyConfig  # noqa: E402
from autonomy.toolrouter import ToolRouter  # noqa: E402
from backend.core.metrics import metrics  # noqa: E402
from browser.downloads import BrowserLog  # noqa: E402
from browser.driver import PlaywrightBrowserDriver  # noqa: E402
from browser.engine import BrowserConfig, BrowserEngine  # noqa: E402
from browser.tools import BrowserTools  # noqa: E402

GOALS = [
    "Search YouTube for Blinding Lights, play the official video and set the volume to 30%.",
    "Search the web for PostgreSQL documentation and open the most relevant official result",
    "Open GitHub and find my Virtual Campus repository.",
]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--headed", action="store_true")
    args = ap.parse_args()
    work = Path(tempfile.mkdtemp(prefix="jarvis-autonomy-check-"))
    config = BrowserConfig(download_dir=work / "downloads", screenshot_dir=work / "shots", navigation_timeout_s=30.0, default_timeout_s=10.0, settle_seconds=3.0)

    def factory():
        return PlaywrightBrowserDriver(browser_type="auto", headless=not args.headed, profile_dir=work / "profile", download_dir=config.download_dir, allow_private=False,
                                       nav_timeout_ms=30000, default_timeout_ms=10000)

    engine = BrowserEngine(factory, config, BrowserLog(work / "browser_log.jsonl"))
    tools = BrowserTools(engine)
    router = ToolRouter(tools, None)

    def context() -> PlanContext:
        st = engine.status()
        yt = tools.youtube
        url = st["url"]
        return PlanContext(host=url.split("/")[2] if url.startswith("http") else "", url=url, browser_open=st["state"] == "ready", yt_query=yt.last_query, yt_results=bool(yt.last_results),
                           github_available=False)

    manager = AutonomyManager(Planner(router), router, Observer(engine), AutonomyConfig(inline_wait_s=1, max_duration_s=150), context_provider=context,
                              browser_stop=engine.stop_current_action, history_path=work / "history.json", threaded=False)
    print(f"Real-world autonomy check ({'headed' if args.headed else 'headless'})\n")
    try:
        for goal in GOALS:
            print(f">> {goal}")
            started = time.perf_counter()
            reply = manager.start(goal, "real-check")
            task = manager.last()
            took = time.perf_counter() - started
            if task is None:
                print("   (no task)", reply.text if reply else "")
                continue
            for s in task.steps:
                print(f"   [{s.status.value:7}] {s.risk.name:15} {s.description}")
            print(f"   -> {task.status.value} in {took:.1f}s | replans={task.replans} retries={task.retries} tool_calls={task.tool_calls}")
            print(f"   answer: {(reply.text if reply else '')[:220]}\n")
        snap = metrics.snapshot()["timers"]
        print("Timings (mean ms):", {k: v["mean_ms"] for k, v in snap.items() if k.startswith("autonomy.") or k.startswith("browser.")})
    finally:
        manager.shutdown()
        engine.shutdown()
        shutil.rmtree(work, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
