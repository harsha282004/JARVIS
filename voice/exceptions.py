"""Shared exceptions for the voice pipeline.

Providers raise these instead of fabricating a result when a model file,
device, or backend is missing or fails.
"""


class VoiceProviderError(Exception):
    """Base exception for voice pipeline failures."""


class ProviderNotConfiguredError(VoiceProviderError):
    """Raised when a provider is missing required configuration or model files."""


class AudioDeviceError(VoiceProviderError):
    """Raised when a microphone or speaker device cannot be opened or used."""
