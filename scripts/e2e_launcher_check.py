#!/usr/bin/env python
"""Real end-to-end check of the Windows runtime: launch JARVIS as a separate process, watch it come up through the local API, then stop it
gracefully and verify it exited cleanly.

    python scripts/e2e_launcher_check.py [--privacy private|active] [--seconds 20] [--no-tray]

It uses a throw-away state directory and a throw-away SQLite database (your real database, .env secrets and integrations are not used
for writing). With --privacy private (the default) the microphone is never opened. With --privacy active the real wake-word listener
starts, so the microphone opens for the duration of the run. Prints a JSON report; exit code 0 only if every check passed.
"""

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def http(port: int, path: str, token: str = "", timeout: float = 20.0):
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", headers={"X-JARVIS-Token": token} if token else {})
    with urllib.request.urlopen(req, timeout=timeout) as response:  # noqa: S310 - loopback only
        return response.read().decode("utf-8")


def process_usage(pid: int) -> tuple[float, float] | None:
    """(cpu_seconds, working_set_mb) of a process via PowerShell (no extra dependency), or None."""
    try:
        # the venv's python.exe is a thin launcher that starts the real interpreter as a child: measure the heaviest of the pair
        script = (f"$ids = @({pid}) + @(Get-CimInstance Win32_Process -Filter 'ParentProcessId={pid}' | ForEach-Object {{ $_.ProcessId }}); "
                  "Get-Process -Id $ids -ErrorAction SilentlyContinue | Sort-Object WorkingSet64 -Descending | Select-Object -First 1 CPU, WorkingSet64 | ConvertTo-Json")
        out = subprocess.run(["powershell", "-NoProfile", "-Command", script],
                             capture_output=True, text=True, timeout=20).stdout
        data = json.loads(out)
        return float(data["CPU"]), float(data["WorkingSet64"]) / (1024 * 1024)
    except Exception:  # noqa: BLE001
        return None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--privacy", default="private", choices=["private", "active"])
    parser.add_argument("--seconds", type=float, default=20.0, help="how long to let it run before stopping")
    parser.add_argument("--port", type=int, default=8791)
    parser.add_argument("--no-tray", action="store_true")
    args = parser.parse_args()

    work = Path(tempfile.mkdtemp(prefix="jarvis-e2e-"))
    db = work / "jarvis.db"
    sys.path.insert(0, str(ROOT))
    os.environ.update({"DATABASE_URL": f"sqlite:///{db.as_posix()}"})
    from sqlalchemy import create_engine

    from backend.models.base import Base
    import backend.models.events, backend.models.knowledge_graph, backend.models.memory, backend.models.proactive, backend.models.rag, backend.models.tasks  # noqa: E401,F401

    engine = create_engine(os.environ["DATABASE_URL"])
    Base.metadata.create_all(engine)
    engine.dispose()

    env = {**os.environ, "DATABASE_URL": f"sqlite:///{db.as_posix()}", "JARVIS_STATE_DIR": str(work / "state"), "API_PORT": str(args.port),
           "JARVIS_PRIVACY_DEFAULT": args.privacy, "JARVIS_TRAY_ENABLED": "false" if args.no_tray else "true", "JARVIS_LOG_JSON": "true",
           "JARVIS_GMAIL_ENABLED": "false", "JARVIS_CALENDAR_ENABLED": "false", "JARVIS_MESSAGING_ENABLED": "false", "JARVIS_PROACTIVE_ENABLED": "false"}
    started = time.perf_counter()
    log_path = work / "console.log"
    log_handle = log_path.open("wb")  # a file, not a pipe: an unread pipe fills up and blocks the process
    proc = subprocess.Popen([sys.executable, "-m", "desktop.launcher"], cwd=ROOT, env=env, stdout=log_handle, stderr=subprocess.STDOUT,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    report: dict = {"privacy": args.privacy, "checks": {}}
    token = ""
    try:
        deadline = time.time() + 120
        while time.time() < deadline and proc.poll() is None:
            try:
                page = http(args.port, "/dashboard")
                token = re.search(r'const TOKEN = "([^"]+)"', page).group(1)
                break
            except Exception:  # noqa: BLE001 - not up yet
                time.sleep(0.5)
        report["checks"]["api_up"] = bool(token)
        report["api_up_seconds"] = round(time.perf_counter() - started, 1)
        status = {}
        while token and time.time() < deadline and proc.poll() is None:
            status = json.loads(http(args.port, "/status", token))
            if status["runtime"] in ("running", "paused", "error"):
                break
            time.sleep(0.5)
        report["runtime_ready_seconds"] = round(time.perf_counter() - started, 1)
        report["status"] = status
        before, t0 = process_usage(proc.pid), time.time()
        time.sleep(max(0.0, args.seconds))
        after = process_usage(proc.pid)
        if before and after and time.time() > t0:
            cores = os.cpu_count() or 1
            report["idle_cpu_percent_of_machine"] = round(100 * (after[0] - before[0]) / (time.time() - t0) / cores, 2)
            report["idle_ram_mb"] = round(after[1])
        if token and proc.poll() is None:
            status = json.loads(http(args.port, "/status", token))
            report["status_after"] = status
            health = json.loads(http(args.port, "/health/services", token))
            report["health"] = {s["name"]: f'{s["state"]} ({s["detail"]})' for s in health["services"]}
            report["overall"] = health["overall"]
            report["metrics"] = json.loads(http(args.port, "/metrics", token))
            report["integrations"] = {i["name"]: f'{i["status"]} ({i["detail"]})' for i in json.loads(http(args.port, "/integrations", token))["integrations"]}
            report["intelligence_sources"] = json.loads(http(args.port, "/intelligence/summary", token)).get("sources")
            expected_mic = args.privacy == "active"
            report["checks"]["microphone_matches_privacy"] = (status["microphone_active"] or status["voice_state"] in ("speaking", "thinking")) == expected_mic if expected_mic else not status["microphone_active"]
            report["checks"]["voice_indicator_truthful"] = status["voice_indicator"] in (("listening", "processing", "speaking") if expected_mic else ("microphone_disabled",))
    finally:
        stop = subprocess.run([sys.executable, "-m", "desktop.launcher", "--stop"], cwd=ROOT, env=env, capture_output=True, text=True, timeout=30)
        report["stop_request"] = stop.stdout.strip()
        try:
            code = proc.wait(timeout=40)
        except subprocess.TimeoutExpired:
            proc.kill()
            code = "killed (did not stop gracefully)"
        report["exit_code"] = code
        report["checks"]["graceful_exit"] = code == 0
        log_handle.close()
        output = log_path.read_text(encoding="utf-8", errors="replace")
        report["log_lines"] = len(output.splitlines())
        report["errors_in_log"] = [line[:200] for line in output.splitlines() if '"severity": "ERROR"' in line or '"severity": "CRITICAL"' in line][:10]
        try:
            http(args.port, "/health", timeout=2)
            report["checks"]["port_released"] = False
        except Exception:  # noqa: BLE001
            report["checks"]["port_released"] = True
    print(json.dumps(report, indent=2, default=str))
    return 0 if all(report["checks"].values()) else 1


if __name__ == "__main__":
    sys.exit(main())
