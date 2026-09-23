"""FastAPI application startup and /health endpoint tests."""

from fastapi.testclient import TestClient

from backend.main import app


def test_health_endpoint_returns_ok():
    client = TestClient(app)
    response = client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["service"] == "JARVIS"
