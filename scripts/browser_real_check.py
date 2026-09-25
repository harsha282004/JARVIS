"""Real-browser, real-internet check of the browser agent (headless by default: silent, nothing appears on screen).

    python scripts/browser_real_check.py [--headed] [--query "Blinding Lights"]

Drives the real BrowserEngine + tools against the real YouTube, GitHub and DuckDuckGo with the installed Edge/Chrome. It uses a throw-away
browser profile (no sign-in, no cookies of yours) and a throw-away download folder. Prints what actually happened at each step, including
failures; nothing is asserted as a pass because live sites change. Exit code 0 if the script ran to the end.
"""

import argparse
import shutil
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from browser.downloads import BrowserLog  # noqa: E402
from browser.driver import PlaywrightBrowserDriver  # noqa: E402
from browser.engine import BrowserConfig, BrowserEngine  # noqa: E402
from browser.tools import BrowserTools  # noqa: E402


def show(step: str, started: float, result) -> None:
    d = result.to_dict()
    flag = "OK " if result.success and result.verified else "OK?" if result.success else "FAIL"
    detail = (d.get("message") or d.get("error") or "")[:110]
    print(f"{flag:4} {step:34} {time.perf_counter() - started:6.1f}s  {detail}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--headed", action="store_true")
    ap.add_argument("--query", default="Blinding Lights")
    args = ap.parse_args()
    work = Path(tempfile.mkdtemp(prefix="jarvis-browser-check-"))
    config = BrowserConfig(download_dir=work / "downloads", screenshot_dir=work / "shots", navigation_timeout_s=30.0, default_timeout_s=10.0, settle_seconds=3.0)

    def factory():
        return PlaywrightBrowserDriver(browser_type="auto", headless=not args.headed, profile_dir=work / "profile", download_dir=config.download_dir, allow_private=False,
                                       nav_timeout_ms=30000, default_timeout_ms=10000)

    engine = BrowserEngine(factory, config, BrowserLog(work / "browser_log.jsonl"))
    tools = BrowserTools(engine)
    print(f"Real browser check ({'headed' if args.headed else 'headless'}), query: {args.query!r}\n")

    def step(name: str, tool: str, arguments: dict | None = None):
        t = time.perf_counter()
        result = tools.call(tool, arguments or {}, session_id="real-check")
        show(name, t, result)
        return result

    try:
        step("open_youtube", "open_youtube")
        found = step("search_youtube", "search_youtube", {"query": args.query})
        for row in (found.data.get("results") or [])[:5]:
            print(f"       {row['n']}. {row['title'][:60]} | {row['channel'][:24]} | official={row['official']} | score={row['score']}")
        played = step("play_youtube (unambiguous only)", "play_youtube", {"query": args.query})
        if played.data.get("ambiguous"):
            print("       ambiguous -> asked:", [c["title"][:40] for c in played.data["candidates"]])
            played = step("play_youtube choice=1", "play_youtube", {"choice": 1})
        if played.success:
            time.sleep(2)
            step("pause_youtube", "pause_youtube")
            step("pause_youtube (idempotent)", "pause_youtube")
            step("resume_youtube", "resume_youtube")
            step("seek_youtube +20s", "seek_youtube", {"seconds": 20})
            step("volume_youtube 30%", "volume_youtube", {"percent": 30})
            step("skip_youtube", "skip_youtube")
        step("close_youtube", "close_youtube")
        step("open_url github.com", "open_url", {"url": "https://github.com/"})
        step("get_page_state", "get_page_state")
        page = step("read_page", "read_page")
        print("       headings:", (page.data.get("headings") or [])[:3], "| injection_suspected:", page.data.get("injection_suspected"))
        step("web_search", "web_search", {"query": "PostgreSQL pgvector"})
        step("open_url file:// (must refuse)", "open_url", {"url": "file:///C:/Windows/win.ini"})
        step("open_url localhost (must refuse)", "open_url", {"url": "http://localhost:8000/"})
        step("close_browser", "close_browser")
        snap = __import__("backend.core.metrics", fromlist=["metrics"]).metrics.snapshot()["timers"]
        print("\nTimings (ms, mean):", {k: v["mean_ms"] for k, v in snap.items() if k.startswith("browser.")})
        print("Recoveries:", engine.recoveries, "Crashes:", engine.crashes)
    finally:
        engine.shutdown()
        shutil.rmtree(work, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
