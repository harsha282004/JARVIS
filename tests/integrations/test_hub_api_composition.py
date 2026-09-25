"""Dashboard Integration Center API, health registration and production composition of the hub."""

import json
import time

import pytest
from fastapi.testclient import TestClient

from backend.core import integration_switch
from backend.core.config import Settings, get_settings
from backend.core.context import AppContext, get_context, set_context
from backend.core.health import ServiceState
from backend.main import app
from integrations.gmail.models import GmailAuthError
from integrations.hub.models import IntegrationStatus, Permission
from tests.hub_helpers import TOKEN, build_hub_harness
from tests.intelligence_helpers import email_raw


@pytest.fixture
def api(tmp_path):
    h = build_hub_harness(tmp_path, emails=[email_raw()])
    ctx = AppContext(settings=get_settings(), hub=h.hub, privacy=h.base.privacy, audit=h.base.audit)
    set_context(ctx)
    yield TestClient(app), ctx, h
    set_context(None)
    integration_switch.install(None)


def auth(ctx):
    return {"X-JARVIS-Token": ctx.api_token}


def test_integrations_endpoint_requires_the_token_and_never_returns_secrets(api):
    client, ctx, h = api
    assert client.get("/integrations").status_code == 401
    assert client.get("/integrations", headers={**auth(ctx), "Host": "evil.example.com"}).status_code == 403
    h.sync("github")
    body = client.get("/integrations", headers=auth(ctx)).json()
    names = {i["name"]: i for i in body["integrations"]}
    assert set(names) == {"gmail", "calendar", "github", "messaging", "documents"}
    assert names["github"]["status"] == "healthy" and names["github"]["last_sync_at"] and names["github"]["items"] > 0
    assert "READ_COMMITS" in names["github"]["permissions_granted"]
    assert "CREATE_EVENT" in names["calendar"]["permissions_granted"] and "DELETE_EVENT" not in names["calendar"]["permissions_granted"]
    assert TOKEN not in json.dumps(body)


def test_status_reflects_real_state_changes_through_the_api(api):
    client, ctx, h = api
    assert client.post("/integrations/gmail/disable", headers=auth(ctx)).json()["status"] == "disabled"
    assert not h.hub.registry.is_enabled("gmail")
    assert client.post("/integrations/gmail/enable", headers=auth(ctx)).json()["enabled"] is True
    h.gmail_auth.ready = False
    assert {i["name"]: i for i in client.get("/integrations", headers=auth(ctx)).json()["integrations"]}["gmail"]["status"] == "disconnected"


def test_sync_disconnect_and_permission_endpoints(api):
    client, ctx, h = api
    out = client.post("/integrations/gmail/sync", headers=auth(ctx)).json()
    assert out["ok"] and out["created"] == 3 and out["info"]["status"] == "healthy"
    granted = client.post("/integrations/calendar/permissions", json={"permission": "DELETE_EVENT", "granted": True}, headers=auth(ctx)).json()["permissions_granted"]
    assert granted.count("DELETE_EVENT") == 1
    assert client.post("/integrations/calendar/permissions", json={"permission": "READ_COMMITS", "granted": True}, headers=auth(ctx)).status_code == 422
    assert client.post("/integrations/calendar/permissions", json={"permission": "NOPE", "granted": True}, headers=auth(ctx)).status_code == 422
    gone = client.post("/integrations/gmail/disconnect", json={"purge": True, "revoke": True}, headers=auth(ctx)).json()
    assert gone["status"] == "disconnected" and h.gmail_auth.forgotten == 1 and h.hub.repo.count("gmail") == 0
    assert client.post("/integrations/nothing/enable", headers=auth(ctx)).status_code == 404


def _wait(predicate):
    for _ in range(80):
        if predicate():
            return True
        time.sleep(0.05)
    return False


def test_connect_runs_in_the_background_and_records_failures(api):
    client, ctx, h = api
    h.gmail_auth.ready = False
    assert client.post("/integrations/gmail/connect", headers=auth(ctx)).status_code == 202
    assert _wait(lambda: h.gmail_auth.authorized == 1 and h.hub.registry.info("gmail").status is not IntegrationStatus.AUTHENTICATING)
    assert h.hub.registry.info("gmail").configured
    h.gmail_auth.ready = False
    h.gmail_auth.fail = GmailAuthError("denied")
    client.post("/integrations/gmail/connect", headers=auth(ctx))
    assert _wait(lambda: h.hub.registry.info("gmail").last_error_kind == "AUTH_ERROR")


def test_endpoints_say_503_when_the_hub_is_not_running():
    ctx = AppContext(settings=get_settings())
    set_context(ctx)
    try:
        assert TestClient(app).get("/integrations", headers={"X-JARVIS-Token": get_context().api_token}).status_code == 503
    finally:
        set_context(None)


def test_dashboard_page_contains_the_integration_center():
    from pathlib import Path

    html = (Path(__file__).resolve().parents[2] / "backend" / "api" / "dashboard.html").read_text(encoding="utf-8")
    assert 'id="integrations"' in html and "/integrations" in html and "Permissions" in html and "Sync now" in html and "Disconnect" in html


# ---- health & production composition -------------------------------------------------------------------------------------------------

def test_registry_health_maps_status_truthfully(tmp_path):
    from desktop.runtime.health_checks import registry_check

    h = build_hub_harness(tmp_path)
    check = registry_check(h.hub.registry, "github")
    h.sync("github")
    assert check().state is ServiceState.HEALTHY
    h.github.status_override = 503
    h.base.clock.advance(hours=1)
    h.hub.engine.sync("github", force=True)
    assert check().state is ServiceState.DEGRADED
    h.hub.registry.set_enabled("github", False)
    assert check().state is ServiceState.DISABLED
    h.github_store.forget()
    h.hub.registry.set_enabled("github", True)
    assert check().state is ServiceState.DISABLED and "not set up" in check().detail  # no token: reported as not set up, never as healthy


def test_composition_registers_all_five_integrations_and_reports_unconfigured_ones_truthfully(tmp_path):
    from desktop.runtime.composition import build_runtime_services

    settings = Settings(_env_file=None, DATABASE_URL="sqlite://", JARVIS_STATE_DIR=str(tmp_path / "state"), JARVIS_GMAIL_ENABLED=True, JARVIS_CALENDAR_ENABLED=False,
                        JARVIS_GITHUB_ENABLED=True, JARVIS_DOCUMENT_DIRS=str(tmp_path), JARVIS_RAG_ENABLED=False, JARVIS_MEMORY_ENABLED=False, JARVIS_EVENTS_ENABLED=False)
    services = build_runtime_services(settings, None, tmp_path)
    try:
        hub = services.hub
        info = {i.name: i for i in hub.registry.all_info()}
        assert set(info) == {"gmail", "calendar", "github", "messaging", "documents"}
        assert info["gmail"].status is IntegrationStatus.DISCONNECTED  # enabled in config, but no sign-in yet
        assert info["calendar"].status is IntegrationStatus.DISCONNECTED and not info["calendar"].configured  # placeholder: JARVIS_CALENDAR_ENABLED is false
        assert info["github"].status is IntegrationStatus.DISCONNECTED  # enabled but no token yet
        assert info["documents"].status is IntegrationStatus.DISCONNECTED  # no RAG here, so nothing is watched
        assert services.sync_runner is not None and services.intelligence_router is not None
        assert integration_switch.enabled("gmail")
        hub.registry.set_enabled("gmail", False)
        assert not integration_switch.enabled("gmail")  # the registry is installed as the global switch
        assert Permission.CREATE_EVENT not in hub.registry.granted("calendar")
    finally:
        integration_switch.install(None)
