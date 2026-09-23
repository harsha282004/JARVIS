"""Optional, user-level "start with Windows" integration.

Creates/removes a shortcut in the current user's Startup folder — no
administrator rights, no registry edits. It is only ever changed by an
explicit `--enable-startup` / `--disable-startup` command; launching JARVIS
never touches it. Paths are resolved at runtime (APPDATA, the running
interpreter, this project's location), never hardcoded.
"""

import os
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

from backend.core.logging import get_logger

logger = get_logger(__name__)

SHORTCUT_NAME = "JARVIS.lnk"
PROJECT_ROOT = Path(__file__).resolve().parents[2]

# Values are passed through the environment, never interpolated into the
# script, so paths containing quotes/spaces cannot break or inject commands.
_CREATE_SHORTCUT_SCRIPT = (
    "$s = (New-Object -ComObject WScript.Shell).CreateShortcut($env:JARVIS_LNK); "
    "$s.TargetPath = $env:JARVIS_TARGET; "
    "$s.Arguments = $env:JARVIS_ARGS; "
    "$s.WorkingDirectory = $env:JARVIS_CWD; "
    "$s.Description = 'JARVIS voice assistant'; "
    "$s.Save()"
)

Runner = Callable[[list[str], dict[str, str]], None]


class StartupIntegrationError(Exception):
    """Raised when the startup shortcut cannot be created or removed."""


def _run_powershell(command: list[str], env: dict[str, str]) -> None:
    result = subprocess.run(command, env=env, capture_output=True, text=True, timeout=30)
    if result.returncode != 0:
        raise StartupIntegrationError(
            f"PowerShell failed to create the shortcut: {result.stderr.strip()}"
        )


def default_startup_dir() -> Path:
    appdata = os.environ.get("APPDATA")
    if not appdata:
        raise StartupIntegrationError("APPDATA is not set; cannot locate the Startup folder")
    return Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"


def windowless_python() -> Path:
    """pythonw.exe next to the running interpreter (no console window), if present."""
    python = Path(sys.executable)
    pythonw = python.with_name("pythonw.exe")
    return pythonw if pythonw.is_file() else python


class StartupManager:
    def __init__(
        self,
        startup_dir: Path | None = None,
        project_root: Path = PROJECT_ROOT,
        target: Path | None = None,
        runner: Runner = _run_powershell,
    ):
        self._startup_dir = startup_dir
        self._project_root = project_root
        self._target = target
        self._runner = runner

    @property
    def shortcut_path(self) -> Path:
        return (self._startup_dir or default_startup_dir()) / SHORTCUT_NAME

    def is_enabled(self) -> bool:
        return self.shortcut_path.is_file()

    def enable(self) -> Path:
        shortcut = self.shortcut_path
        shortcut.parent.mkdir(parents=True, exist_ok=True)
        env = {
            **os.environ,
            "JARVIS_LNK": str(shortcut),
            "JARVIS_TARGET": str(self._target or windowless_python()),
            "JARVIS_ARGS": "-m desktop.launcher",
            "JARVIS_CWD": str(self._project_root),
        }
        self._runner(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", _CREATE_SHORTCUT_SCRIPT],
            env,
        )
        if not shortcut.is_file():
            raise StartupIntegrationError(f"Shortcut was not created at {shortcut}")
        logger.info("Startup integration enabled: %s", shortcut)
        return shortcut

    def disable(self) -> bool:
        """Remove the shortcut. Returns False if there was nothing to remove."""
        shortcut = self.shortcut_path
        if not shortcut.is_file():
            logger.info("Startup integration already disabled")
            return False
        shortcut.unlink()
        logger.info("Startup integration disabled: %s", shortcut)
        return True
