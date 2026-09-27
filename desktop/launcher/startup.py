"""Optional, user-level "start with Windows" integration (no administrator rights, nothing outside the current user's account).

Three mechanisms, exactly ONE of which is active at a time (enabling one removes the others, so Windows can never start two JARVIS runtimes):

  task      a per-user Task Scheduler task that runs at logon (default; the reliable choice):
              * has a real working directory and starts the correct interpreter explicitly (a Run entry cannot set a working directory);
              * waits ~20 s after logon so the audio subsystem, the taskbar and the network are up (the voice engine also retries on its own);
              * `MultipleInstances=IgnoreNew`: Windows itself will not start a second copy;
              * restarts JARVIS (5 times, 1 minute apart) if the process ends with a failure code - a deliberate exit (tray Exit, `--stop`, Windows shutdown) exits with 0
                and is never restarted; a duplicate start exits with 0 as well;
              * does not depend on Explorer, VS Code, a console or a terminal.
  run       an entry in HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Run (fallback when Task Scheduler is unavailable, e.g. by policy).
  shortcut  a shortcut in the user's Startup folder (the original mechanism; kept for compatibility).

Every mechanism launches `<project>\\.venv\\Scripts\\pythonw.exe <project>\\scripts\\windows\\jarvis_launcher.pyw --startup-source windows`, which sets its own
working directory and import path, so none of them depends on the directory Windows starts the process in. It is only ever changed by an explicit
`--enable-startup` / `--disable-startup`; launching JARVIS never touches it. Paths are resolved at run time, never hard-coded.
"""

import os
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

from backend.core.logging import get_logger

logger = get_logger(__name__)

SHORTCUT_NAME = "JARVIS.lnk"
TASK_NAME = "JARVIS"
RUN_VALUE = "JARVIS"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
PROJECT_ROOT = Path(__file__).resolve().parents[2]
LAUNCHER_SCRIPT = PROJECT_ROOT / "scripts" / "windows" / "jarvis_launcher.pyw"
METHODS = ("task", "run", "shortcut")

# Values are passed through the environment, never interpolated into the script, so paths containing quotes/spaces cannot break or inject commands.
_CREATE_SHORTCUT_SCRIPT = (
    "$s = (New-Object -ComObject WScript.Shell).CreateShortcut($env:JARVIS_LNK); "
    "$s.TargetPath = $env:JARVIS_TARGET; "
    "$s.Arguments = $env:JARVIS_ARGS; "
    "$s.WorkingDirectory = $env:JARVIS_CWD; "
    "$s.Description = 'JARVIS voice assistant'; "
    "$s.Save()"
)

_CREATE_TASK_SCRIPT = (
    "$ErrorActionPreference = 'Stop'; "
    "$a = New-ScheduledTaskAction -Execute $env:JARVIS_TASK_EXE -Argument $env:JARVIS_TASK_ARGS -WorkingDirectory $env:JARVIS_TASK_CWD; "
    "$t = New-ScheduledTaskTrigger -AtLogOn -User $env:JARVIS_TASK_USER; "
    "$t.Delay = 'PT20S'; "
    "$s = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -MultipleInstances IgnoreNew "
    "-RestartCount 5 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit (New-TimeSpan -Seconds 0); "
    "$p = New-ScheduledTaskPrincipal -UserId $env:JARVIS_TASK_USER -LogonType Interactive -RunLevel Limited; "
    "Register-ScheduledTask -TaskName $env:JARVIS_TASK_NAME -Action $a -Trigger $t -Settings $s -Principal $p "
    "-Description 'JARVIS personal assistant (starts at logon, restarts if it fails)' -Force | Out-Null"
)
_REMOVE_TASK_SCRIPT = "Unregister-ScheduledTask -TaskName $env:JARVIS_TASK_NAME -Confirm:$false -ErrorAction Stop"
_QUERY_TASK_SCRIPT = "$t = Get-ScheduledTask -TaskName $env:JARVIS_TASK_NAME -ErrorAction SilentlyContinue; if ($t) { 'yes' } else { 'no' }"

Runner = Callable[[list[str], dict[str, str]], None]


class StartupIntegrationError(Exception):
    """Raised when start-with-Windows cannot be created, removed or read."""


def _run_powershell(command: list[str], env: dict[str, str]) -> None:
    result = subprocess.run(command, env=env, capture_output=True, text=True, timeout=60)
    if result.returncode != 0:
        raise StartupIntegrationError(f"PowerShell failed: {result.stderr.strip()[:300]}")


def _query_powershell(command: list[str], env: dict[str, str]) -> str:
    result = subprocess.run(command, env=env, capture_output=True, text=True, timeout=60)
    return result.stdout.strip()


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


def project_pythonw(project_root: Path = PROJECT_ROOT) -> Path:
    """The project's OWN interpreter (.venv), never whatever `python` happens to be on PATH; falls back to the running interpreter."""
    venv = project_root / ".venv" / "Scripts" / "pythonw.exe"
    return venv if venv.is_file() else windowless_python()


class _WinRegistry:
    """HKCU Run access (injectable so tests can use a scratch key)."""

    def __init__(self, key_path: str = RUN_KEY):
        self.key_path = key_path

    def get(self, name: str) -> str | None:
        import winreg

        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, self.key_path, 0, winreg.KEY_READ) as key:
                return str(winreg.QueryValueEx(key, name)[0])
        except FileNotFoundError:
            return None

    def set(self, name: str, value: str) -> None:
        import winreg

        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, self.key_path, 0, winreg.KEY_SET_VALUE) as key:
            winreg.SetValueEx(key, name, 0, winreg.REG_SZ, value)

    def delete(self, name: str) -> bool:
        import winreg

        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, self.key_path, 0, winreg.KEY_SET_VALUE) as key:
                winreg.DeleteValue(key, name)
            return True
        except FileNotFoundError:
            return False


class StartupManager:
    def __init__(
        self,
        startup_dir: Path | None = None,
        project_root: Path = PROJECT_ROOT,
        target: Path | None = None,
        runner: Runner = _run_powershell,
        *,
        registry=None,
        query: Callable[[list[str], dict[str, str]], str] = _query_powershell,
        task_name: str = TASK_NAME,
        run_value: str = RUN_VALUE,
    ):
        self._startup_dir = startup_dir
        self._project_root = project_root
        self._target = target
        self._runner = runner
        self._registry = registry if registry is not None else _WinRegistry()
        self._query = query
        self._task_name = task_name
        self._run_value = run_value

    # ---- what Windows will run ----------------------------------------------------------------------------------------------------------

    @property
    def interpreter(self) -> Path:
        return self._target or project_pythonw(self._project_root)

    @property
    def launcher_script(self) -> Path:
        return self._project_root / "scripts" / "windows" / "jarvis_launcher.pyw"

    def arguments(self) -> str:
        return f'"{self.launcher_script}" --startup-source windows'

    def command_line(self) -> str:
        return f'"{self.interpreter}" {self.arguments()}'

    @property
    def shortcut_path(self) -> Path:
        return (self._startup_dir or default_startup_dir()) / SHORTCUT_NAME

    def _ps(self, script: str) -> list[str]:
        return ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", script]

    def _task_env(self) -> dict[str, str]:
        return {**os.environ, "JARVIS_TASK_NAME": self._task_name, "JARVIS_TASK_EXE": str(self.interpreter), "JARVIS_TASK_ARGS": self.arguments(),
                "JARVIS_TASK_CWD": str(self._project_root), "JARVIS_TASK_USER": os.environ.get("USERDOMAIN", "") + "\\" + os.environ.get("USERNAME", "") if os.environ.get("USERDOMAIN")
                else os.environ.get("USERNAME", "")}

    # ---- status -------------------------------------------------------------------------------------------------------------------------

    def task_enabled(self) -> bool:
        try:
            return self._query(self._ps(_QUERY_TASK_SCRIPT), self._task_env()) == "yes"
        except Exception:  # noqa: BLE001 - "cannot tell" is reported as not enabled, never as a crash
            return False

    def run_enabled(self) -> bool:
        try:
            return self._registry.get(self._run_value) is not None
        except Exception:  # noqa: BLE001
            return False

    def shortcut_enabled(self) -> bool:
        try:
            return self.shortcut_path.is_file()
        except StartupIntegrationError:
            return False

    def status(self) -> dict:
        active = {"task": self.task_enabled(), "run": self.run_enabled(), "shortcut": self.shortcut_enabled()}
        on = [name for name, enabled in active.items() if enabled]
        return {**active, "enabled": bool(on), "methods": on, "duplicate": len(on) > 1, "command": self.command_line()}

    def is_enabled(self) -> bool:
        return bool(self.status()["enabled"])

    # ---- enable / disable ----------------------------------------------------------------------------------------------------------------

    def enable(self, method: str = "shortcut", *, exclusive: bool = False):
        """Enable one mechanism. With `exclusive=True` (what `--enable-startup` uses) every other mechanism is removed first, so Windows can never start two runtimes.
        Returns the shortcut Path for `shortcut` (unchanged public behaviour) and a short description for `task` / `run`."""
        if method not in METHODS:
            raise StartupIntegrationError(f"unknown startup method '{method}' (use one of: {', '.join(METHODS)})")
        if exclusive:
            self.disable()
        if method == "task":
            self._runner(self._ps(_CREATE_TASK_SCRIPT), self._task_env())
            if not self.task_enabled():
                raise StartupIntegrationError("The scheduled task was not created")
            logger.info("Startup integration enabled: Task Scheduler task '%s'", self._task_name)
            return f"Task Scheduler task '{self._task_name}' (at logon, +20 s, restarts on failure)"
        if method == "run":
            self._registry.set(self._run_value, self.command_line())
            if not self.run_enabled():
                raise StartupIntegrationError("The Run entry was not created")
            logger.info("Startup integration enabled: HKCU Run entry '%s'", self._run_value)
            return f"HKCU Run entry '{self._run_value}'"
        shortcut = self.shortcut_path
        shortcut.parent.mkdir(parents=True, exist_ok=True)
        env = {**os.environ, "JARVIS_LNK": str(shortcut), "JARVIS_TARGET": str(self.interpreter), "JARVIS_ARGS": self.arguments(), "JARVIS_CWD": str(self._project_root)}
        self._runner(self._ps(_CREATE_SHORTCUT_SCRIPT), env)
        if not shortcut.is_file():
            raise StartupIntegrationError(f"Shortcut was not created at {shortcut}")
        logger.info("Startup integration enabled: %s", shortcut)
        return shortcut

    def disable(self) -> bool:
        """Remove every start-with-Windows mechanism. Returns False if there was nothing to remove."""
        removed = False
        if self.shortcut_enabled():
            self.shortcut_path.unlink()
            removed = True
            logger.info("Startup shortcut removed")
        try:
            if self._registry.delete(self._run_value):
                removed = True
                logger.info("Startup Run entry removed")
        except Exception:  # noqa: BLE001
            pass
        if self.task_enabled():
            try:
                self._runner(self._ps(_REMOVE_TASK_SCRIPT), self._task_env())
                removed = True
                logger.info("Startup scheduled task removed")
            except StartupIntegrationError as exc:
                logger.warning("Could not remove the scheduled task: %s", exc)
        if not removed:
            logger.info("Startup integration already disabled")
        return removed
