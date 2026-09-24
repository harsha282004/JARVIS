"""Command-line entry point: `python -m desktop.launcher`.

    python -m desktop.launcher                   run the JARVIS runtime
    python -m desktop.launcher --enable-startup  add a Startup-folder shortcut
    python -m desktop.launcher --disable-startup remove it
    python -m desktop.launcher --startup-status  show whether it is enabled
"""

import argparse
import os
import signal
import sys
import threading

from pydantic import ValidationError

from backend.core.config import Settings, get_settings
from backend.core.logging import configure_logging, get_logger
from desktop.launcher.app import JarvisApplication
from desktop.launcher.single_instance import SingleInstanceGuard
from desktop.launcher.startup import PROJECT_ROOT, StartupIntegrationError, StartupManager
from desktop.runtime.manager import RuntimeManager
from desktop.runtime.power import SleepResumeWatcher
from desktop.tray.tray import TrayController
from voice.bootstrap import build_reminder_scheduler, build_task_system, build_voice_engine

LOG_FILE = PROJECT_ROOT / "logs" / "jarvis.log"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="python -m desktop.launcher", description=__doc__)
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--enable-startup", action="store_true", help="start JARVIS when you log in to Windows")
    group.add_argument("--disable-startup", action="store_true", help="remove the Windows startup shortcut")
    group.add_argument("--startup-status", action="store_true", help="report whether startup is enabled")
    return parser.parse_args(argv)


def _run_startup_command(args: argparse.Namespace, startup: StartupManager) -> int:
    try:
        if args.enable_startup:
            print(f"Startup enabled: {startup.enable()}")
        elif args.disable_startup:
            print("Startup disabled." if startup.disable() else "Startup was not enabled.")
        else:
            print("Startup is enabled." if startup.is_enabled() else "Startup is disabled.")
    except StartupIntegrationError as exc:
        print(f"Startup error: {exc}")
        return 1
    return 0


def _build_application(settings: Settings, exit_event: threading.Event) -> JarvisApplication:
    task_system = build_task_system(settings)
    manager = RuntimeManager(lambda: build_voice_engine(settings, task_system))
    tray = TrayController(manager, exit_event.set) if settings.JARVIS_TRAY_ENABLED else None
    power = SleepResumeWatcher(manager.handle_system_resume)
    scheduler = build_reminder_scheduler(settings, task_system, tray.notify if tray is not None else None)
    return JarvisApplication(manager, exit_event, tray=tray, power=power, scheduler=scheduler)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    # pythonw.exe has no console: give libraries a harmless stdout/stderr.
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w")
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w")

    # .env and model paths in it are relative to the project root.
    os.chdir(PROJECT_ROOT)

    try:
        settings = get_settings()
    except ValidationError as exc:
        configure_logging("INFO", LOG_FILE)
        get_logger(__name__).error("Configuration invalid: %s", exc)
        return 2
    configure_logging(settings.LOG_LEVEL, LOG_FILE)
    logger = get_logger(__name__)

    if args.enable_startup or args.disable_startup or args.startup_status:
        return _run_startup_command(args, StartupManager())

    logger.info("JARVIS starting (configuration loaded)")
    if not settings.JARVIS_RUNTIME_ENABLED:
        logger.info("JARVIS_RUNTIME_ENABLED is false; exiting")
        return 0

    guard = SingleInstanceGuard()
    if not guard.acquire():
        logger.error("Another JARVIS runtime is already running; exiting")
        return 3

    exit_event = threading.Event()
    for name in ("SIGINT", "SIGBREAK"):
        if hasattr(signal, name):
            signal.signal(getattr(signal, name), lambda *_: exit_event.set())

    try:
        return _build_application(settings, exit_event).run()
    finally:
        guard.release()
