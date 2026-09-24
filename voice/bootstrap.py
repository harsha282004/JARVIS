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
from agent.memory.policy import MemoryPolicy
from agent.memory.repository import MemoryRepository
from agent.memory.service import MemoryService
from backend.core.config import Settings
from backend.core.database import SessionLocal
from backend.core.conversation.engine import ConversationEngine
from backend.core.llm.base import LLMProvider
from backend.core.llm.ollama_provider import OllamaProvider
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


def _build_conversation(settings: Settings) -> ConversationEngine:
    llm = _build_llm(settings)
    # No tools exist yet, so the brain is given an empty tool catalog.
    agent = (
        AgentBrain(
            llm,
            tools=[],
            max_plan_steps=settings.JARVIS_AGENT_MAX_PLAN_STEPS,
            documents_enabled=settings.JARVIS_RAG_ENABLED,
        )
        if settings.JARVIS_AGENT_ENABLED
        else None
    )
    memory = _build_memory(settings)
    rag = build_rag_service(settings, llm)
    graph = build_graph_service(settings)
    if graph is not None:
        # Keep derived graph facts consistent with memory and the document index.
        if memory is not None:
            memory.add_listener(MemoryGraphSync(graph).handle)
        if rag is not None:
            rag.add_listener(DocumentGraphSync(graph).handle)
    return ConversationEngine(
        llm=llm,
        max_messages=settings.JARVIS_MAX_CONVERSATION_MESSAGES,
        timeout_seconds=settings.JARVIS_CONVERSATION_TIMEOUT_SECONDS,
        agent=agent,
        memory=memory,
        rag=rag,
        graph=GraphContextProvider(graph) if graph is not None else None,
        permissions=PermissionManager(
            tools=[],  # no tools exist yet, so every requested tool is denied as unknown
            audit=AuditLog(enabled=settings.JARVIS_PERMISSION_AUDIT_ENABLED),
            default_expiry_seconds=settings.JARVIS_PERMISSION_DEFAULT_EXPIRY_SECONDS,
        ),
    )


def build_voice_engine(settings: Settings) -> VoiceEngine:
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
        conversation=_build_conversation(settings),
        tts=_build_tts(settings),
        audio_input=AudioInput(
            sample_rate=settings.AUDIO_SAMPLE_RATE, device=settings.MICROPHONE_DEVICE
        ),
        # No separate output-device setting in Phase 1 — playback uses the
        # system default speaker.
        audio_output=AudioOutput(),
        sample_rate=settings.AUDIO_SAMPLE_RATE,
        listen_seconds=settings.AUDIO_LISTEN_SECONDS,
    )
