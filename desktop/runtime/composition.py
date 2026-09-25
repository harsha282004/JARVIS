"""Composition root of the Windows runtime: builds and connects every long-lived JARVIS service.

    settings -> event bus, privacy controller, preferences, notification center, audit log
             -> task system -> intelligence (context engine, planner, router, runner)
             -> health monitor, supervisor (automatic recovery), power tracker
             -> tray actions, wired to the RuntimeManager once it exists

Everything here is optional and isolated: a service that cannot be built is logged and left out, and JARVIS keeps running with what
remains. This module only wires things together; it contains no business logic.
"""

import os
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from agent.intelligence.confirmation import ConfirmationEngine
from agent.intelligence.context_engine import PersonalContextEngine
from agent.intelligence.dependencies import DependencyStore
from agent.intelligence.findings import FindingKind
from agent.intelligence.plan_executor import PlanExecutor
from agent.intelligence.planner import DayPlanner
from agent.intelligence.proactive import IntelligenceNotifier
from agent.intelligence.router import IntelligenceRouter
from agent.intelligence.runner import IntelligenceRunner
from agent.intelligence.service import IntelligenceService
from agent.intelligence.snapshot import SnapshotCollector
from agent.intelligence.timeline import ActivityTimeline
from agent.tasks.notifications import clean_notification_text
from agent.tasks.system import TaskSystem
from agent.tasks.zone import resolve_timezone
from backend.core import integration_switch
from backend.core.action_audit import ActionAuditLog
from backend.core.config import Settings
from backend.core.database import check_database_connection, dispose_engine, schema_status
from backend.core.events import EventBus, SystemEvent
from backend.core.health import HealthMonitor
from backend.core.llm.base import LLMProvider, LLMProviderError
from backend.core.logging import get_logger
from backend.core.notifications import Notification, NotificationCenter
from backend.core.preferences import PreferenceStore, Preferences, parse_hhmm
from backend.core.privacy import PrivacyController, PrivacyMode
from backend.core.recovery import BackoffPolicy, SupervisedService, Supervisor
from desktop.runtime.health_checks import build_health_monitor
from desktop.runtime.periodic import PeriodicTask
from desktop.runtime.power import PowerState, PowerStateTracker
from desktop.tray.tray import TrayActions
from integrations.hub.hub import IntegrationHub
from integrations.hub.models import Permission
from integrations.hub.registry import UnconfiguredAdapter
from integrations.hub.sync import SyncRunner

logger = get_logger(__name__)


class _NoLLM(LLMProvider):
    """The intelligence layer is deterministic: handing its services a provider that refuses proves it never asks for a model."""

    def chat(self, messages, json_mode: bool = False) -> str:
        raise LLMProviderError("the intelligence layer does not use a language model")


@dataclass
class RuntimeServices:
    settings: Settings
    state_dir: Path
    bus: EventBus
    privacy: PrivacyController
    prefs: PreferenceStore
    audit: ActionAuditLog
    center: NotificationCenter
    health: HealthMonitor
    supervisor: Supervisor
    task_system: TaskSystem | None
    power: PowerStateTracker | None = None
    gmail: object | None = None
    calendar: object | None = None
    memory: object | None = None
    db_status: Callable[[], tuple[bool, str]] = lambda: (True, "no database check")
    intelligence_service: IntelligenceService | None = None
    intelligence_router: IntelligenceRouter | None = None
    intelligence_runner: IntelligenceRunner | None = None
    hub: IntegrationHub | None = None
    sync_runner: SyncRunner | None = None
    tray_holder: dict = field(default_factory=dict)  # "tray" -> TrayController once it exists (notifications are delivered through it)
    periodic: list[PeriodicTask] = field(default_factory=list)
    manager: object | None = None
    paused_by_privacy: bool = False
    locked_by_power: bool = False

    # ---- wiring that needs the RuntimeManager -------------------------------------------------------------------

    def start_paused(self) -> bool:
        """The saved privacy mode survives restarts: PRIVATE/PAUSED means the microphone is never opened at startup."""
        return not self.privacy.capabilities.microphone

    def attach_manager(self, manager) -> None:
        self.manager = manager
        self.privacy.add_listener(self._on_privacy_change)
        self.supervisor.add(SupervisedService(
            "voice_runtime",
            check=lambda: manager.state.value != "error",  # only a crashed runtime is unhealthy; paused/stopped are deliberate
            restart=manager.restart,
            policy=BackoffPolicy(self.settings.JARVIS_RECOVERY_INITIAL_SECONDS, 2.0, 120.0, self.settings.JARVIS_RECOVERY_MAX_ATTEMPTS,
                                 self.settings.JARVIS_RECOVERY_COOLDOWN_SECONDS),
            on_failed=lambda name: self.bus.publish(SystemEvent.INTEGRATION_FAILED, service=name),
            on_recovered=lambda name: self.bus.publish(SystemEvent.INTEGRATION_RECOVERED, service=name),
        )) if self.settings.JARVIS_AUTO_RECOVERY else None

    def _on_privacy_change(self, old: PrivacyMode, new: PrivacyMode) -> None:
        manager = self.manager
        if manager is None:
            return
        if not self.privacy.capabilities.microphone:
            if manager.state.value == "running" and manager.pause():
                self.paused_by_privacy = True
        elif self.paused_by_privacy:
            self.paused_by_privacy = False
            manager.resume()

    def on_power_change(self, old: PowerState, new: PowerState) -> None:
        self.bus.publish(SystemEvent.POWER_STATE_CHANGED, old=old.value, new=new.value)
        if new is PowerState.BACKGROUND and self.privacy.mode is PrivacyMode.ACTIVE:
            self.privacy.set_mode(PrivacyMode.BACKGROUND)
            self.locked_by_power = True
        elif new is PowerState.ACTIVE and self.privacy.mode is PrivacyMode.BACKGROUND and self.locked_by_power:
            self.locked_by_power = False
            self.privacy.set_mode(PrivacyMode.ACTIVE)

    def handle_resume(self) -> None:
        """Called after Windows wakes: note the resume, refresh connections and services."""
        self.bus.publish(SystemEvent.SYSTEM_SLEEP)
        if self.power is not None:
            self.power.note_resume()
        self.bus.publish(SystemEvent.SYSTEM_RESUME)
        try:
            dispose_engine()  # pooled connections are stale after sleep; new ones are opened on demand
        except Exception as exc:  # noqa: BLE001
            logger.warning("Resume housekeeping failed (%s)", type(exc).__name__)
        self.health.check_all()


def _state_dir(settings: Settings, project_root: Path) -> Path:
    path = Path(settings.JARVIS_STATE_DIR)
    return path if path.is_absolute() else project_root / path


def build_runtime_services(settings: Settings, task_system: TaskSystem | None, project_root: Path) -> RuntimeServices:
    from voice import bootstrap as b  # local import: bootstrap pulls in the voice stack

    state = _state_dir(settings, project_root)
    state.mkdir(parents=True, exist_ok=True)
    bus = EventBus()
    zone = task_system.parser.zone if task_system is not None else resolve_timezone(settings.JARVIS_TIMEZONE)

    privacy = PrivacyController(state / "privacy.json", bus, default=PrivacyMode(settings.JARVIS_PRIVACY_DEFAULT))
    prefs = PreferenceStore(state / "preferences.json", Preferences(
        quiet_enabled=settings.JARVIS_PROACTIVE_QUIET_HOURS_ENABLED, quiet_start=settings.JARVIS_PROACTIVE_QUIET_START, quiet_end=settings.JARVIS_PROACTIVE_QUIET_END,
        workday_start=settings.JARVIS_WORKDAY_START, workday_end=settings.JARVIS_WORKDAY_END, auto_create_tasks=settings.JARVIS_AUTO_CREATE_TASKS,
    ))
    audit = ActionAuditLog(state / "audit.jsonl")
    holder: dict = {}

    def deliver(channel: str, note: Notification) -> None:
        text = clean_notification_text(note.body)
        if channel == "desktop":
            tray = holder.get("tray")
            if tray is None:
                raise RuntimeError("no tray")
            tray.notify(note.title or "JARVIS", text)
        elif channel == "voice":
            if task_system is None or not task_system.announcements.put(text):
                raise RuntimeError("voice not accepting")
        else:
            raise ValueError("unknown channel")

    center = NotificationCenter(preferences=prefs, zone=zone, deliver=deliver, privacy=privacy, bus=bus, state_file=state / "notifications.json",
                                cooldown_minutes=settings.JARVIS_NOTIFICATION_COOLDOWN_MINUTES)
    monitor = HealthMonitor(bus)
    supervisor = Supervisor(interval_seconds=max(5.0, settings.JARVIS_HEALTH_INTERVAL_SECONDS / 2))
    services = RuntimeServices(settings, state, bus, privacy, prefs, audit, center, monitor, supervisor, task_system, tray_holder=holder)
    services.power = PowerStateTracker(services.on_power_change)

    if settings.JARVIS_INTELLIGENCE_ENABLED:
        try:
            _build_intelligence(services, settings, task_system, zone, b, bus, state)
        except Exception as exc:  # noqa: BLE001 - the intelligence layer is optional; JARVIS keeps running without it
            logger.error("Personal intelligence could not be built (%s); continuing without it", type(exc).__name__)
            services.intelligence_service = services.intelligence_router = services.intelligence_runner = None

    def db_status() -> tuple[bool, str]:
        if not check_database_connection():
            return False, "database unreachable"
        return schema_status()

    services.db_status = db_status
    return services


def _build_intelligence(services: RuntimeServices, settings: Settings, task_system, zone, b, bus: EventBus, state: Path) -> None:
    llm = _NoLLM()
    gmail = b.build_gmail_service(settings, llm) if settings.JARVIS_GMAIL_ENABLED else None
    calendar = b.build_calendar_service(settings, zone) if settings.JARVIS_CALENDAR_ENABLED else None
    tasks = task_system.tasks if task_system is not None else None
    reminders = task_system.reminders if task_system is not None else None
    events = b.build_event_service(settings, zone, tasks) if settings.JARVIS_EVENTS_ENABLED else None
    memory = b._build_memory(settings)  # noqa: SLF001
    rag = b.build_rag_service(settings, llm) if settings.JARVIS_RAG_ENABLED else None
    services.gmail, services.calendar, services.memory = gmail, calendar, memory
    hub = _build_hub(services, settings, zone, b, llm, rag, memory) if settings.JARVIS_HUB_ENABLED else None
    services.hub = hub

    confirmations = ConfirmationEngine(services.audit)
    deps = DependencyStore(state / "dependencies.json")
    from agent.tasks.models import utcnow

    planner = DayPlanner(zone, parse_hhmm(services.prefs.get().workday_start), parse_hhmm(services.prefs.get().workday_end),
                         settings.JARVIS_DEFAULT_TASK_MINUTES, settings.JARVIS_PLAN_BUFFER_MINUTES)
    collector = SnapshotCollector(zone=zone, clock=utcnow, tasks=tasks, reminders=reminders, events=events, calendar=calendar, gmail=gmail,
                                  memory=memory, rag=rag, hub=hub, offline=lambda: settings.JARVIS_OFFLINE_MODE)

    def rag_search(query: str):
        return [(r.title or r.filename, r.page) for r in rag.search(query)] if rag is not None else []

    service = IntelligenceService(
        zone=zone, collector=collector, engine=PersonalContextEngine(zone, utcnow, deps), planner=planner, confirmations=confirmations,
        prefs=services.prefs, deps=deps, timeline=ActivityTimeline(state / "timeline.jsonl"), audit=services.audit, bus=bus,
        executor=PlanExecutor(calendar, zone, confirmations, utcnow, gate=(lambda: hub.registry.allowed("calendar", Permission.CREATE_EVENT)) if hub is not None else None) if calendar is not None else None, tasks=tasks,
        rag_search=rag_search if rag is not None else None, state_file=state / "intelligence.json", auto_create_default=settings.JARVIS_AUTO_CREATE_TASKS,
        notifications=services.center,
    )
    skip = frozenset({FindingKind.DEADLINE_PENDING_TASK, FindingKind.OVERDUE_TASK, FindingKind.CALENDAR_OVERLAP}) if settings.JARVIS_PROACTIVE_ENABLED else frozenset()
    notifier = IntelligenceNotifier(services.center, service.explanations, skip) if settings.JARVIS_INTELLIGENCE_PROACTIVE else None
    services.intelligence_service = service
    hub_router = None
    if hub is not None:
        from agent.intelligence.hub_router import HubRouter
        from agent.memory.models import Confidence, MemoryBasis, MemoryCandidate, MemorySource, MemoryType

        def remember(text: str) -> None:
            if memory is not None:  # an explicit statement by the user: stored as a user memory (the memory policy still screens it)
                memory.store(MemoryCandidate(type=MemoryType.CONTEXT, content=text, source=MemorySource.EXPLICIT_USER_STATEMENT, basis=MemoryBasis.EXPLICIT, confidence=Confidence.HIGH))

        hub_router = HubRouter(hub, service, remember=remember)
    services.intelligence_router = IntelligenceRouter(service, hub_router)
    services.intelligence_runner = IntelligenceRunner(
        service, notifier, center=services.center, bus=bus, privacy=services.privacy, interval_seconds=settings.JARVIS_INTELLIGENCE_INTERVAL_SECONDS,
    )


def build_health(services: RuntimeServices, manager) -> None:
    """Registers the real checks (needs the manager, so it runs after the manager exists)."""
    runner = services.intelligence_runner
    build_health_monitor(
        services.health, settings=services.settings, manager=manager, privacy=services.privacy, db_status=services.db_status,
        scheduler_alive=lambda: runner.is_alive() if runner is not None else None,
        gmail=services.gmail, calendar=services.calendar, messaging=None, memory_enabled=services.memory is not None, hub=services.hub,
    )


def build_tray_actions(services: RuntimeServices, manager) -> TrayActions:
    svc = services.intelligence_service
    ts = services.task_system

    def briefing() -> str:
        return svc.morning().text

    def tasks() -> str:
        items = ts.tasks.incomplete_tasks(limit=6) if ts and ts.tasks else []
        return "Open tasks: " + "; ".join(t.title for t in items) if items else "You have no open tasks."

    def reminders() -> str:
        items = ts.reminders.upcoming_reminders(limit=5) if ts and ts.reminders else []
        return "Upcoming reminders: " + "; ".join(r.message for r in items) if items else "You have no upcoming reminders."

    def memory() -> str:
        mem = services.memory
        if mem is None:
            return "Personal memory is not enabled."
        found = mem.search(None, limit=50)
        return f"I have {len(found)} stored memories." + (" Most recent: " + "; ".join(m.content for m in found[:2]) if found else "")

    def integrations() -> str:
        if services.hub is not None:
            return "; ".join(f"{i.display_name}: {i.status.value}" for i in services.hub.registry.all_info())
        reports = services.health.check_all()
        names = {"gmail": "Gmail", "calendar": "Calendar", "messaging": "Messaging", "llm": "LLM", "database": "Database"}
        return "; ".join(f"{names[r.name]}: {r.state.value}" for r in reports if r.name in names)

    def settings_() -> None:
        env = services.state_dir.parent / ".env"
        target = env if env.exists() else services.state_dir.parent
        try:
            os.startfile(str(target))  # type: ignore[attr-defined]  # opens .env in the user's editor (Windows only)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Could not open settings (%s)", type(exc).__name__)

    def toggle_private() -> None:
        services.privacy.set_mode(PrivacyMode.ACTIVE if services.privacy.mode is PrivacyMode.PRIVATE else PrivacyMode.PRIVATE)

    return TrayActions(
        show_briefing=briefing if svc is not None else None,
        show_tasks=tasks if ts is not None and ts.tasks is not None else None,
        show_reminders=reminders if ts is not None and ts.reminders is not None else None,
        show_memory=memory if services.memory is not None else None,
        show_integrations=integrations,
        open_settings=settings_,
        toggle_private=toggle_private,
        talk=manager.request_activation,
    )


def _build_hub(services: RuntimeServices, settings: Settings, zone, b, llm, rag, memory) -> IntegrationHub:
    """Every integration is registered, configured or not, so status is always reported truthfully. The registry also becomes the global on/off switch."""
    from backend.core.database import SessionLocal
    from agent.tasks.models import utcnow
    from integrations.calendar.adapter import CalendarAdapter
    from integrations.calendar.client import HttpCalendarClient
    from integrations.calendar.service import CalendarService
    from integrations.documents.adapter import DocumentsAdapter
    from integrations.github.adapter import GitHubAdapter, ProjectRepos
    from integrations.github.auth import GitHubTokenStore
    from integrations.github.client import GitHubClient
    from integrations.gmail.adapter import GmailAdapter
    from integrations.gmail.client import HttpGmailClient
    from integrations.gmail.service import GmailService
    from integrations.messaging.adapter import MessagingAdapter
    from integrations.messaging.service import MessagingService

    state = services.state_dir
    hub = IntegrationHub(SessionLocal, state / "integrations.json", services.bus, zone, utcnow, center=services.center, retention_days=settings.JARVIS_HUB_RETENTION_DAYS)
    integration_switch.install(hub.registry.is_enabled)

    def placeholder(name: str, label: str, perms: set[Permission], flag: str) -> None:
        hub.register(UnconfiguredAdapter(name, label, frozenset(perms), f"Set {flag}=true in .env"))

    if settings.JARVIS_GMAIL_ENABLED:
        auth = b.build_gmail_authenticator(settings)
        service = GmailService(HttpGmailClient(auth), llm, max_results=settings.JARVIS_GMAIL_MAX_RESULTS, is_ready=lambda: auth.is_ready() and integration_switch.enabled("gmail"))
        hub.register(GmailAdapter(service, auth, zone, utcnow, initial_days=settings.JARVIS_GMAIL_SYNC_INITIAL_DAYS,
                                  attachments_dir=b._project_path(settings.JARVIS_GMAIL_ATTACHMENTS_DIR)))  # noqa: SLF001
    else:
        placeholder("gmail", "Gmail", {Permission.READ_EMAIL, Permission.SEARCH_EMAIL, Permission.READ_ATTACHMENT}, "JARVIS_GMAIL_ENABLED")
    if settings.JARVIS_CALENDAR_ENABLED:
        auth = b.build_calendar_authenticator(settings)
        service = CalendarService(HttpCalendarClient(auth, zone=zone), zone=zone, max_results=settings.JARVIS_CALENDAR_MAX_RESULTS,
                                  is_ready=lambda: auth.is_ready() and integration_switch.enabled("calendar"))
        hub.register(CalendarAdapter(service, auth, zone, utcnow))
    else:
        placeholder("calendar", "Google Calendar", {Permission.READ_EVENTS, Permission.CREATE_EVENT, Permission.UPDATE_EVENT, Permission.DELETE_EVENT}, "JARVIS_CALENDAR_ENABLED")
    if settings.JARVIS_GITHUB_ENABLED:
        store = GitHubTokenStore(b._project_path(settings.JARVIS_GITHUB_TOKEN_PATH), lambda: settings.GITHUB_TOKEN.get_secret_value(), settings.JARVIS_ENCRYPT_TOKENS)  # noqa: SLF001
        hub.register(GitHubAdapter(GitHubClient(store.token), store, ProjectRepos(state / "github_projects.json"), utcnow,
                                   settings.GITHUB_OAUTH_CLIENT_ID, settings.JARVIS_GITHUB_OAUTH_SCOPE))
    else:
        placeholder("github", "GitHub", {Permission.READ_REPOSITORIES, Permission.READ_COMMITS, Permission.READ_ISSUES, Permission.READ_PULL_REQUESTS}, "JARVIS_GITHUB_ENABLED")
    if settings.JARVIS_MESSAGING_ENABLED:
        hub.register(MessagingAdapter(MessagingService(b.build_messaging_registry(settings), llm, max_results=settings.JARVIS_MESSAGING_MAX_RESULTS), zone, utcnow))
    else:
        placeholder("messaging", "Messaging (Telegram)", {Permission.READ_MESSAGES, Permission.SEARCH_MESSAGES}, "JARVIS_MESSAGING_ENABLED")
    directories = [Path(p.strip()) for p in settings.JARVIS_DOCUMENT_DIRS.split(";") if p.strip()]
    if rag is not None and directories:
        hub.register(DocumentsAdapter(rag, directories, state / "document_watch.json", utcnow, settings.JARVIS_DOCUMENT_REMOVE_DELETED))
    else:
        placeholder("documents", "Documents", {Permission.READ_DOCUMENTS, Permission.INDEX_DOCUMENTS}, "JARVIS_DOCUMENT_DIRS (and JARVIS_RAG_ENABLED)")
    services.sync_runner = SyncRunner(hub.engine, services.bus, services.privacy, interval_seconds=settings.JARVIS_SYNC_LOOP_SECONDS)
    services.periodic.append(PeriodicTask("hub-prune", hub.prune, 24 * 3600.0, run_immediately=False))
    return hub
