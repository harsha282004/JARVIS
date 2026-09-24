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
from integrations.gmail.auth import GmailAuthenticator
from integrations.gmail.client import HttpGmailClient
from integrations.gmail.service import GmailService
from integrations.gmail.tools import GmailTool, GmailToolContext, build_gmail_tools
from agent.memory.policy import MemoryPolicy
from agent.memory.repository import MemoryRepository
from agent.memory.service import MemoryService
from pathlib import Path

from backend.core.config import Settings
from backend.core.database import SessionLocal
from backend.core.conversation.engine import ConversationEngine
from backend.core.llm.base import LLMProvider
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
    settings: Settings, system: TaskSystem | None, desktop_send=None
) -> ReminderScheduler | None:
    """The scheduler for `system`. `desktop_send(title, message)` shows a local desktop notification (the tray's).
    None when reminders are off or no notification channel is enabled (they could never be delivered)."""
    if system is None or system.reminders is None:
        return None
    channels: list[tuple[str, NotificationService]] = []
    if settings.JARVIS_REMINDER_DESKTOP_NOTIFICATIONS and desktop_send is not None:
        channels.append(("desktop", DesktopNotifier(desktop_send)))
    if settings.JARVIS_REMINDER_VOICE_NOTIFICATIONS:
        channels.append(("voice", VoiceNotifier(system.announcements)))
    if not channels:
        logger.warning("No reminder notification channel is available; the reminder scheduler is not started")
        return None
    return ReminderScheduler(
        system.reminders,
        CompositeNotifier(channels),
        tasks=system.tasks,
        poll_seconds=settings.JARVIS_REMINDER_POLL_SECONDS,
        missed_policy=MissedPolicy(settings.JARVIS_MISSED_REMINDER_POLICY),
    )


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
    )


def build_gmail_service(settings: Settings, llm: LLMProvider) -> GmailService | None:
    """The Gmail service, or None when Gmail is disabled. Building it never contacts Google and never fails for
    missing credentials: a request then gets a clear setup message (see docs/gmail-intelligence.md)."""
    if not settings.JARVIS_GMAIL_ENABLED:
        return None
    auth = build_gmail_authenticator(settings)
    return GmailService(
        HttpGmailClient(auth), llm, max_results=settings.JARVIS_GMAIL_MAX_RESULTS, is_ready=auth.is_ready
    )


def build_gmail_tools_for(settings: Settings, llm: LLMProvider, zone) -> list[GmailTool]:
    """The read-only Gmail tools, or none when Gmail is disabled. Summaries use the same local LLM as everything else."""
    service = build_gmail_service(settings, llm)
    return build_gmail_tools(GmailToolContext(service, zone, utcnow)) if service is not None else []


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
        client_id, client_secret,
    )


def build_calendar_tools_for(settings: Settings, zone, *, events: EventService | None = None) -> list[CalendarTool]:
    """The Google Calendar tools, or none when Calendar is disabled. Building them never contacts Google and never fails
    for missing credentials: a request then gets a clear setup message (docs/google-calendar-integration.md)."""
    if not settings.JARVIS_CALENDAR_ENABLED:
        return []
    auth = build_calendar_authenticator(settings)
    service = CalendarService(
        HttpCalendarClient(auth, zone=zone), zone=zone, max_results=settings.JARVIS_CALENDAR_MAX_RESULTS, is_ready=auth.is_ready
    )
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


def _build_conversation(settings: Settings, task_system: TaskSystem | None = None) -> ConversationEngine:
    llm = _build_llm(settings)
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
    )


def build_voice_engine(settings: Settings, task_system: TaskSystem | None = None) -> VoiceEngine:
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
        conversation=_build_conversation(settings, task_system),
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
