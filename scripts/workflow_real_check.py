"""REAL_WORLD check of the Personal Operator against a RUNNING JARVIS with your real accounts (read-only).

    python -m desktop.launcher            # in another terminal, with Gmail / Calendar / GitHub connected
    python scripts/workflow_real_check.py [--port 8790]

It talks to the local dashboard API (loopback only; the per-run token is read from the dashboard page exactly as the browser does) and runs READ-ONLY workflows: the morning
briefing, important-email review, tomorrow's meeting preparation, upcoming deadlines and a GitHub activity review WITHOUT adding tasks. It never creates a task or a reminder,
never opens the browser, never submits anything. Every system that is not connected is reported as NOT VERIFIED; nothing is invented. It asserts nothing (your data changes);
the printed table is the evidence. Exit code 0 if the script ran to the end.
"""

import argparse
import json
import re
import sys
import time
import urllib.request

READ_ONLY_GOALS = [
    ("Give me my morning briefing.", {"tasks"}),
    ("Check my important emails and tell me what needs attention today.", {"gmail"}),
    ("Prepare for tomorrow's meetings.", {"calendar"}),
    ("Check my upcoming deadlines and tell me what I need to finish this week.", {"tasks"}),
    ("Find my GitHub activity from this week.", {"github"}),
]


def call(port: int, path: str, token: str = "", body: dict | None = None, timeout: float = 120.0):
    headers = {"X-JARVIS-Token": token} if token else {}
    data = None
    if body is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(body).encode()
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=data, headers=headers, method="POST" if body is not None else "GET")
    with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310 - loopback only
        return r.read().decode("utf-8")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8790)
    args = ap.parse_args()
    try:
        page = call(args.port, "/dashboard")
    except Exception as exc:  # noqa: BLE001
        print(f"JARVIS is not running on port {args.port} ({type(exc).__name__}). Start it with: python -m desktop.launcher")
        return 2
    token = re.search(r'const TOKEN = "([^"]+)"', page)
    if token is None:
        print("could not read the dashboard token")
        return 2
    token = token.group(1)
    snap = json.loads(call(args.port, "/workflows", token))
    print("connected systems:", {k: ("connected" if v else "NOT CONNECTED") for k, v in snap["systems"].items()})
    print(f"{'goal':70} {'result':22} seconds")
    for goal, needs in READ_ONLY_GOALS:
        missing = [n for n in needs if not snap["systems"].get(n)]
        if missing:
            print(f"{goal[:70]:70} {'NOT VERIFIED':22} -    ({', '.join(missing)} not connected)")
            continue
        began = time.time()
        out = json.loads(call(args.port, "/workflows", token, {"goal": goal}))
        wf = out.get("workflow") or {}
        deadline = time.time() + 90
        while wf and wf["status"] in ("RUNNING", "READY", "PLANNING") and time.time() < deadline:
            time.sleep(1)
            wf = json.loads(call(args.port, f"/workflows/{wf['workflow_id']}", token))
        status = wf.get("status", "NOT STARTED")
        print(f"{goal[:70]:70} {status:22} {time.time() - began:5.1f}")
        print("   ->", (wf.get("result") or wf.get("failure") or out.get("message") or "")[:400])
    return 0


if __name__ == "__main__":
    sys.exit(main())
