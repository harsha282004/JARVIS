"""Command-line entry point: `python -m desktop.launcher`.

    python -m desktop.launcher                   run the JARVIS runtime
    python -m desktop.launcher --enable-startup  add a Startup-folder shortcut
    python -m desktop.launcher --disable-startup remove it
    python -m desktop.launcher --startup-status  show whether it is enabled
    python -m desktop.launcher --stop            ask the running JARVIS to exit gracefully
"""

import argparse
import os
import platform
import signal
import sys
import threading
import time

from pydantic import ValidationError

from backend.core.config import Settings, get_settings
from backend.core.logging import configure_logging, get_logger
from backend.core.database import dispose_engine
from desktop.launcher.app import JarvisApplication
from desktop.launcher.single_instance import ExitSignal, SingleInstanceGuard
from desktop.launcher.startup import PROJECT_ROOT, StartupIntegrationError, StartupManager
from desktop.runtime.composition import build_health, build_runtime_services, build_tray_actions
from desktop.runtime.manager import RuntimeManager
from desktop.runtime.periodic import PeriodicTask
from desktop.runtime.power import SleepResumeWatcher
from desktop.tray.tray import TrayController
from voice.bootstrap import build_proactive_engine, build_reminder_scheduler, build_task_system, build_voice_engine

LOG_FILE = PROJECT_ROOT / "logs" / "jarvis.log"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="python -m desktop.launcher", description=__doc__)
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--enable-startup", action="store_true", help="start JARVIS when you log in to Windows")
    group.add_argument("--disable-startup", action="store_true", help="remove the Windows startup shortcut")
    group.add_argument("--startup-status", action="store_true", help="report whether startup is enabled")
    group.add_argument("--stop", action="store_true", help="ask the running JARVIS to shut down gracefully")
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


def _build_application(settings: Settings, exit_event: threading.Event, started_at: float | None = None) -> JarvisApplication:
    logger = get_logger(__name__)
    task_system = build_task_system(settings)
    services = build_runtime_services(settings, task_system, PROJECT_ROOT)
    bus = services.bus
    router = services.intelligence_router
    manager = RuntimeManager(lambda: build_voice_engine(settings, task_system, router, bus, services.voice), start_paused=services.start_paused)
    services.attach_manager(manager)
    tray = None
    if settings.JARVIS_TRAY_ENABLED:
        tray = TrayController(
            manager, exit_event.set, actions=build_tray_actions(services, manager),
            overall=services.health.overall, privacy_mode=lambda: services.privacy.mode,
        )
        services.tray_holder["tray"] = tray
    build_health(services, manager)

    def on_resume() -> None:
        services.handle_resume()
        manager.handle_system_resume()

    power = SleepResumeWatcher(on_resume)
    desktop_send = tray.notify if tray is not None else None
    proactive = build_proactive_engine(settings, task_system, desktop_send)
    scheduler = build_reminder_scheduler(settings, task_system, desktop_send, proactive)

    def health_tick() -> None:
        services.health.check_all()
        if tray is not None:
            tray.refresh()

    manager.add_listener(lambda _status: health_tick())  # the status shown must follow the runtime immediately, not at the next interval
    background: list = [
        PeriodicTask("health", health_tick, settings.JARVIS_HEALTH_INTERVAL_SECONDS),
        PeriodicTask("power", lambda: services.power.poll(), 5.0, run_immediately=False),
        services.supervisor,
    ]
    if services.intelligence_runner is not None:
        background.append(services.intelligence_runner)
    if services.sync_runner is not None:
        background.append(services.sync_runner)
    background.extend(services.periodic)
    try:
        from backend.core.context import AppContext, set_context
        from backend.api.server import ApiServer

        set_context(AppContext(settings=settings, bus=bus, health=services.health, privacy=services.privacy, prefs=services.prefs, audit=services.audit,
                               center=services.center, intelligence=services.intelligence_service, manager=manager, task_system=task_system, hub=services.hub, voice=services.voice, browser=services.browser, autonomy=services.autonomy, operator=services.operator))
        if settings.JARVIS_API_ENABLED:
            background.append(ApiServer(settings.API_HOST, settings.API_PORT))
    except Exception as exc:  # noqa: BLE001 - the local dashboard is optional
        logger.error("Local API could not be prepared (%s)", type(exc).__name__)
    return JarvisApplication(manager, exit_event, tray=tray, power=power, scheduler=scheduler, background=background, bus=bus,
                             cleanup=([services.operator.shutdown] if services.operator is not None else []) + ([services.autonomy.shutdown] if services.autonomy is not None else []) + ([services.browser.shutdown] if services.browser is not None else []) + [dispose_engine], started_at=started_at)


def _log_startup(settings: Settings) -> None:
    """One structured startup record: version facts and non-secret configuration, so a support question can be answered from the log."""
    from backend.core.database import schema_status
    from backend.core.logging import log_event

    log = get_logger(__name__)
    log_event(log, "startup_begin", python=platform.python_version(), os=platform.platform(), env=settings.APP_ENV, offline=settings.JARVIS_OFFLINE_MODE,
              llm=f"{settings.LLM_PROVIDER}:{settings.LLM_MODEL}", intelligence=settings.JARVIS_INTELLIGENCE_ENABLED, tray=settings.JARVIS_TRAY_ENABLED)
    ok, detail = schema_status()
    log_event(log, "database_check", level=20 if ok else 30, ok=ok, detail=detail)


def main(argv: list[str] | None = None) -> int:
    started_at = time.perf_counter()
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
    configure_logging(settings.LOG_LEVEL, LOG_FILE, json_format=settings.JARVIS_LOG_JSON)
    logger = get_logger(__name__)

    if args.stop:
        print("Shutdown requested." if ExitSignal.send() else "JARVIS is not running.")
        return 0
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
    exit_signal = ExitSignal()
    exit_signal.wait_in_thread(exit_event.set)
    for name in ("SIGINT", "SIGBREAK"):
        if hasattr(signal, name):
            signal.signal(getattr(signal, name), lambda *_: exit_event.set())

    try:
        _log_startup(settings)
        return _build_application(settings, exit_event, started_at).run()
    finally:
        exit_signal.close()
        guard.release()
