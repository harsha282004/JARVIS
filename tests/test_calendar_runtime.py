"""Google Calendar configuration, bootstrap wiring, the setup CLI and Git hygiene. No network, no real account."""

import importlib.util
import subprocess
from pathlib import Path

import pytest

from backend.core.config import Settings, get_settings
from backend.core.security import PermissionStatus
from integrations.base import Integration
from integrations.calendar.base import CalendarClient, CalendarIntegration
from integrations.calendar.intents import CALENDAR_ACTION_NAMES, parse_calendar_action
from voice.bootstrap import _build_conversation, build_calendar_tools_for, build_event_tools_for

ROOT = Path(__file__).resolve().parents[1]


def settings(tmp_path=None, **overrides) -> Settings:
    base = {
        "DATABASE_URL": "postgresql+psycopg2://jarvis:jarvis@localhost:5432/jarvis_test",
        "JARVIS_MEMORY_ENABLED": False, "JARVIS_RAG_ENABLED": False, "JARVIS_KG_ENABLED": False,
        "JARVIS_TASKS_ENABLED": False, "JARVIS_REMINDERS_ENABLED": False, "JARVIS_EVENTS_ENABLED": False,
        "JARVIS_GMAIL_ENABLED": False, "JARVIS_TIMEZONE": "Asia/Kolkata",
    }
    if tmp_path is not None:
        base["JARVIS_CALENDAR_CREDENTIALS_PATH"] = str(tmp_path / "credentials.json")
        base["JARVIS_CALENDAR_TOKEN_PATH"] = str(tmp_path / "token.json")
    base.update(overrides)
    return Settings(_env_file=None, **base)


def test_calendar_settings_have_safe_defaults_and_bounds():
    s = settings()
    assert s.JARVIS_CALENDAR_ENABLED is False and s.JARVIS_CALENDAR_MAX_RESULTS == 20
    assert s.JARVIS_CALENDAR_CREDENTIALS_PATH == ".jarvis/calendar/credentials.json" and s.JARVIS_CALENDAR_TOKEN_PATH == ".jarvis/calendar/token.json"
    for bad in (0, 101, -1):
        with pytest.raises(ValueError):
            settings(JARVIS_CALENDAR_MAX_RESULTS=bad)
    assert "hunter2-secret" not in repr(settings(CALENDAR_CLIENT_SECRET="hunter2-secret"))  # SecretStr never prints its value


def test_calendar_is_off_by_default():
    engine = _build_conversation(settings(), None)
    assert engine._actions is None
    assert engine._permissions.request_permission("calendar_events", "execute").status is PermissionStatus.DENIED
    assert build_calendar_tools_for(settings(), None) == []


def test_enabled_calendar_registers_the_documented_tools_and_nothing_else(tmp_path):
    engine = _build_conversation(settings(tmp_path, JARVIS_CALENDAR_ENABLED=True), None)
    perms = engine._permissions
    reads = {"calendar_list", "calendar_events", "calendar_search", "calendar_get_event"}
    for name in reads:
        assert perms.request_permission(name, "execute").status is PermissionStatus.APPROVED
    for name in CALENDAR_ACTION_NAMES - reads:
        assert perms.request_permission(name, "execute").status is PermissionStatus.PENDING  # the user must say yes
    for name in ("calendar_delete", "calendar_share", "calendar_send_invite", "calendar_acl", "gmail_send"):
        assert perms.request_permission(name, "execute").status is PermissionStatus.DENIED
    assert set(engine._actions._tools) == CALENDAR_ACTION_NAMES


def test_calendar_works_alongside_events_and_shares_one_event_service(tmp_path):
    cfg = settings(tmp_path, JARVIS_CALENDAR_ENABLED=True, JARVIS_EVENTS_ENABLED=True)
    engine = _build_conversation(cfg, None)
    tools = set(engine._actions._tools)
    assert CALENDAR_ACTION_NAMES <= tools and {"event_create", "event_cancel"} <= tools
    assert engine._permissions.request_permission("event_create", "execute").status is PermissionStatus.APPROVED
    assert engine._permissions.request_permission("calendar_create_event", "execute").status is PermissionStatus.PENDING
    assert build_event_tools_for(cfg, None) != []


def test_missing_credentials_give_a_clear_setup_message_and_touch_nothing(tmp_path):
    engine = _build_conversation(settings(tmp_path, JARVIS_CALENDAR_ENABLED=True), None)
    action = parse_calendar_action({"name": "calendar_events", "arguments": {"scope": "today"}})
    outcome = engine._actions.execute(action, "session-1")
    assert "Google Calendar isn't set up yet" in outcome.reply and "calendar_cli.py auth" in outcome.reply and not outcome.executed
    assert list(tmp_path.iterdir()) == []  # no file, token or request was created


def test_integration_registry_reports_whether_calendar_is_ready():
    assert isinstance(CalendarIntegration(lambda: False), Integration) and CalendarIntegration(lambda: False).is_configured() is False
    assert CalendarIntegration(lambda: True).is_configured() is True
    assert {"list_calendars", "list_events", "get_event", "create_event", "update_event", "delete_event"} <= set(CalendarClient.__abstractmethods__)


def load_cli():
    spec = importlib.util.spec_from_file_location("calendar_cli", ROOT / "scripts" / "calendar_cli.py")
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)
    return cli


def test_cli_status_reports_missing_files_without_secrets(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("JARVIS_CALENDAR_CREDENTIALS_PATH", str(tmp_path / "credentials.json"))
    monkeypatch.setenv("JARVIS_CALENDAR_TOKEN_PATH", str(tmp_path / "token.json"))
    monkeypatch.setenv("CALENDAR_CLIENT_ID", "")
    monkeypatch.setenv("CALENDAR_CLIENT_SECRET", "")
    monkeypatch.setenv("GMAIL_CLIENT_ID", "")
    monkeypatch.setenv("GMAIL_CLIENT_SECRET", "")
    get_settings.cache_clear()
    try:
        cli = load_cli()
        assert cli.main(["status"]) == 1
        out = capsys.readouterr().out
        assert "MISSING" in out and "calendar.events" in out and "not created yet" in out
        (tmp_path / "credentials.json").write_text('{"installed": {"client_secret": "GOCSPX-TOPSECRET"}}')
        (tmp_path / "token.json").write_text('{"refresh_token": "1//TOPSECRET"}')
        assert cli.main(["status"]) == 0
        out = capsys.readouterr().out
        assert "TOPSECRET" not in out and "found" in out  # file contents are never printed
    finally:
        get_settings.cache_clear()


def test_cli_only_reads_it_has_no_mutating_command():
    text = (ROOT / "scripts" / "calendar_cli.py").read_text(encoding="utf-8")
    assert "create_event" not in text and "update_event" not in text and "delete_event" not in text
    assert "print(auth" not in text and "access_token" not in text and "refresh_token" not in text


def test_the_gmail_token_and_calendar_token_are_separate_and_gmail_stays_read_only():
    from integrations.gmail.auth import SCOPES as GMAIL_SCOPES
    from integrations.calendar.auth import SCOPES

    assert GMAIL_SCOPES == ("https://www.googleapis.com/auth/gmail.readonly",)
    assert set(SCOPES) == {"https://www.googleapis.com/auth/calendar.events", "https://www.googleapis.com/auth/calendar.calendarlist.readonly"}
    assert settings().JARVIS_CALENDAR_TOKEN_PATH != settings().JARVIS_GMAIL_TOKEN_PATH


def git(*args) -> str:
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, check=False).stdout


@pytest.mark.skipif(not (ROOT / ".git").exists(), reason="not a git checkout")
def test_credentials_and_tokens_are_git_ignored():
    for path in (".jarvis/calendar/credentials.json", ".jarvis/calendar/token.json", ".jarvis/gmail/token.json", "credentials.json",
                 "calendar_token.json", "token.json", ".env"):
        assert subprocess.run(["git", "check-ignore", "-q", path], cwd=ROOT).returncode == 0, path
    assert ".env.example" not in git("check-ignore", ".env.example")


def test_no_secret_looking_values_are_committed_in_the_calendar_files():
    import re

    pattern = re.compile(r"GOCSPX-|ya29\.|1//0|AIza[0-9A-Za-z_-]{20}|\d{6,}-[a-z0-9]{20,}\.apps\.googleusercontent\.com")
    files = [*(ROOT / "integrations" / "calendar").glob("*.py"), ROOT / "integrations" / "google_oauth.py", ROOT / ".env.example",
             ROOT / "scripts" / "calendar_cli.py", ROOT / "docs" / "google-calendar-integration.md"]
    hits = [f.name for f in files if f.exists() and pattern.search(f.read_text(encoding="utf-8"))]
    assert hits == []


def test_no_migration_or_table_was_added_for_calendar():
    versions = ROOT / "backend" / "migrations" / "versions"
    if not versions.exists():
        versions = next(ROOT.glob("**/alembic/versions"), versions)
    text = "".join(p.read_text(encoding="utf-8", errors="ignore") for p in versions.glob("*.py")) if versions.exists() else ""
    assert "calendar" not in text.lower()
