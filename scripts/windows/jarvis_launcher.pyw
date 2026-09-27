r"""JARVIS launcher for Windows to run (Task Scheduler, Run entry, Startup folder, start_jarvis.ps1).

Unlike `python -m desktop.launcher` this works from ANY working directory: it does not need to be started inside the project, in VS Code, or in an activated
virtual environment. Start it with the project's own interpreter:
    <project>\.venv\Scripts\pythonw.exe <project>\scripts\windows\jarvis_launcher.pyw [--startup-source windows]
Arguments are passed through (--startup-source, --self-check, --stop, --status, ...).
"""

import os
import sys
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
try:
    sys.path.insert(0, str(ROOT))
    os.chdir(ROOT)
    from desktop.launcher.entry import run

    code = run()
except BaseException as exc:  # noqa: BLE001 - even an import failure must leave a trace (a windowless process has no console)
    code = 1
    try:
        log = ROOT / "logs" / "jarvis-crash.log"
        log.parent.mkdir(parents=True, exist_ok=True)
        with log.open("a", encoding="utf-8") as handle:
            handle.write(f"=== {time.strftime('%Y-%m-%d %H:%M:%S')} launcher_import_failure pid={os.getpid()} exe={sys.executable} cwd={os.getcwd()}\n"
                         + "".join(traceback.format_exception(type(exc), exc, exc.__traceback__)))
    except OSError:
        pass
sys.exit(code)
