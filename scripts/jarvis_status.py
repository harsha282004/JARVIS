"""Is JARVIS running, and is every part of it healthy? Safe to run repeatedly: it only reads (a process probe, the start-with-Windows registration and the local API);
it never starts, stops or changes anything, and prints no secret.

    python scripts/jarvis_status.py            human readable
    python scripts/jarvis_status.py --json     machine readable (exit code 0 = running, 1 = stopped)
    python scripts/jarvis_status.py --port 8000

Reports: process RUNNING/STOPPED with PID and parent PID, how it was started (windows/manual/tray), start-with-Windows registration and the exact command Windows runs, and,
when running: runtime state, tray, API, voice state, microphone, wake listener, database, language model (Groq) and the last shutdown reason from the log.
"""

import argparse
import json
import re
import subprocess
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def _get(port: int, path: str, token: str = "", timeout: float = 8.0):
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", headers={"X-JARVIS-Token": token} if token else {})
    with urllib.request.urlopen(req, timeout=timeout) as response:  # noqa: S310 - loopback only
        return response.read().decode("utf-8")


def jarvis_processes() -> list[dict]:
    """The python/pythonw processes running the JARVIS launcher, with parent PIDs (PowerShell/CIM; empty off Windows or on error)."""
    if sys.platform != "win32":
        return []
    script = ("Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -match 'jarvis_launcher\\.pyw|desktop\\.launcher' -and $_.CommandLine -notmatch "
              "'jarvis_status' -and $_.Name -match '^pythonw?\\.exe$' } | Select-Object ProcessId,ParentProcessId,Name,CreationDate | ConvertTo-Json -Compress")
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-Command", script], capture_output=True, text=True, timeout=30).stdout.strip()
        data = json.loads(out) if out else []
    except Exception:  # noqa: BLE001
        return []
    data = [data] if isinstance(data, dict) else data
    return [{"pid": d["ProcessId"], "parent_pid": d["ParentProcessId"], "name": d["Name"]} for d in data]


def last_shutdown_reason() -> str | None:
    log = ROOT / "logs" / "jarvis.log"
    try:
        for line in reversed(log.read_text(encoding="utf-8", errors="ignore").splitlines()[-4000:]):
            m = re.search(r"SHUTDOWN_REASON=(\w+)(?: DETAIL=(.*?))?(?:\"|$)", line)
            if m:
                return m.group(1) + (f" ({m.group(2).strip()})" if m.group(2) else "")
    except OSError:
        pass
    return None


def collect(port: int | None) -> dict:
    from backend.core.config import get_settings
    from desktop.launcher.single_instance import SingleInstanceGuard
    from desktop.launcher.startup import StartupManager

    settings = get_settings()
    port = port or settings.API_PORT
    running = SingleInstanceGuard.is_running()
    procs = jarvis_processes() if running else []
    info: dict = {"process": "RUNNING" if running else "STOPPED", "project_root": str(ROOT), "processes": procs, "pid": procs[0]["pid"] if procs else None,
                  "parent_pid": procs[0]["parent_pid"] if procs else None, "api_port": port, "last_shutdown_reason": last_shutdown_reason()}
    try:
        info["startup"] = StartupManager().status()
    except Exception as exc:  # noqa: BLE001
        info["startup"] = {"error": type(exc).__name__}
    if not running:
        return info
    try:
        page = _get(port, "/dashboard")
        token = (re.search(r'const TOKEN = "([^"]+)"', page) or [None, ""])[1]
        info["api"] = "ACTIVE"
        status = json.loads(_get(port, "/status", token))
        proc = status.get("process") or {}
        info.update({"runtime_state": status.get("runtime"), "tray": proc.get("tray"), "tray_health": proc.get("tray_health"), "startup_source": proc.get("startup_source"), "voice_state": status.get("voice_state"),
                     "microphone_active": status.get("microphone_active"), "voice_indicator": status.get("voice_indicator_text"), "overall_health": status.get("overall"),
                     "last_error": status.get("last_error")})
        health = {s["name"]: f'{s["state"]} ({s["detail"]})' for s in json.loads(_get(port, "/health/services", token))["services"]}
        info["database"], info["llm"], info["stt"], info["tts"] = health.get("database"), health.get("llm"), health.get("stt"), health.get("tts")
        voice = json.loads(_get(port, "/voice", token))
        info["microphone"] = voice.get("microphone")
        info["wake_listener"] = ("waiting for the wake phrase" if status.get("microphone_active") else "not listening") + f" (ready={voice['wake_word']['ready']}, health={voice['wake_word']['health']})"
        info["session"] = voice.get("conversation", {}).get("session")
    except Exception as exc:  # noqa: BLE001 - a process without a reachable API is reported as such, not as a failure of this script
        info["api"] = f"NOT REACHABLE ({type(exc).__name__})"
    return info


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--port", type=int, default=None)
    args = ap.parse_args()
    info = collect(args.port)
    if args.json:
        print(json.dumps(info, indent=2, default=str))
    else:
        print(f"JARVIS process: {info['process']}   PID: {info.get('pid')}   Parent PID: {info.get('parent_pid')}   started by: {info.get('startup_source', '-')}")
        for label, key in (("Runtime state", "runtime_state"), ("Tray controller", "tray"), ("API", "api"), ("Voice", "voice_state"), ("Microphone", "microphone"), ("Wake listener", "wake_listener"),
                           ("Voice session", "session"), ("Database", "database"), ("Groq / LLM", "llm"), ("Speech recognition", "stt"), ("Speech output", "tts"), ("Overall", "overall_health")):
            if key in info:
                print(f"{label + ':':20} {info[key]}")
        th = info.get("tray_health")
        if th:
            # "Tray controller: running" only ever meant "pystray's call did not raise" -- this line is the real
            # signal: whether Windows itself has actually registered the icon (Shell_NotifyIconGetRect), which is
            # the strongest state this script can verify without visually inspecting the notification area itself.
            registered = th.get("icon_registered")
            reg_text = "YES (confirmed with Windows)" if registered is True else "NO" if registered is False else "unknown (not Windows, or not yet checked)"
            print(f"{'Tray icon window:':20} {'created' if th.get('icon_created') else 'not created'}  (thread alive: {th.get('thread_alive')})")
            print(f"{'Tray icon registered:':20} {reg_text}" + (f"  -- {th['last_error']}" if th.get("last_error") else ""))
        st = info.get("startup", {})
        print(f"{'Start with Windows:':20} {', '.join(st['methods']) if st.get('methods') else 'disabled'}" + ("  (WARNING: more than one mechanism)" if st.get("duplicate") else ""))
        if st.get("command"):
            print(f"{'Windows runs:':20} {st['command']}")
        print(f"{'Last shutdown:':20} {info.get('last_shutdown_reason') or 'none recorded'}")
    return 0 if info["process"] == "RUNNING" else 1


if __name__ == "__main__":
    sys.exit(main())
