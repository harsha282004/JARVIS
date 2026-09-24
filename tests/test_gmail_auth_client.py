"""Gmail OAuth handling (real google-auth Credentials, network patched) and the HTTP client (httpx.MockTransport).

No real Google account is contacted here; see tests/integration for the opt-in real Gmail test.
"""

import json
import logging
import os
import stat
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from google.auth.exceptions import RefreshError, TransportError

from integrations.gmail.auth import GMAIL_READONLY_SCOPE, SCOPES, GmailAuthenticator
from integrations.gmail.client import GMAIL_API_BASE, HARD_MAX_RESULTS, HttpGmailClient, validate_id
from integrations.gmail.models import (
    GmailAuthError,
    GmailAuthRevoked,
    GmailNotConfigured,
    GmailNotFound,
    GmailPermissionDenied,
    GmailRateLimited,
    GmailResponseError,
    GmailUnavailable,
)
from tests.gmail_helpers import raw_message

SECRET_TOKEN = "ya29.SECRET-ACCESS-TOKEN"
REFRESH_SECRET = "1//SECRET-REFRESH-TOKEN"
CLIENT_SECRET = "GOCSPX-SECRET-CLIENT"


def write_token(path, token=None, refresh=REFRESH_SECRET):
    path.parent.mkdir(parents=True, exist_ok=True)
    info = {"token": token, "refresh_token": refresh, "client_id": "cid.apps.googleusercontent.com",
            "client_secret": CLIENT_SECRET, "token_uri": "https://oauth2.googleapis.com/token", "scopes": list(SCOPES)}
    path.write_text(json.dumps(info), encoding="utf-8")


def patch_refresh(monkeypatch, result=None, error=None):
    """Replace only google-auth's network call; Credentials' own logic (validity, expiry, to_json) is real."""
    import google.oauth2.reauth as greauth

    calls = []

    def fake_refresh_grant(request, token_uri, refresh_token, client_id, client_secret, scopes=None, rapt_token=None,
                           enable_reauth_refresh=False):
        calls.append(refresh_token)
        if error:
            raise error
        return (result or "fresh-access-token", refresh_token, datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=1), {}, rapt_token)

    monkeypatch.setattr(greauth, "refresh_grant", fake_refresh_grant)
    return calls


# ---- OAuth -----------------------------------------------------------------------------------------------------------


def test_only_the_read_only_scope_is_requested():
    assert SCOPES == (GMAIL_READONLY_SCOPE,) == ("https://www.googleapis.com/auth/gmail.readonly",)


def test_missing_credentials_and_token_are_reported_clearly(tmp_path):
    auth = GmailAuthenticator(tmp_path / "credentials.json", tmp_path / "token.json")
    status = auth.status()
    assert not status.configured and not status.authorized and not auth.is_ready()
    with pytest.raises(GmailNotConfigured) as exc:
        auth.access_token()
    assert "gmail_cli.py auth" in exc.value.user_message
    with pytest.raises(GmailNotConfigured):
        auth.authorize()  # no OAuth client file: nothing is opened or attempted


def test_status_reflects_the_files_without_touching_the_network(tmp_path):
    (tmp_path / "credentials.json").write_text("{}")
    auth = GmailAuthenticator(tmp_path / "credentials.json", tmp_path / "token.json")
    assert auth.status().configured and not auth.status().authorized
    write_token(tmp_path / "token.json")
    assert auth.status().authorized and auth.is_ready()


def test_unreadable_token_file_fails_safely(tmp_path, caplog):
    (tmp_path / "token.json").write_text("this is not json {")
    auth = GmailAuthenticator(tmp_path / "c.json", tmp_path / "token.json")
    with caplog.at_level(logging.DEBUG):
        with pytest.raises(GmailAuthError):
            auth.access_token()
    assert "this is not json" not in caplog.text


def test_expired_token_is_refreshed_cached_and_saved(tmp_path, monkeypatch):
    write_token(tmp_path / "token.json", token=None)
    calls = patch_refresh(monkeypatch)
    auth = GmailAuthenticator(tmp_path / "c.json", tmp_path / "token.json")
    assert auth.access_token() == "fresh-access-token"
    assert auth.access_token() == "fresh-access-token" and len(calls) == 1  # still valid: no second refresh
    saved = json.loads((tmp_path / "token.json").read_text())
    assert saved["token"] == "fresh-access-token" and saved["refresh_token"] == REFRESH_SECRET
    assert auth.access_token(force_refresh=True) and len(calls) == 2
    auth.invalidate()
    assert auth.access_token() and len(calls) == 3  # after a 401 the next call refreshes


def test_token_file_is_owner_only_where_the_platform_supports_it(tmp_path, monkeypatch):
    write_token(tmp_path / "token.json")
    patch_refresh(monkeypatch)
    GmailAuthenticator(tmp_path / "c.json", tmp_path / "token.json").access_token()
    if os.name != "nt":
        assert stat.S_IMODE(os.stat(tmp_path / "token.json").st_mode) == 0o600


def test_revoked_authorization_is_detected_and_not_retried(tmp_path, monkeypatch, caplog):
    write_token(tmp_path / "token.json")
    calls = patch_refresh(monkeypatch, error=RefreshError("invalid_grant: Token has been expired or revoked.", {"error": "invalid_grant"}))
    auth = GmailAuthenticator(tmp_path / "c.json", tmp_path / "token.json")
    with caplog.at_level(logging.DEBUG):
        with pytest.raises(GmailAuthRevoked) as exc:
            auth.access_token()
    assert "auth" in exc.value.user_message and len(calls) == 1
    assert REFRESH_SECRET not in caplog.text and CLIENT_SECRET not in caplog.text and "invalid_grant" not in caplog.text


def test_token_without_a_refresh_token_needs_reauthorization(tmp_path):
    write_token(tmp_path / "token.json", token=None, refresh=None)
    with pytest.raises(GmailAuthRevoked):
        GmailAuthenticator(tmp_path / "c.json", tmp_path / "token.json").access_token()


def test_network_failure_during_refresh_is_unavailable_not_revoked(tmp_path, monkeypatch):
    write_token(tmp_path / "token.json")
    patch_refresh(monkeypatch, error=TransportError("connection reset"))
    with pytest.raises(GmailUnavailable):
        GmailAuthenticator(tmp_path / "c.json", tmp_path / "token.json").access_token()


def test_unexpected_refresh_error_is_an_auth_error_without_details(tmp_path, monkeypatch):
    write_token(tmp_path / "token.json")
    patch_refresh(monkeypatch, error=ValueError(f"boom {CLIENT_SECRET}"))
    with pytest.raises(GmailAuthError) as exc:
        GmailAuthenticator(tmp_path / "c.json", tmp_path / "token.json").access_token()
    assert CLIENT_SECRET not in str(exc.value) and CLIENT_SECRET not in exc.value.user_message


# ---- HTTP client --------------------------------------------------------------------------------------------------------


class StaticAuth:
    def __init__(self, fail=None):
        self.tokens = 0
        self.invalidated = 0
        self.fail = fail

    def access_token(self, force_refresh=False):
        if self.fail:
            raise self.fail
        self.tokens += 1
        return SECRET_TOKEN

    def invalidate(self):
        self.invalidated += 1


def client_for(handler, auth=None):
    sleeps = []
    http = httpx.Client(transport=httpx.MockTransport(handler))
    return HttpGmailClient(auth or StaticAuth(), http=http, sleep=sleeps.append), sleeps


def json_response(data, status=200, headers=None):
    return httpx.Response(status, json=data, headers=headers)


def test_search_lists_then_fetches_each_message_read_only(tmp_path):
    seen = []

    def handler(request: httpx.Request):
        seen.append(request)
        if request.url.path.endswith("/messages"):
            return json_response({"messages": [{"id": "m2"}, {"id": "m1"}], "resultSizeEstimate": 2})
        return json_response(raw_message(id=request.url.path.rsplit("/", 1)[1], subject="S"))

    client, _ = client_for(handler)
    result = client.search("from:john is:unread", 10)
    assert [m.message_id for m in result.messages] == ["m2", "m1"] and result.estimated_total == 2 and not result.truncated
    assert all(r.method == "GET" for r in seen)  # never anything but GET
    assert all(str(r.url).startswith(GMAIL_API_BASE) for r in seen)  # the fixed Gmail host only
    assert seen[0].url.params["q"] == "from:john is:unread" and seen[0].url.params["maxResults"] == "10"
    assert seen[0].headers["authorization"] == f"Bearer {SECRET_TOKEN}"
    assert all(r.url.params.get("format") == "full" for r in seen[1:])


def test_result_count_is_bounded_and_pagination_reports_more():
    def handler(request):
        if request.url.path.endswith("/messages"):
            asked = int(request.url.params["maxResults"])
            return json_response({"messages": [{"id": f"m{i}"} for i in range(asked + 5)], "nextPageToken": "NEXT", "resultSizeEstimate": 5000})
        return json_response(raw_message(id=request.url.path.rsplit("/", 1)[1]))

    client, _ = client_for(handler)
    result = client.search("", 500)  # an absurd request is clamped
    assert result.count == HARD_MAX_RESULTS == 50
    assert result.truncated and result.next_page_token == "NEXT" and result.estimated_total == 5000


def test_pagination_pages_are_bounded_and_the_token_is_forwarded():
    pages = []

    def handler(request):
        if request.url.path.endswith("/messages"):
            pages.append(request.url.params.get("pageToken"))
            return json_response({"messages": [{"id": f"m{len(pages)}"}], "nextPageToken": f"p{len(pages)}"})
        return json_response(raw_message(id=request.url.path.rsplit("/", 1)[1]))

    client, _ = client_for(handler)
    result = client.search("", 30, page_token="START")
    assert pages == ["START", "p1", "p2"]  # at most three list calls, however many results are still missing
    assert result.count == 3 and result.next_page_token == "p3"


def test_no_results_and_deleted_messages_are_handled():
    def handler(request):
        if request.url.path.endswith("/messages"):
            return json_response({"resultSizeEstimate": 0} if request.url.params.get("q") == "none" else {"messages": [{"id": "gone"}, {"id": "ok"}]})
        if request.url.path.endswith("/gone"):
            return httpx.Response(404, json={"error": {"code": 404}})
        return json_response(raw_message(id="ok"))

    client, _ = client_for(handler)
    assert client.search("none", 5).messages == []
    assert [m.message_id for m in client.search("x", 5).messages] == ["ok"]  # deleted between list and get


def test_get_message_and_thread():
    def handler(request):
        if "/threads/" in request.url.path:
            return json_response({"id": "t1", "messages": [raw_message(id="b", thread="t1", date_ms=2), raw_message(id="a", thread="t1", date_ms=1)]})
        return json_response(raw_message(id="m1"))

    client, _ = client_for(handler)
    assert client.get_message("m1").message_id == "m1"
    assert [m.message_id for m in client.get_thread("t1").messages] == ["a", "b"]


@pytest.mark.parametrize("bad", ["", "../../etc/passwd", "a/b", "m1?x=1", "m 1", "x" * 200, None, 5])
def test_ids_are_validated_before_they_reach_a_url(bad):
    called = []
    client, _ = client_for(lambda r: called.append(r) or json_response({}))
    with pytest.raises(GmailNotFound):
        client.get_message(bad)
    with pytest.raises(GmailNotFound):
        client.get_thread(bad)
    assert called == []
    assert validate_id("18c4f0a1b2c3d4e5") == "18c4f0a1b2c3d4e5"


def test_rate_limit_is_retried_with_backoff_then_reported():
    attempts = []

    def handler(request):
        attempts.append(1)
        return httpx.Response(429, headers={"Retry-After": "3"}, json={"error": {"code": 429}})

    client, sleeps = client_for(handler)
    with pytest.raises(GmailRateLimited):
        client.get_message("m1")
    assert len(attempts) == 4 and len(sleeps) == 3  # bounded: never an infinite loop
    assert sleeps[0] == 3.0 and sleeps == sorted(sleeps)  # honours Retry-After, then backs off


def test_rate_limit_403_is_treated_like_429_but_other_403_is_permission_denied():
    def limited(request):
        return httpx.Response(403, json={"error": {"errors": [{"reason": "rateLimitExceeded"}]}})

    with pytest.raises(GmailRateLimited):
        client_for(limited)[0].get_message("m1")

    calls = []

    def denied(request):
        calls.append(1)
        return httpx.Response(403, json={"error": {"errors": [{"reason": "insufficientPermissions"}]}})

    with pytest.raises(GmailPermissionDenied):
        client_for(denied)[0].get_message("m1")
    assert len(calls) == 1  # not retried


def test_transient_server_errors_recover():
    state = {"n": 0}

    def handler(request):
        state["n"] += 1
        return httpx.Response(503) if state["n"] < 3 else json_response(raw_message(id="m1"))

    client, sleeps = client_for(handler)
    assert client.get_message("m1").message_id == "m1" and len(sleeps) == 2


def test_persistent_server_errors_and_network_failures_are_unavailable():
    with pytest.raises(GmailUnavailable):
        client_for(lambda r: httpx.Response(500))[0].get_message("m1")

    def offline(request):
        raise httpx.ConnectError("no route to host")

    client, sleeps = client_for(offline)
    with pytest.raises(GmailUnavailable):
        client.get_message("m1")
    assert len(sleeps) == 3


def test_401_refreshes_the_token_once_then_fails():
    auth = StaticAuth()
    state = {"n": 0}

    def handler(request):
        state["n"] += 1
        return httpx.Response(401) if state["n"] == 1 else json_response(raw_message(id="m1"))

    client, _ = client_for(handler, auth)
    assert client.get_message("m1").message_id == "m1" and auth.invalidated == 1

    auth2 = StaticAuth()
    with pytest.raises(GmailAuthError):
        client_for(lambda r: httpx.Response(401), auth2)[0].get_message("m1")
    assert auth2.invalidated == 1  # exactly one refresh attempt


def test_not_found_and_malformed_responses():
    with pytest.raises(GmailNotFound):
        client_for(lambda r: httpx.Response(404))[0].get_message("m1")
    with pytest.raises(GmailResponseError):
        client_for(lambda r: httpx.Response(200, content=b"<html>not json</html>"))[0].get_message("m1")
    with pytest.raises(GmailResponseError):
        client_for(lambda r: json_response([1, 2, 3]))[0].get_message("m1")
    with pytest.raises(GmailResponseError):
        client_for(lambda r: json_response({"no": "id"}))[0].get_message("m1")
    with pytest.raises(Exception) as exc:
        client_for(lambda r: httpx.Response(418))[0].get_message("m1")
    assert "418" in str(exc.value)


def test_authentication_errors_propagate_without_any_request():
    called = []
    client, _ = client_for(lambda r: called.append(r), StaticAuth(fail=GmailNotConfigured("x")))
    with pytest.raises(GmailNotConfigured):
        client.search("", 5)
    assert called == []


def test_tokens_and_message_content_are_never_logged(caplog):
    def handler(request):
        return httpx.Response(503, text=f"secret body {SECRET_TOKEN}")

    client, _ = client_for(handler)
    with caplog.at_level(logging.DEBUG):
        with pytest.raises(GmailUnavailable):
            client.get_message("m1")
    assert SECRET_TOKEN not in caplog.text and "secret body" not in caplog.text


def test_client_id_and_secret_from_the_environment_configure_oauth(tmp_path, monkeypatch):
    import google_auth_oauthlib.flow as flow_module

    auth = GmailAuthenticator(tmp_path / "c.json", tmp_path / "token.json", "cid.apps.googleusercontent.com", CLIENT_SECRET)
    assert auth.status().configured and not auth.status().authorized
    assert not GmailAuthenticator(tmp_path / "c.json", tmp_path / "t.json", "cid", "").status().configured

    seen = {}

    class FakeCreds:
        def to_json(self):
            return json.dumps({"token": "t", "refresh_token": "r", "client_id": "cid", "client_secret": "s"})

    class FakeFlow:
        @classmethod
        def from_client_config(cls, config, scopes):
            seen["config"], seen["scopes"] = config, scopes
            return cls()

        def run_local_server(self, **kwargs):
            seen["kwargs"] = kwargs
            return FakeCreds()

    monkeypatch.setattr(flow_module, "InstalledAppFlow", FakeFlow)
    auth.authorize(open_browser=False)
    assert seen["scopes"] == [GMAIL_READONLY_SCOPE]  # read-only, nothing broader
    assert seen["config"]["installed"]["client_id"] == "cid.apps.googleusercontent.com"
    assert (tmp_path / "token.json").is_file() and auth.is_ready()


def test_client_secret_setting_is_not_exposed_by_repr():
    from backend.core.config import Settings

    s = Settings(_env_file=None, DATABASE_URL="postgresql+psycopg2://u:p@localhost/x", GMAIL_CLIENT_SECRET="GOCSPX-TOPSECRET")
    assert "TOPSECRET" not in repr(s) and "TOPSECRET" not in str(s)
