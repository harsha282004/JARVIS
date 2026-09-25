"""Integration Hub core: error classification, permissions, registry status, normalized store, secrets at rest."""

import json
import sys
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from sqlalchemy import create_engine, inspect
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.core.events import EventBus, SystemEvent
from backend.models.base import Base
from integrations.calendar.models import CalendarAuthRevoked, CalendarNotFound, CalendarUnavailable
from integrations.github.models import GitHubAuthError, GitHubRateLimited
from integrations.gmail.models import GmailAuthRevoked, GmailPermissionDenied, GmailRateLimited, GmailResponseError
from integrations.hub.models import ErrorKind, HubError, IntegrationStatus, ItemKind, NormalizedItem, Permission, ToolResult, WRITE_PERMISSIONS, classify_error
from integrations.hub.registry import IntegrationAdapter, IntegrationRegistry, SyncBatch, UnconfiguredAdapter
from integrations.hub.repository import HubRepository
from integrations.messaging.models import MessagingRateLimited, MessagingUnavailable, UnsupportedCapability

import backend.models.hub  # noqa: F401

NOW = datetime(2026, 9, 24, 9, tzinfo=timezone.utc)


# ---- error classification ------------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("exc,kind", [
    (GmailAuthRevoked(), ErrorKind.AUTH_ERROR), (CalendarAuthRevoked(), ErrorKind.AUTH_ERROR), (GitHubAuthError(), ErrorKind.AUTH_ERROR),
    (GmailPermissionDenied(), ErrorKind.PERMISSION_ERROR), (GmailRateLimited(), ErrorKind.RATE_LIMIT), (GitHubRateLimited(retry_after=30), ErrorKind.RATE_LIMIT),
    (MessagingRateLimited(), ErrorKind.RATE_LIMIT), (CalendarUnavailable(), ErrorKind.NETWORK_ERROR), (MessagingUnavailable(), ErrorKind.NETWORK_ERROR),
    (CalendarNotFound(), ErrorKind.NOT_FOUND), (GmailResponseError("x"), ErrorKind.SERVER_ERROR), (UnsupportedCapability("t", "send"), ErrorKind.INVALID_REQUEST),
    (ConnectionError("x"), ErrorKind.NETWORK_ERROR), (TimeoutError(), ErrorKind.NETWORK_ERROR), (httpx.ConnectError("x"), ErrorKind.NETWORK_ERROR),
    (FileNotFoundError("token"), ErrorKind.CONFIGURATION_ERROR), (KeyError("x"), ErrorKind.SERVER_ERROR), (RuntimeError("weird"), ErrorKind.UNKNOWN_ERROR),
])
def test_errors_are_classified(exc, kind):
    assert classify_error(exc).kind is kind


def test_classified_messages_are_meaningful_and_carry_retry_after():
    err = classify_error(GmailAuthRevoked(), "Gmail")
    assert "authorize" in err.message.lower() or "sign in" in err.message.lower() or "again" in err.message.lower()
    assert classify_error(GitHubRateLimited(retry_after=42)).retry_after == 42.0
    assert "unexpected" in classify_error(RuntimeError("secret details")).message.lower() and "secret details" not in classify_error(RuntimeError("secret details")).message


def test_tool_result_shapes():
    ok = ToolResult.ok("gmail", [1], count=1).to_dict()
    assert ok == {"success": True, "source": "gmail", "data": [1], "metadata": {"count": 1}, "error": None}
    bad = ToolResult.from_exception("gmail", GmailAuthRevoked(), "Gmail").to_dict()
    assert bad["success"] is False and bad["data"] is None and bad["error"]["type"] == "AUTH_ERROR" and bad["error"]["message"]


# ---- registry ----------------------------------------------------------------------------------------------------------------------

class Fake(IntegrationAdapter):
    name, display_name = "fake", "Fake"
    permissions = frozenset({Permission.READ_EMAIL, Permission.CREATE_EVENT, Permission.READ_ATTACHMENT})

    def __init__(self, configured=True):
        self.configured, self.auth_calls, self.health_fail, self.disconnected, self.revoked = configured, 0, None, 0, 0

    def is_configured(self):
        return self.configured

    def authenticate(self):
        self.auth_calls += 1
        if self.health_fail:
            raise self.health_fail
        self.configured = True

    def health_check(self):
        if self.health_fail:
            raise self.health_fail
        return "ok"

    def disconnect(self):
        self.disconnected += 1
        self.configured = False

    def revoke(self):
        self.revoked += 1
        return True

    def sync(self, cursor, limit):
        return SyncBatch([], cursor)


def registry(tmp_path=None, adapter=None, bus=None):
    reg = IntegrationRegistry(tmp_path / "i.json" if tmp_path else None, bus, clock=lambda: NOW)
    reg.register(adapter or Fake())
    return reg


def test_write_and_content_copying_permissions_are_not_granted_by_default(tmp_path):
    reg = registry(tmp_path)
    assert reg.granted("fake") == {Permission.READ_EMAIL}
    assert reg.allowed("fake", Permission.READ_EMAIL) == (True, "")
    ok, why = reg.allowed("fake", Permission.CREATE_EVENT)
    assert not ok and "CREATE_EVENT" in why
    assert Permission.CREATE_EVENT in WRITE_PERMISSIONS
    assert reg.grant("fake", Permission.CREATE_EVENT) and reg.allowed("fake", Permission.CREATE_EVENT)[0]
    reg.revoke_permission("fake", Permission.CREATE_EVENT)
    assert not reg.allowed("fake", Permission.CREATE_EVENT)[0]
    assert not reg.grant("fake", Permission.READ_COMMITS)  # a permission the integration does not have cannot be granted


def test_settings_persist_across_restart(tmp_path):
    reg = registry(tmp_path)
    reg.grant("fake", Permission.CREATE_EVENT)
    reg.set_enabled("fake", False)
    again = registry(tmp_path)
    assert not again.is_enabled("fake") and Permission.CREATE_EVENT in again.granted("fake")


def test_status_is_derived_from_recorded_facts(tmp_path):
    a = Fake()
    reg = registry(tmp_path, a)
    assert reg.info("fake").status is IntegrationStatus.CONNECTED  # configured, never synchronized
    reg.record_success("fake", "c1")
    assert reg.info("fake").status is IntegrationStatus.HEALTHY and reg.info("fake").last_sync_at == NOW
    reg.set_syncing("fake", True)
    assert reg.info("fake").status is IntegrationStatus.SYNCING
    reg.set_syncing("fake", False)
    reg.record_failure("fake", HubError(ErrorKind.NETWORK_ERROR, "offline"), NOW + timedelta(minutes=1))
    assert reg.info("fake").status is IntegrationStatus.DEGRADED
    reg.record_failure("fake", HubError(ErrorKind.AUTH_ERROR, "Please reconnect."), None)
    assert reg.info("fake").status is IntegrationStatus.DISCONNECTED and "reconnect" in reg.info("fake").detail.lower()
    reg.record_failure("fake", HubError(ErrorKind.UNKNOWN_ERROR, "odd"), None)
    assert reg.info("fake").status is IntegrationStatus.ERROR
    reg.set_enabled("fake", False)
    assert reg.info("fake").status is IntegrationStatus.DISABLED
    a.configured = False
    reg.set_enabled("fake", True)
    assert reg.info("fake").status is IntegrationStatus.DISCONNECTED


def test_is_connected_answers_honestly(tmp_path):
    reg = registry(tmp_path, Fake(configured=False))
    ok, sentence = reg.is_connected("fake")
    assert not ok and "not connected" in sentence
    assert reg.is_connected("nothing")[0] is False
    reg2 = registry(tmp_path / "x" if False else None, Fake())
    reg2.record_success("fake", None)
    assert reg2.is_connected("fake")[0] and "Last synchronized just now" in reg2.is_connected("fake")[1]


def test_connect_records_outcome_and_publishes(tmp_path):
    bus, seen = EventBus(), []
    bus.subscribe(SystemEvent.INTEGRATION_CONNECTED, lambda e: seen.append(e.payload["integration"]))
    a = Fake(configured=False)
    reg = registry(tmp_path, a, bus)
    info = reg.connect("fake")
    assert info.status in (IntegrationStatus.CONNECTED, IntegrationStatus.HEALTHY) and a.auth_calls == 1 and seen == ["fake"]
    a2 = Fake(configured=False)
    a2.health_fail = GmailAuthRevoked()
    reg2 = registry(None, a2)
    info2 = reg2.connect("fake")
    assert info2.last_error_kind == "AUTH_ERROR" and info2.status is IntegrationStatus.DISCONNECTED  # the failure is recorded, not raised, not hidden


def test_disconnect_forgets_credentials_revokes_and_purges(tmp_path):
    a, purged = Fake(), []
    reg = registry(tmp_path, a)
    reg.record_success("fake", "cursor")
    info = reg.disconnect("fake", revoke=True, purge=lambda n: purged.append(n) or 3)
    assert a.disconnected == 1 and a.revoked == 1 and purged == ["fake"]
    assert info.status is IntegrationStatus.DISCONNECTED and reg.cursor("fake") is None and info.last_sync_at is None


def test_unconfigured_placeholder_reports_truthfully(tmp_path):
    reg = IntegrationRegistry(None)
    reg.register(UnconfiguredAdapter("github", "GitHub", frozenset({Permission.READ_COMMITS}), "Set JARVIS_GITHUB_ENABLED=true"))
    info = reg.info("github")
    assert info.status is IntegrationStatus.DISCONNECTED and not info.configured and "not set up" in info.detail
    assert not reg.allowed("github", Permission.READ_COMMITS)[0]


def test_adapter_capabilities_come_from_what_it_actually_implements():
    assert Fake().supported == {"authenticate", "health_check", "disconnect", "revoke", "sync"}
    assert UnconfiguredAdapter("x", "X", frozenset(), "").supported == frozenset()


# ---- normalized store --------------------------------------------------------------------------------------------------------------

@pytest.fixture
def repo():
    engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    return HubRepository(sessionmaker(bind=engine, expire_on_commit=False), clock=lambda: NOW)


def item(sid="m1", title="Project review", summary="s", ts=NOW, source="gmail", kind=ItemKind.EMAIL, **meta):
    return NormalizedItem(kind, source, sid, ts, title, summary, meta, "high", sid, NOW)


def test_upsert_is_idempotent_and_detects_changes(repo):
    assert repo.upsert(item()) == "created"
    assert repo.upsert(item()) == "unchanged"  # the same source item retrieved again: no new row, no change
    assert repo.upsert(item(title="Project review moved")) == "updated"
    assert repo.count("gmail") == 1
    assert repo.get("gmail", ItemKind.EMAIL, "m1").title == "Project review moved"


def test_same_id_in_different_sources_or_kinds_are_different_items(repo):
    repo.upsert(item("1"))
    repo.upsert(item("1", source="telegram", kind=ItemKind.MESSAGE))
    repo.upsert(item("1", kind=ItemKind.EVENT))
    assert repo.count() == 3


def test_search_is_parameterized_and_matches_all_words(repo):
    repo.upsert(item("a", "Hackathon registration"))
    repo.upsert(item("b", "Project review"))
    assert [i.source_id for i in repo.search("hackathon")] == ["a"]
    assert repo.search("project hackathon") == []
    assert repo.search("'; DROP TABLE hub_items; --") == [] and repo.count() == 2  # text is data, never SQL


def test_removed_items_are_hidden_and_revived_when_they_return(repo):
    repo.upsert(item("e1", kind=ItemKind.EVENT, source="calendar"))
    assert repo.mark_deleted("calendar", ItemKind.EVENT, ["e1"]) == 1
    assert repo.count("calendar") == 0 and repo.get("calendar", ItemKind.EVENT, "e1") is None
    assert repo.upsert(item("e1", kind=ItemKind.EVENT, source="calendar")) == "updated" and repo.count("calendar") == 1


def test_purge_and_retention(repo):
    repo.upsert(item("a"))
    repo.upsert(item("b", source="github", kind=ItemKind.COMMIT))
    assert repo.purge("gmail") == 1 and repo.count() == 1
    later = HubRepository(repo._sf, clock=lambda: NOW + timedelta(days=100))
    assert later.prune(90) == 1 and later.count() == 0


def test_migration_creates_the_table_and_indexes_on_a_scratch_database(tmp_path):
    from alembic import command
    from alembic.config import Config
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    cfg = Config(str(root / "database" / "alembic.ini"))
    cfg.set_main_option("script_location", str(root / "database" / "migrations"))
    url = f"sqlite:///{(tmp_path / 'm.db').as_posix()}"
    cfg.cmd_opts = type("O", (), {"x": [f"url={url}"]})()
    command.upgrade(cfg, "0007_hub")
    insp = inspect(create_engine(url))
    assert "hub_items" in insp.get_table_names()
    assert any(i["column_names"] == ["source", "kind", "source_timestamp"] for i in insp.get_indexes("hub_items"))
    assert any(u["name"] == "uq_hub_items_source" for u in insp.get_unique_constraints("hub_items"))
    command.downgrade(cfg, "0006_proactive")
    assert "hub_items" not in inspect(create_engine(url)).get_table_names()


# ---- secrets at rest ---------------------------------------------------------------------------------------------------------------

def test_secret_files_roundtrip_and_plaintext_fallback(tmp_path):
    from backend.core.secrets import MARKER, is_encrypted, read_secret, write_secret

    p = tmp_path / "t"
    encrypted = write_secret(p, "sekret-value")
    assert read_secret(p) == "sekret-value"
    if sys.platform == "win32":
        assert encrypted and is_encrypted(p) and "sekret-value" not in p.read_text(encoding="utf-8") and p.read_text(encoding="utf-8").startswith(MARKER)
    p.write_text("old-plaintext-token")
    assert read_secret(p) == "old-plaintext-token" and not is_encrypted(p)  # tokens written before encryption existed still work


@pytest.mark.skipif(sys.platform != "win32", reason="DPAPI is Windows-only")
def test_corrupted_encrypted_secret_is_a_clean_error_not_a_traceback_with_content(tmp_path):
    from backend.core.secrets import MARKER, SecretError, read_secret

    p = tmp_path / "t"
    p.write_text(MARKER + "bm90IGRwYXBp")
    with pytest.raises(SecretError):
        read_secret(p)


def test_google_token_can_be_stored_encrypted_and_reloaded_and_forgotten(tmp_path):
    from google.oauth2.credentials import Credentials

    from integrations.gmail.auth import GmailAuthenticator

    auth = GmailAuthenticator(tmp_path / "c.json", tmp_path / "tok.json", "cid", "csecret", encrypt_at_rest=True)
    creds = Credentials(token="access-1", refresh_token="refresh-1", client_id="cid", client_secret="csecret", token_uri="https://oauth2.googleapis.com/token", scopes=list(auth.scopes))
    auth._save(creds)
    raw = (tmp_path / "tok.json").read_text(encoding="utf-8")
    if sys.platform == "win32":
        assert "refresh-1" not in raw and "csecret" not in raw  # neither the refresh token nor the client secret is readable on disk
    loaded = auth._load()
    assert loaded.refresh_token == "refresh-1" and auth.is_ready()
    assert auth.forget() is True and not auth.is_ready() and auth.forget() is False


def test_revoke_remote_sends_the_token_only_to_google_and_reports_success(tmp_path):
    from google.oauth2.credentials import Credentials

    from integrations.gmail.auth import GmailAuthenticator

    seen = []

    def handler(request: httpx.Request):
        seen.append((str(request.url), request.content.decode()))
        return httpx.Response(200)

    auth = GmailAuthenticator(tmp_path / "c.json", tmp_path / "tok.json", "cid", "csecret")
    auth._save(Credentials(token="access-1", refresh_token="r", client_id="cid", client_secret="s", token_uri="https://oauth2.googleapis.com/token", scopes=list(auth.scopes),
                           expiry=(datetime.now(timezone.utc) + timedelta(hours=1)).replace(tzinfo=None)))
    assert auth.revoke_remote(httpx.Client(transport=httpx.MockTransport(handler))) is True
    assert seen == [("https://oauth2.googleapis.com/revoke", "token=access-1")]
    assert GmailAuthenticator(tmp_path / "x.json", tmp_path / "none.json").revoke_remote() is False  # nothing to revoke without a token
    _ = json
