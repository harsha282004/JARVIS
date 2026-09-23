"""Builds a VoiceEngine from application settings.

Provider selection goes through the *_PROVIDER config values so swapping an
implementation later is a config change, not a code change at call sites —
today only one concrete implementation exists per interface.
"""

from agent.brain.brain import AgentBrain
from agent.memory.models import Confidence
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


def _build_conversation(settings: Settings) -> ConversationEngine:
    llm = _build_llm(settings)
    # No tools exist yet, so the brain is given an empty tool catalog.
    agent = (
        AgentBrain(llm, tools=[], max_plan_steps=settings.JARVIS_AGENT_MAX_PLAN_STEPS)
        if settings.JARVIS_AGENT_ENABLED
        else None
    )
    return ConversationEngine(
        llm=llm,
        max_messages=settings.JARVIS_MAX_CONVERSATION_MESSAGES,
        timeout_seconds=settings.JARVIS_CONVERSATION_TIMEOUT_SECONDS,
        agent=agent,
        memory=_build_memory(settings),
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
