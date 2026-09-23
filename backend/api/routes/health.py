"""Health check route."""

from fastapi import APIRouter

from backend.core.config import get_settings

router = APIRouter()


@router.get("/health")
def health() -> dict:
    settings = get_settings()
    return {"status": "ok", "service": settings.APP_NAME}
