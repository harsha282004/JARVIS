"""Command-line entry point: `python -m desktop.launcher` (from the project folder) or, from anywhere, the project's own interpreter running
`scripts/windows/jarvis_launcher.pyw`.

    ... --startup-source windows|manual|tray    who started this run (recorded in the startup log)
    ... --enable-startup [--startup-method task|run|shortcut]
                                                start JARVIS when you log in to Windows (default: a Task Scheduler task; exactly one mechanism is ever active)
    ... --disable-startup                       remove every start-with-Windows mechanism
    ... --startup-status                        show which mechanism is enabled and the exact command Windows runs
    ... --status                                is a JARVIS runtime running right now?
    ... --self-check                            verify configuration and paths WITHOUT starting anything (safe from any working directory)
    ... --stop                                  ask the running JARVIS to exit gracefully
"""

import argparse
import os
import platform
import signal
import sys
import threading
import time
from pathlib import Path

from pydantic import ValidationError

from backend.core.config import Settings, get_settings
from backend.core.logging import configure_logging, get_logger
from backend.core.database import dispose_engine
from desktop.launcher.app import ExitCoordinator, JarvisApplication
from desktop.launcher.session import SessionEndWatcher, install_console_handler
from desktop.launcher.single_instance import ExitSignal, SingleInstanceGuard
from desktop.launcher.startup import PROJECT_ROOT, StartupIntegrationError, StartupManager
from desktop.runtime.composition import build_health, build_runtime_services, build_tray_actions
from desktop.runtime.manager import RuntimeManager
from desktop.runtime.periodic import PeriodicTask
from desktop.runtime.power import SleepResumeWatcher
from desktop.tray.tray import TrayController
from voice.bootstrap import build_conversation_engine, build_proactive_engine, build_reminder_scheduler, build_task_system, build_voice_engine

LOG_FILE = PROJECT_ROOT / "logs" / "jarvis.log"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="python -m desktop.launcher", description=__doc__)
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--enable-startup", action="store_true", help="start JARVIS when you log in to Windows")
    group.add_argument("--disable-startup", action="store_true", help="remove the Windows startup shortcut")
    group.add_argument("--startup-status", action="store_true", help="report whether startup is enabled")
    group.add_argument("--stop", action="store_true", help="ask the running JARVIS to shut down gracefully")
    group.add_argument("--status", action="store_true", help="report whether a JARVIS runtime is running")
    group.add_argument("--self-check", action="store_true", help="verify configuration and paths without starting anything")
    parser.add_argument("--startup-source", default="manual", help="who started this run (windows, manual, tray, ...): recorded in the log")
    parser.add_argument("--startup-method", choices=["task", "run", "shortcut"], default="task", help="with --enable-startup: the mechanism to register")
    return parser.parse_args(argv)


def _run_startup_command(args: argparse.Namespace, startup: StartupManager) -> int:
    try:
        if args.enable_startup:
            method = getattr(args, "startup_method", "task")
            try:
                print(f"Startup enabled: {startup.enable(method, exclusive=True)}")
            except StartupIntegrationError as exc:
                if method != "task":
                    raise
                print(f"Task Scheduler was not available ({exc}); using a Run entry instead.")
                print(f"Startup enabled: {startup.enable('run', exclusive=True)}")
        elif args.disable_startup:
            print("Startup disabled." if startup.disable() else "Startup was not enabled.")
        else:
            st = startup.status()
            print("Startup is enabled via: " + ", ".join(st["methods"]) if st["enabled"] else "Startup is disabled.")
            print("Command Windows runs: " + st["command"])
            if st["duplicate"]:
                print("WARNING: more than one start-with-Windows mechanism is enabled (JARVIS would be started twice; the second copy exits at once). "
                      "Run --enable-startup again to keep exactly one.")
    except StartupIntegrationError as exc:
        print(f"Startup error: {exc}")
        return 1
    return 0


def self_check(settings: Settings) -> int:
    """No secrets, nothing started. Proves the process found its project, its .env and its model files regardless of the directory it was launched from."""
    root = PROJECT_ROOT
    def model(path: str) -> str:
        return "not set" if not path else ("ok" if (root / path).is_file() or Path(path).is_file() else f"MISSING ({path})")

    lines = [f"PROJECT_ROOT={root}", f"PYTHON_EXECUTABLE={sys.executable}", f"CWD={os.getcwd()}", f"ENV_FILE={'present' if (root / '.env').is_file() else 'missing'}",
             "CONFIG_STATUS=ok", f"LLM={settings.LLM_PROVIDER}:{settings.LLM_MODEL} api_key={'configured' if settings.GROQ_API_KEY.get_secret_value() else 'missing'}",
             f"DATABASE={settings.DATABASE_URL.split(':', 1)[0]} (credentials not shown)", f"WAKE_MODEL={model(settings.WAKE_WORD_MODEL_PATH)}", f"TTS_MODEL={model(settings.TTS_MODEL_PATH)}",
             f"API={settings.API_HOST}:{settings.API_PORT}", f"TRAY_ENABLED={settings.JARVIS_TRAY_ENABLED}"]
    print("\n".join(lines))
    return 0


def _build_application(settings: Settings, exit_event: threading.Event, started_at: float | None = None, coordinator: ExitCoordinator | None = None,
                       startup_source: str = "manual") -> JarvisApplication:
    logger = get_logger(__name__)
    task_system = build_task_system(settings)
    services = build_runtime_services(settings, task_system, PROJECT_ROOT)
    bus = services.bus
    router = services.intelligence_router
    manager = RuntimeManager(lambda: build_voice_engine(settings, task_system, router, bus, services.voice), start_paused=services.start_paused)
    services.attach_manager(manager)

    chat = None
    try:
        from backend.core.dashboard_chat import DashboardChat

        chat = DashboardChat(build_conversation_engine(settings, task_system, router, bus))
    except Exception as exc:  # noqa: BLE001 - the dashboard command bar is optional; JARVIS keeps running without it
        logger.error("Dashboard chat could not be built (%s); the dashboard command bar will be unavailable", type(exc).__name__)
    tray = None
    if settings.JARVIS_TRAY_ENABLED:
        tray = TrayController(
            manager, (lambda: coordinator.request("user_exit", "tray Exit")) if coordinator is not None else exit_event.set, actions=build_tray_actions(services, manager),
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
                               center=services.center, intelligence=services.intelligence_service, manager=manager, task_system=task_system, hub=services.hub, voice=services.voice, browser=services.browser, autonomy=services.autonomy, operator=services.operator, chat=chat))
        if settings.JARVIS_API_ENABLED:
            background.append(ApiServer(settings.API_HOST, settings.API_PORT))
    except Exception as exc:  # noqa: BLE001 - the local dashboard is optional
        logger.error("Local API could not be prepared (%s)", type(exc).__name__)
    def report(info: dict) -> None:
        """Startup record once the voice runtime has left STARTING: tray, voice and microphone status (never audio, never content)."""
        deadline = time.monotonic() + 30.0   # the microphone opens a moment after the runtime says RUNNING (models load first): report what it really is
        while time.monotonic() < deadline:
            st = manager.status()
            if st.microphone_active or st.state.value != "running":
                break
            time.sleep(0.5)
        status = manager.status()
        mic = services.voice.status.snapshot() if services.voice is not None else {}
        logger.info("STARTUP_REPORT TRAY_STATUS=%s VOICE_STATUS=%s VOICE_STATE=%s MICROPHONE_STATUS=%s MICROPHONE_ACTIVE=%s WAKE_LISTENER=%s DEGRADED=%s", info.get("tray"),
                    status.state.value, status.voice_state, mic.get("microphone", "unknown"), status.microphone_active,
                    "waiting for the wake phrase" if status.microphone_active else "not listening", ",".join(info.get("degraded", [])) or "none")

    app = JarvisApplication(manager, exit_event, tray=tray, power=power, scheduler=scheduler, background=background, bus=bus, coordinator=coordinator,
                             startup_source=startup_source, report=report,
                             cleanup=([services.operator.shutdown] if services.operator is not None else []) + ([services.autonomy.shutdown] if services.autonomy is not None else []) + ([services.browser.shutdown] if services.browser is not None else []) + [dispose_engine], started_at=started_at)
    try:
        from backend.core.context import get_context

        ctx = get_context()
        if ctx is not None:
            ctx.app, ctx.startup_source = app, startup_source
    except Exception:  # noqa: BLE001 - the local API is optional
        pass
    return app


def _log_startup(settings: Settings, startup_source: str = "manual") -> None:
    """One structured startup record: version facts and non-secret configuration, so a support question can be answered from the log."""
    from backend.core.database import schema_status
    from backend.core.logging import log_event

    log = get_logger(__name__)
    log.info("JARVIS_STARTING STARTUP_SOURCE=%s PROJECT_ROOT=%s PYTHON_EXECUTABLE=%s PID=%d PARENT_PID=%d ENVIRONMENT=%s CONFIG_STATUS=ok LLM_STATUS=%s:%s api_key=%s",
             startup_source, PROJECT_ROOT, sys.executable, os.getpid(), os.getppid(), settings.APP_ENV, settings.LLM_PROVIDER, settings.LLM_MODEL,
             "configured" if settings.GROQ_API_KEY.get_secret_value() else "missing")
    log_event(log, "startup_begin", python=platform.python_version(), os=platform.platform(), env=settings.APP_ENV, offline=settings.JARVIS_OFFLINE_MODE,
              llm=f"{settings.LLM_PROVIDER}:{settings.LLM_MODEL}", intelligence=settings.JARVIS_INTELLIGENCE_ENABLED, tray=settings.JARVIS_TRAY_ENABLED)
    ok, detail = schema_status()
    log_event(log, "database_check", level=20 if ok else 30, ok=ok, detail=detail)
    log.info("DATABASE_STATUS=%s (%s)", "ready" if ok else "degraded", detail)


def _install_signal_handlers(coordinator: ExitCoordinator) -> dict:
    """Ctrl+C (SIGINT) and Ctrl+Break (SIGBREAK) become a stop request with reason `ctrl_c`. Returns the previous handlers (tests restore them)."""
    previous = {}
    for name in ("SIGINT", "SIGBREAK"):
        if hasattr(signal, name):
            sig = getattr(signal, name)
            previous[sig] = signal.signal(sig, lambda *_args, _n=name: coordinator.request("ctrl_c", _n))
    return previous


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
    if args.self_check:
        return self_check(settings)
    if args.status:
        running = SingleInstanceGuard.is_running()
        print("JARVIS is running." if running else "JARVIS is not running.")
        return 0 if running else 1
    if args.enable_startup or args.disable_startup or args.startup_status:
        return _run_startup_command(args, StartupManager())

    source = args.startup_source
    logger.info("JARVIS starting (configuration loaded)")
    if not settings.JARVIS_RUNTIME_ENABLED:
        logger.info("JARVIS_RUNTIME_ENABLED is false; exiting")
        return 0

    guard = SingleInstanceGuard()
    if not guard.acquire():
        # Windows auto-start + a manual start, or a second auto-start mechanism: the running JARVIS is left alone and this copy ends quietly. Exit code 0, so a
        # Task Scheduler "restart on failure" policy does not treat a duplicate start as a failure.
        logger.warning("JARVIS_ALREADY_RUNNING STARTUP_SOURCE=%s; not starting a second instance", source)
        return 0

    exit_event = threading.Event()
    coordinator = ExitCoordinator(exit_event)
    exit_signal = ExitSignal()
    exit_signal.wait_in_thread(lambda: coordinator.request("user_exit", "stop command"))
    _install_signal_handlers(coordinator)
    console_handler = install_console_handler(coordinator.request)  # console close / logoff / shutdown when run from a terminal (kept referenced)
    session = SessionEndWatcher(coordinator.request)
    session.start()  # Windows logoff / shutdown / restart of a windowless run: a stop request with reason windows_shutdown, never a crash

    try:
        _log_startup(settings, source)
        code = _build_application(settings, exit_event, started_at, coordinator, source).run()
        session.notify_shutdown_complete()
        return code
    finally:
        session.notify_shutdown_complete()
        session.stop()
        exit_signal.close()
        guard.release()
        del console_handler
