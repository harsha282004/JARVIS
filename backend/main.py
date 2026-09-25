"""FastAPI application entrypoint.

Run locally with:
    uvicorn backend.main:app --reload --host 127.0.0.1 --port 8000
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from backend.api.routes.health import router as health_router
from backend.api.routes.system import router as system_router
from backend.core.config import get_settings
from backend.core.logging import configure_logging, get_logger

settings = get_settings()
configure_logging(settings.LOG_LEVEL)
logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    logger.info("%s starting up (env=%s)", settings.APP_NAME, settings.APP_ENV)
    yield


app = FastAPI(title=settings.APP_NAME, lifespan=lifespan)
app.include_router(health_router)
app.include_router(system_router)
