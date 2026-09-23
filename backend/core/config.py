"""Centralized application configuration.

Settings are loaded from environment variables (and a local `.env` file in
development). Required values have no fabricated defaults: if a required
setting is missing, startup fails with a clear error instead of guessing.
"""

from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=True,
        extra="ignore",
    )

    # --- Application ---
    APP_ENV: Literal["development", "test", "production"] = "development"
    APP_NAME: str = "JARVIS"
    LOG_LEVEL: str = "INFO"

    # --- API server ---
    API_HOST: str = "127.0.0.1"
    API_PORT: int = 8000

    # --- Database ---
    # Required: no default. Startup must fail clearly if this is unset
    # rather than silently falling back to a fabricated connection string.
    DATABASE_URL: str = Field(...)

    # --- LLM provider ---
    LLM_PROVIDER: str = "ollama"
    LLM_MODEL: str = "llama3"
    OLLAMA_BASE_URL: str = "http://localhost:11434"


@lru_cache
def get_settings() -> Settings:
    """Return a cached Settings instance.

    Raises pydantic.ValidationError if required environment variables are
    missing or malformed.
    """
    return Settings()
