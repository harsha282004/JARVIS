"""Builds a VoiceEngine from application settings.

Provider selection goes through the *_PROVIDER config values so swapping an
implementation later is a config change, not a code change at call sites —
today only one concrete implementation exists per interface.
"""

from agent.brain.brain import AgentBrain
from agent.knowledge_graph.context import GraphContextProvider
from agent.knowledge_graph.repository import GraphRepository
from agent.knowledge_graph.service import GraphService
from agent.knowledge_graph.sync import DocumentGraphSync, MemoryGraphSync
from agent.memory.models import Confidence
from agent.rag.chunker import Chunker
from agent.rag.documents import DocumentRepository
from agent.rag.embeddings import SentenceTransformerProvider
from agent.rag.retriever import Retriever
from agent.rag.service import RagLimits, RagService
from agent.rag.store import SqlVectorStore
from agent.events.graph import EventGraphLinker
from agent.events.repository import EventRepository
from agent.events.service import EventService
from agent.events.tools import EventTool, EventToolContext, build_event_tools
from agent.tasks.executor import TaskActionExecutor
from agent.tasks.models import MissedPolicy, TaskPriority, utcnow
from agent.tasks.notifications import (
    AnnouncementQueue,
    CompositeNotifier,
    DesktopNotifier,
    NotificationService,
    VoiceNotifier,
)
from agent.tasks.repository import TaskRepository
from agent.tasks.scheduler import ReminderScheduler
from agent.briefing.builder import Builder as BriefingBuilder
from agent.briefing.collector import ProductivityCollector
from agent.briefing.service import BriefingService
from agent.briefing.tools import BriefingToolContext, build_briefing_tools
from agent.proactive.engine import ProactiveEngine
from agent.proactive.models import Channel
from agent.proactive.policy import NotificationPolicy, PolicyConfig, parse_clock
from agent.proactive.repository import NotificationRepository
from agent.proactive.sources import CalendarSignalSource, EventSignalSource, GmailSignalSource, SignalSource, TaskSignalSource
from agent.proactive.tools import ProactiveExplainTool, ProactiveToolContext, build_proactive_tools
from agent.tasks.service import ReminderService, TaskService
from agent.tasks.system import TaskSystem
from agent.tasks.timeparse import TimeParser
from agent.tasks.tools import TaskToolContext, build_task_tools
from agent.tasks.zone import resolve_timezone
from integrations.calendar.auth import CalendarAuthenticator
from integrations.calendar.client import HttpCalendarClient
from integrations.calendar.service import CalendarService
from integrations.calendar.sync import CalendarEventSync
from integrations.calendar.tools import CalendarTool, CalendarToolContext, build_calendar_tools
from integrations.messaging.base import ProviderRegistry
from integrations.messaging.service import MessagingService
from integrations.messaging.telegram import TelegramProvider
from integrations.messaging.tools import MessagingTool, MessagingToolContext, build_messaging_tools
from integrations.gmail.auth import GmailAuthenticator
from integrations.gmail.client import HttpGmailClient
from integrations.gmail.service import GmailService
from integrations.gmail.tools import GmailTool, GmailToolContext, build_gmail_tools
from agent.memory.policy import MemoryPolicy
from agent.memory.repository import MemoryRepository
from agent.memory.service import MemoryService
from pathlib import Path

from backend.core import integration_switch as switch
from backend.core.config import Settings
from backend.core.database import SessionLocal
from backend.core.conversation.engine import ConversationEngine
from backend.core.llm.base import LLMProvider
from backend.core.llm.metered import MeteredLLM
from backend.core.llm.ollama_provider import OllamaProvider
from backend.core.logging import get_logger
from backend.core.security import AuditLog, PermissionManager
from voice.audio import AudioInput, AudioOutput
from voice.engine import VoiceEngine
from voice.exceptions import ProviderNotConfiguredError
from voice.stt.base import STTProvider
from voice.stt.faster_whisper_provider import FasterWhisperProvider
from voice.tts.base import TTSProvider
from voice.tts.piper_provider import PiperProvider
from voice.wakeword.base import WakeWordProvider
from voice.wakeword.openwakeword_provider import OpenWakeWordProvider


logger = get_logger(__name__)


def _build_wakeword(settings: Settings) -> WakeWordProvider:
    if settings.WAKE_WORD_PROVIDER == "openwakeword":
        return OpenWakeWordProvider(
            model_path=settings.WAKE_WORD_MODEL_PATH,
            threshold=settings.WAKE_WORD_THRESHOLD,
        )
    raise ProviderNotConfiguredError(
        f"Unknown WAKE_WORD_PROVIDER '{settings.WAKE_WORD_PROVIDER}'"
    )


def _build_stt(settings: Settings) -> STTProvider:
    if settings.STT_PROVIDER == "faster_whisper":
        return FasterWhisperProvider(
            model_size=settings.STT_MODEL,
            language=settings.STT_LANGUAGE,
            device=settings.STT_DEVICE,
        )
    raise ProviderNotConfiguredError(f"Unknown STT_PROVIDER '{settings.STT_PROVIDER}'")


def _build_llm(settings: Settings) -> LLMProvider:
    if settings.LLM_PROVIDER == "ollama":
        return OllamaProvider(base_url=settings.OLLAMA_BASE_URL, model=settings.LLM_MODEL)
    raise ProviderNotConfiguredError(f"Unknown LLM_PROVIDER '{settings.LLM_PROVIDER}'")


def _build_tts(settings: Settings) -> TTSProvider:
    if settings.TTS_PROVIDER == "piper":
        return PiperProvider(model_path=settings.TTS_MODEL_PATH)
    raise ProviderNotConfiguredError(f"Unknown TTS_PROVIDER '{settings.TTS_PROVIDER}'")


def _build_memory(settings: Settings) -> MemoryService | None:
    if not settings.JARVIS_MEMORY_ENABLED:
        return None
    return MemoryService(
        MemoryRepository(SessionLocal),
        policy=MemoryPolicy(
            auto_save=settings.JARVIS_MEMORY_AUTO_SAVE,
            min_confidence=Confidence[settings.JARVIS_MEMORY_MIN_CONFIDENCE.upper()],
        ),
        max_retrieval=settings.JARVIS_MEMORY_MAX_RETRIEVAL,
    )


def build_rag_service(settings: Settings, llm: LLMProvider) -> RagService | None:
    if not settings.JARVIS_RAG_ENABLED:
        return None
    embedder = SentenceTransformerProvider(settings.JARVIS_RAG_EMBEDDING_MODEL)  # loads lazily
    store = SqlVectorStore(SessionLocal)
    return RagService(
        documents=DocumentRepository(SessionLocal),
        store=store,
        embedder=embedder,
        retriever=Retriever(embedder, store, settings.JARVIS_RAG_TOP_K, settings.JARVIS_RAG_MIN_SCORE),
        llm=llm,
        chunker=Chunker(settings.JARVIS_RAG_CHUNK_SIZE, settings.JARVIS_RAG_CHUNK_OVERLAP),
        limits=RagLimits(
            max_document_bytes=settings.JARVIS_RAG_MAX_DOCUMENT_SIZE_MB * 1024 * 1024,
            max_chunks_per_document=settings.JARVIS_RAG_MAX_CHUNKS_PER_DOCUMENT,
        ),
    )


def build_graph_service(settings: Settings) -> GraphService | None:
    if not settings.JARVIS_KG_ENABLED:
        return None
    return GraphService(
        GraphRepository(SessionLocal),
        min_confidence=Confidence[settings.JARVIS_KG_MIN_CONFIDENCE.upper()],
        max_path_depth=settings.JARVIS_KG_MAX_PATH_DEPTH,
        max_results=settings.JARVIS_KG_MAX_RESULTS,
    )


def build_task_system(settings: Settings, clock=utcnow) -> TaskSystem | None:
    """Task and reminder services over one repository (database sessions are per call and per thread).
    None when both are disabled, or when the timezone cannot be loaded (logged; JARVIS keeps running)."""
    if not (settings.JARVIS_TASKS_ENABLED or settings.JARVIS_REMINDERS_ENABLED):
        return None
    try:
        zone = resolve_timezone(settings.JARVIS_TIMEZONE)
    except Exception as exc:  # noqa: BLE001 - e.g. no tz database; tasks are optional, the assistant is not
        logger.error("Tasks and reminders are unavailable: timezone could not be loaded (%s)", type(exc).__name__)
        return None
    repository = TaskRepository(SessionLocal)
    tasks = (
        TaskService(
            repository,
            zone=zone,
            clock=clock,
            default_priority=TaskPriority[settings.JARVIS_DEFAULT_TASK_PRIORITY.upper()],
        )
        if settings.JARVIS_TASKS_ENABLED
        else None
    )
    reminders = ReminderService(repository, zone=zone, clock=clock) if settings.JARVIS_REMINDERS_ENABLED else None
    parser = TimeParser(zone)
    tools = build_task_tools(TaskToolContext(tasks, reminders, parser, clock))
    return TaskSystem(tasks, reminders, parser, tools, AnnouncementQueue())


def build_reminder_scheduler(
    settings: Settings, system: TaskSystem | None, desktop_send=None, proactive: ProactiveEngine | None = None
) -> ReminderScheduler | None:
    """The one scheduler thread. `desktop_send(title, message)` shows a local desktop notification (the tray's).
    Reminders need reminders enabled and a notification channel. The proactive engine (Phase 14) runs as an extra pass on
    the same thread, so there is no second scheduler; if reminders are off but proactive is on, the thread still runs it."""
    reminders = system.reminders if system is not None else None
    extra = [proactive.run_once] if proactive is not None else []
    notifier: NotificationService | None = None
    if reminders is not None:
        channels: list[tuple[str, NotificationService]] = []
        if settings.JARVIS_REMINDER_DESKTOP_NOTIFICATIONS and desktop_send is not None:
            channels.append(("desktop", DesktopNotifier(desktop_send)))
        if settings.JARVIS_REMINDER_VOICE_NOTIFICATIONS:
            channels.append(("voice", VoiceNotifier(system.announcements)))
        if channels:
            notifier = CompositeNotifier(channels)
        else:
            logger.warning("No reminder notification channel is available; reminders will not be delivered")
            reminders = None
    if reminders is None and not extra:
        return None
    return ReminderScheduler(
        reminders,
        notifier,
        tasks=system.tasks if system is not None and reminders is not None else None,
        extra_passes=extra,
        poll_seconds=settings.JARVIS_REMINDER_POLL_SECONDS,
        missed_policy=MissedPolicy(settings.JARVIS_MISSED_REMINDER_POLICY),
    )


def build_proactive_engine(settings: Settings, system: TaskSystem | None, desktop_send=None, clock=utcnow) -> ProactiveEngine | None:
    """The proactive engine, or None when it is disabled or has nothing to observe or no channel to notify through.
    It reuses the existing services and the existing tray/voice notifiers; it creates no second scheduler or notifier."""
    if not settings.JARVIS_PROACTIVE_ENABLED:
        return None
    try:
        zone = system.parser.zone if system is not None else resolve_timezone(settings.JARVIS_TIMEZONE)
        config = PolicyConfig(
            enabled=True, quiet_hours_enabled=settings.JARVIS_PROACTIVE_QUIET_HOURS_ENABLED,
            quiet_start=parse_clock(settings.JARVIS_PROACTIVE_QUIET_START), quiet_end=parse_clock(settings.JARVIS_PROACTIVE_QUIET_END),
            cooldown_minutes=settings.JARVIS_PROACTIVE_COOLDOWN_MINUTES, lookahead_minutes=settings.JARVIS_PROACTIVE_LOOKAHEAD_MINUTES,
            max_per_hour=settings.JARVIS_PROACTIVE_MAX_PER_HOUR,
        )
    except Exception as exc:  # noqa: BLE001 - proactive is optional; the assistant is not
        logger.error("Proactive intelligence is unavailable (%s)", type(exc).__name__)
        return None
    lookahead = config.lookahead_minutes
    refresh = settings.JARVIS_PROACTIVE_EXTERNAL_POLL_MINUTES
    sources: list[SignalSource] = []
    if system is not None and system.tasks is not None:
        sources.append(TaskSignalSource(system.tasks, lookahead))
    calendar = build_calendar_service(settings, zone) if settings.JARVIS_PROACTIVE_CALENDAR else None
    if settings.JARVIS_EVENTS_ENABLED:
        events = build_event_service(settings, zone, system.tasks if system is not None else None)
        sources.append(EventSignalSource(events, lookahead, calendar_active=calendar is not None))
    if calendar is not None:
        sources.append(CalendarSignalSource(calendar, lookahead, refresh))
    if settings.JARVIS_PROACTIVE_GMAIL:
        gmail = build_gmail_service(settings, _build_llm(settings))
        if gmail is not None:
            sources.append(GmailSignalSource(gmail, refresh))
    notifiers: dict[Channel, NotificationService] = {}
    if settings.JARVIS_REMINDER_DESKTOP_NOTIFICATIONS and desktop_send is not None:
        notifiers[Channel.DESKTOP] = DesktopNotifier(desktop_send, title="JARVIS")
    if settings.JARVIS_REMINDER_VOICE_NOTIFICATIONS and system is not None:
        notifiers[Channel.VOICE] = VoiceNotifier(system.announcements)
    if not sources or not notifiers:
        logger.warning("Proactive intelligence has %s; it is not started", "nothing to observe" if not sources else "no notification channel")
        return None
    return ProactiveEngine(
        sources, NotificationPolicy(config, zone), NotificationRepository(SessionLocal), notifiers, zone, clock=clock,
        interval_seconds=settings.JARVIS_PROACTIVE_POLL_SECONDS,
    )


def build_proactive_tools_for(settings: Settings, zone) -> list[ProactiveExplainTool]:
    """The read-only "why did you notify me?" tool, or none when proactive intelligence is disabled."""
    if not settings.JARVIS_PROACTIVE_ENABLED:
        return []
    return build_proactive_tools(ProactiveToolContext(NotificationRepository(SessionLocal), zone, utcnow))


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _project_path(value: str) -> Path:
    path = Path(value).expanduser()
    return path if path.is_absolute() else PROJECT_ROOT / path


def build_gmail_authenticator(settings: Settings) -> GmailAuthenticator:
    return GmailAuthenticator(
        _project_path(settings.JARVIS_GMAIL_CREDENTIALS_PATH),
        _project_path(settings.JARVIS_GMAIL_TOKEN_PATH),
        settings.GMAIL_CLIENT_ID,
        settings.GMAIL_CLIENT_SECRET.get_secret_value(),
        encrypt_at_rest=settings.JARVIS_ENCRYPT_TOKENS,
    )


def build_gmail_service(settings: Settings, llm: LLMProvider) -> GmailService | None:
    """The Gmail service, or None when Gmail is disabled. Building it never contacts Google and never fails for
    missing credentials: a request then gets a clear setup message (see docs/gmail-intelligence.md)."""
    if not settings.JARVIS_GMAIL_ENABLED:
        return None
    auth = build_gmail_authenticator(settings)
    return GmailService(
        HttpGmailClient(auth), llm, max_results=settings.JARVIS_GMAIL_MAX_RESULTS, is_ready=lambda: auth.is_ready() and switch.enabled("gmail")
    )


def build_gmail_tools_for(settings: Settings, llm: LLMProvider, zone) -> list[GmailTool]:
    """The read-only Gmail tools, or none when Gmail is disabled. Summaries use the same local LLM as everything else."""
    service = build_gmail_service(settings, llm)
    return build_gmail_tools(GmailToolContext(service, zone, utcnow)) if service is not None else []


def _telegram_token_source(settings: Settings):
    """The bot token: MESSAGING_TELEGRAM_BOT_TOKEN, else the token file. Read on demand, never logged or cached in text."""

    def read() -> str:
        if not switch.enabled("messaging"):
            return ""  # switched off by the user: the provider then reports "not set up"
        value = settings.MESSAGING_TELEGRAM_BOT_TOKEN.get_secret_value().strip()
        if value:
            return value
        path = _project_path(settings.JARVIS_MESSAGING_TELEGRAM_TOKEN_PATH)
        return path.read_text(encoding="utf-8").strip() if path.is_file() else ""

    return read


def build_messaging_registry(settings: Settings) -> ProviderRegistry:
    """The real providers. Only Telegram (official Bot API, read-only) exists; nothing else is ever registered."""
    registry = ProviderRegistry()
    registry.register(TelegramProvider(_telegram_token_source(settings)))
    return registry


def build_messaging_tools_for(settings: Settings, llm: LLMProvider, zone) -> list[MessagingTool]:
    """The read-only messaging tools, or none when messaging is disabled. Building them never contacts a provider and
    never fails for a missing token: a request then gets a clear setup message (docs/messaging-integration.md)."""
    service = build_messaging_service(settings, llm)
    if service is None:
        return []
    return build_messaging_tools(MessagingToolContext(service, TimeParser(zone), utcnow))


def build_messaging_service(settings: Settings, llm: LLMProvider) -> MessagingService | None:
    """The messaging service, or None when messaging is disabled. Never contacts a provider when built."""
    if not settings.JARVIS_MESSAGING_ENABLED:
        return None
    return MessagingService(build_messaging_registry(settings), llm, max_results=settings.JARVIS_MESSAGING_MAX_RESULTS)


def build_briefing_service(
    settings: Settings, llm: LLMProvider, zone, *, tasks=None, reminders=None, events=None, gmail=None, calendar=None, messaging=None, clock=utcnow
) -> BriefingService | None:
    """The briefing service over the EXISTING services (it creates no task/reminder/calendar/email system of its own), or None
    when briefings are disabled. Every service is optional: whatever is not set up is simply left out of a briefing."""
    if not settings.JARVIS_BRIEFING_ENABLED:
        return None
    collector = ProductivityCollector(
        zone=zone, clock=clock, tasks=tasks, reminders=reminders, events=events, calendar=calendar, gmail=gmail, messaging=messaging,
        max_items=settings.JARVIS_BRIEFING_MAX_ITEMS, lookahead_days=settings.JARVIS_BRIEFING_LOOKAHEAD_DAYS, email_limit=settings.JARVIS_BRIEFING_EMAIL_LIMIT,
    )
    return BriefingService(collector, BriefingBuilder(zone, settings.JARVIS_BRIEFING_MAX_ITEMS), llm=llm, use_llm=settings.JARVIS_BRIEFING_USE_LLM, clock=clock)


def build_briefing_tools_for(settings: Settings, llm: LLMProvider, zone, **services) -> list:
    """The read-only briefing tools, or none when briefings are disabled."""
    service = build_briefing_service(settings, llm, zone, **services)
    return build_briefing_tools(BriefingToolContext(service)) if service is not None else []


def build_event_service(settings: Settings, zone, tasks=None) -> EventService | None:
    if not settings.JARVIS_EVENTS_ENABLED:
        return None
    return EventService(
        EventRepository(SessionLocal), zone=zone, tasks=tasks, max_results=settings.JARVIS_EVENT_MAX_RESULTS,
        lookahead_days=settings.JARVIS_EVENT_DEFAULT_LOOKAHEAD_DAYS,
    )


def build_calendar_authenticator(settings: Settings) -> CalendarAuthenticator:
    """Calendar OAuth. The client comes from the credentials JSON, else CALENDAR_CLIENT_ID/SECRET, else the Gmail client
    values (one Google Desktop-app client can serve both APIs). The Calendar token is its own file."""
    client_id = settings.CALENDAR_CLIENT_ID or settings.GMAIL_CLIENT_ID
    client_secret = settings.CALENDAR_CLIENT_SECRET.get_secret_value() or settings.GMAIL_CLIENT_SECRET.get_secret_value()
    return CalendarAuthenticator(
        _project_path(settings.JARVIS_CALENDAR_CREDENTIALS_PATH), _project_path(settings.JARVIS_CALENDAR_TOKEN_PATH),
        client_id, client_secret, encrypt_at_rest=settings.JARVIS_ENCRYPT_TOKENS,
    )


def build_calendar_service(settings: Settings, zone) -> CalendarService | None:
    """The Google Calendar service, or None when Calendar is disabled. Never contacts Google when built."""
    if not settings.JARVIS_CALENDAR_ENABLED:
        return None
    auth = build_calendar_authenticator(settings)
    return CalendarService(HttpCalendarClient(auth, zone=zone), zone=zone, max_results=settings.JARVIS_CALENDAR_MAX_RESULTS, is_ready=lambda: auth.is_ready() and switch.enabled("calendar"))


def build_calendar_tools_for(settings: Settings, zone, *, events: EventService | None = None) -> list[CalendarTool]:
    """The Google Calendar tools, or none when Calendar is disabled. Building them never contacts Google and never fails
    for missing credentials: a request then gets a clear setup message (docs/google-calendar-integration.md)."""
    service = build_calendar_service(settings, zone)
    if service is None:
        return []
    context = CalendarToolContext(
        service, TimeParser(zone), utcnow, sync=CalendarEventSync(service, events), lookahead_days=settings.JARVIS_EVENT_DEFAULT_LOOKAHEAD_DAYS
    )
    return build_calendar_tools(context)


def build_event_tools_for(
    settings: Settings, zone, *, tasks=None, gmail=None, rag=None, memory=None, graph=None, events: EventService | None = None
) -> list[EventTool]:
    """The event/deadline tools, or none when events are disabled. Events live in the local database; sources
    (Gmail, documents, memory) are only read when the user asks. These tools never talk to any calendar."""
    if not settings.JARVIS_EVENTS_ENABLED:
        return []
    events = events or build_event_service(settings, zone, tasks)
    context = EventToolContext(
        events, TimeParser(zone), utcnow, tasks=tasks, gmail=gmail, rag=rag, memory=memory,
        linker=EventGraphLinker(graph) if graph is not None else None,
    )
    return build_event_tools(context)


def _build_conversation(settings: Settings, task_system: TaskSystem | None = None, intelligence=None, bus=None) -> ConversationEngine:
    llm = MeteredLLM(_build_llm(settings))  # counts LLM calls and latency (backend.core.metrics)
    memory = _build_memory(settings)
    rag = build_rag_service(settings, llm)
    graph = build_graph_service(settings)
    if graph is not None:
        # Keep derived graph facts consistent with memory and the document index.
        if memory is not None:
            memory.add_listener(MemoryGraphSync(graph).handle)
        if rag is not None:
            rag.add_listener(DocumentGraphSync(graph).handle)

    tools = list(task_system.tools) if task_system is not None else []
    gmail = build_gmail_service(settings, llm)
    events_service = None
    if settings.JARVIS_GMAIL_ENABLED or settings.JARVIS_EVENTS_ENABLED or settings.JARVIS_CALENDAR_ENABLED:
        zone = task_system.parser.zone if task_system is not None else resolve_timezone(settings.JARVIS_TIMEZONE)
        tasks_service = task_system.tasks if task_system is not None else None
        events_service = build_event_service(settings, zone, tasks_service)
        if gmail is not None:
            tools += build_gmail_tools(GmailToolContext(gmail, zone, utcnow))
        tools += build_event_tools_for(
            settings, zone, tasks=tasks_service, gmail=gmail, rag=rag, memory=memory, graph=graph, events=events_service,
        )
        tools += build_calendar_tools_for(settings, zone, events=events_service)
    if settings.JARVIS_MESSAGING_ENABLED:
        zone = task_system.parser.zone if task_system is not None else resolve_timezone(settings.JARVIS_TIMEZONE)
        tools += build_messaging_tools_for(settings, llm, zone)
    if settings.JARVIS_PROACTIVE_ENABLED:
        zone = task_system.parser.zone if task_system is not None else resolve_timezone(settings.JARVIS_TIMEZONE)
        tools += build_proactive_tools_for(settings, zone)
    if settings.JARVIS_BRIEFING_ENABLED:
        zone = task_system.parser.zone if task_system is not None else resolve_timezone(settings.JARVIS_TIMEZONE)
        tasks_for_briefing = task_system.tasks if task_system is not None else None
        tools += build_briefing_tools_for(
            settings, llm, zone, tasks=tasks_for_briefing, reminders=task_system.reminders if task_system is not None else None,
            events=events_service or (build_event_service(settings, zone, tasks_for_briefing) if settings.JARVIS_EVENTS_ENABLED else None),
            gmail=gmail, calendar=build_calendar_service(settings, zone), messaging=build_messaging_service(settings, llm),
        )
    descriptors = [tool.descriptor() for tool in tools]
    agent = (
        AgentBrain(
            llm,
            tools=descriptors,
            max_plan_steps=settings.JARVIS_AGENT_MAX_PLAN_STEPS,
            documents_enabled=settings.JARVIS_RAG_ENABLED,
        )
        if settings.JARVIS_AGENT_ENABLED
        else None
    )
    permissions = PermissionManager(
        tools=[d.security_info() for d in descriptors],  # only the registered local tools are known; anything else is denied
        audit=AuditLog(enabled=settings.JARVIS_PERMISSION_AUDIT_ENABLED),
        default_expiry_seconds=settings.JARVIS_PERMISSION_DEFAULT_EXPIRY_SECONDS,
    )
    return ConversationEngine(
        llm=llm,
        max_messages=settings.JARVIS_MAX_CONVERSATION_MESSAGES,
        timeout_seconds=settings.JARVIS_CONVERSATION_TIMEOUT_SECONDS,
        agent=agent,
        memory=memory,
        rag=rag,
        graph=GraphContextProvider(graph) if graph is not None else None,
        permissions=permissions,
        actions=TaskActionExecutor(tools, permissions) if tools and agent is not None else None,
        intelligence=intelligence,
        bus=bus,
    )


def build_voice_engine(settings: Settings, task_system: TaskSystem | None = None, intelligence=None, bus=None) -> VoiceEngine:
    """Construct a VoiceEngine wired to the providers named in `settings`.

    Raises ProviderNotConfiguredError / AudioDeviceError with a clear
    message if any provider's model/config/hardware isn't available —
    never falls back to a fake provider.
    """
    if not settings.WAKE_WORD_ENABLED:
        raise ProviderNotConfiguredError(
            "WAKE_WORD_ENABLED is false; the voice engine requires wake-word "
            "detection to be enabled in Phase 1"
        )

    return VoiceEngine(
        wakeword=_build_wakeword(settings),
        stt=_build_stt(settings),
        conversation=_build_conversation(settings, task_system, intelligence, bus),
        tts=_build_tts(settings),
        audio_input=AudioInput(
            sample_rate=settings.AUDIO_SAMPLE_RATE, device=settings.MICROPHONE_DEVICE
        ),
        # No separate output-device setting in Phase 1 — playback uses the
        # system default speaker.
        audio_output=AudioOutput(),
        sample_rate=settings.AUDIO_SAMPLE_RATE,
        listen_seconds=settings.AUDIO_LISTEN_SECONDS,
        announcements=task_system.announcements if task_system is not None else None,
    )
