"""Configuration loading tests."""

import os

import pytest
from pydantic import ValidationError

from backend.core.config import Settings, get_settings


def test_settings_load_from_environment():
    settings = get_settings()
    assert settings.APP_NAME == "JARVIS"
    assert settings.DATABASE_URL


def test_settings_defaults_applied():
    settings = get_settings()
    assert settings.API_HOST == "127.0.0.1"
    assert settings.API_PORT == 8000
    assert settings.LOG_LEVEL == "INFO"


def test_missing_required_setting_fails_clearly(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("APP_ENV", "test")
        mp.delenv("DATABASE_URL", raising=False)
        with pytest.raises(ValidationError):
            Settings(_env_file=None)
