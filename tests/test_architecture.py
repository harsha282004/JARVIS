"""Import-boundary smoke tests.

Confirms core architectural components import cleanly and independently,
catching accidental circular imports between backend/agent/voice/integrations.
"""


def test_backend_core_imports():
    from backend.core import config, database, logging, security  # noqa: F401


def test_backend_llm_interface_imports():
    from backend.core.llm.base import LLMProvider

    assert hasattr(LLMProvider, "generate")


def test_agent_interfaces_import():
    from agent.memory.base import MemoryInterface
    from agent.tools.base import Tool

    assert hasattr(Tool, "run")
    assert hasattr(MemoryInterface, "store")


def test_voice_interface_imports():
    from voice.base import VoiceProvider

    assert hasattr(VoiceProvider, "is_ready")


def test_integrations_interface_imports():
    from integrations.base import Integration

    assert hasattr(Integration, "is_configured")


def test_security_permission_manager_denies_by_default():
    from backend.core.security import PermissionManager, PermissionRequest

    manager = PermissionManager()
    assert manager.authorize(PermissionRequest(tool_name="example", action="read")) is False
