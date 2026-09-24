"""Gmail configuration, bootstrap wiring and the setup CLI. No network, no real account."""

import pytest

from agent.tasks.executor import TaskActionExecutor  # noqa: F401  (import check: same executor serves Gmail)
from backend.core.config import Settings, get_settings
from backend.core.security import PermissionStatus
from integrations.base import Integration
from integrations.gmail.base import GmailClient, GmailIntegration
from integrations.gmail.intents import GMAIL_ACTION_NAMES, parse_gmail_action
from voice.bootstrap import PROJECT_ROOT, _build_conversation, _project_path, build_gmail_tools_for, build_task_system


def settings(tmp_path=None, **overrides) -> Settings:
    base = {
        "DATABASE_URL": "postgresql+psycopg2://jarvis:jarvis@localhost:5432/jarvis_test",
        "JARVIS_MEMORY_ENABLED": False, "JARVIS_RAG_ENABLED": False, "JARVIS_KG_ENABLED": False,
        "JARVIS_TASKS_ENABLED": False, "JARVIS_REMINDERS_ENABLED": False, "JARVIS_EVENTS_ENABLED": False, "JARVIS_BRIEFING_ENABLED": False,
        "JARVIS_TIMEZONE": "Asia/Kolkata",
    }
    if tmp_path is not None:
        base["JARVIS_GMAIL_CREDENTIALS_PATH"] = str(tmp_path / "credentials.json")
        base["JARVIS_GMAIL_TOKEN_PATH"] = str(tmp_path / "token.json")
    base.update(overrides)
    return Settings(_env_file=None, **base)


def test_gmail_settings_have_safe_defaults_and_bounds():
    s = settings()
    assert s.JARVIS_GMAIL_ENABLED is False  # off until the user sets it up
    assert s.JARVIS_GMAIL_MAX_RESULTS == 10
    assert s.JARVIS_GMAIL_CREDENTIALS_PATH == ".jarvis/gmail/credentials.json" and s.JARVIS_GMAIL_TOKEN_PATH == ".jarvis/gmail/token.json"
    for bad in (0, 51, -1):
        with pytest.raises(ValueError):
            settings(JARVIS_GMAIL_MAX_RESULTS=bad)


def test_relative_paths_resolve_inside_the_project_and_absolute_ones_are_kept(tmp_path):
    assert _project_path(".jarvis/gmail/token.json") == PROJECT_ROOT / ".jarvis" / "gmail" / "token.json"
    assert _project_path(str(tmp_path / "t.json")) == tmp_path / "t.json"


def test_gmail_is_off_by_default_and_nothing_changes_from_phase_9():
    engine = _build_conversation(settings(), None)
    assert engine._actions is None
    assert engine._permissions.request_permission("gmail_search", "execute").status is PermissionStatus.DENIED
    assert build_gmail_tools_for(settings(), object(), None) == []


def test_enabled_gmail_registers_only_the_read_only_tools(tmp_path):
    cfg = settings(tmp_path, JARVIS_GMAIL_ENABLED=True)
    engine = _build_conversation(cfg, None)
    assert engine._actions is not None
    perms = engine._permissions
    for name in GMAIL_ACTION_NAMES:
        assert perms.request_permission(name, "execute").status is PermissionStatus.APPROVED  # registered, LOW risk
    for name in ("gmail_send", "gmail_delete", "gmail_modify", "gmail_reply", "send_email", "gmail_label"):
        assert perms.request_permission(name, "execute").status is PermissionStatus.DENIED


def test_gmail_tools_work_alongside_task_tools(tmp_path):
    cfg = settings(tmp_path, JARVIS_GMAIL_ENABLED=True, JARVIS_TASKS_ENABLED=True, JARVIS_REMINDERS_ENABLED=True)
    engine = _build_conversation(cfg, build_task_system(cfg))
    tools = set(engine._actions._tools)
    assert GMAIL_ACTION_NAMES <= tools and {"create_task", "create_reminder", "cancel_reminder"} <= tools


def test_missing_credentials_give_a_clear_setup_message_and_touch_nothing(tmp_path):
    cfg = settings(tmp_path, JARVIS_GMAIL_ENABLED=True)
    engine = _build_conversation(cfg, None)
    action = parse_gmail_action({"name": "gmail_search", "arguments": {"query": "is:unread"}})
    outcome = engine._actions.execute(action, "session-1")
    assert "Gmail isn't set up yet" in outcome.reply and "gmail_cli.py auth" in outcome.reply and not outcome.executed
    assert list(tmp_path.iterdir()) == []  # no file was created


def test_integration_registry_reports_whether_gmail_is_ready():
    assert isinstance(GmailIntegration(lambda: False), Integration) and GmailIntegration(lambda: False).is_configured() is False
    assert GmailIntegration(lambda: True).is_configured() is True
    assert {"search", "get_message", "get_thread"} <= set(GmailClient.__abstractmethods__)


def test_cli_status_reports_missing_files_without_secrets(tmp_path, monkeypatch, capsys):
    import importlib.util
    from pathlib import Path

    monkeypatch.setenv("JARVIS_GMAIL_CREDENTIALS_PATH", str(tmp_path / "credentials.json"))
    monkeypatch.setenv("JARVIS_GMAIL_TOKEN_PATH", str(tmp_path / "token.json"))
    get_settings.cache_clear()
    try:
        spec = importlib.util.spec_from_file_location("gmail_cli", Path(__file__).resolve().parents[1] / "scripts" / "gmail_cli.py")
        cli = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cli)
        assert cli.main(["status"]) == 1
        out = capsys.readouterr().out
        assert "MISSING" in out and "gmail.readonly" in out and "not created yet" in out

        (tmp_path / "credentials.json").write_text('{"installed": {"client_secret": "GOCSPX-TOPSECRET"}}')
        (tmp_path / "token.json").write_text('{"refresh_token": "1//TOPSECRET"}')
        assert cli.main(["status"]) == 0
        out = capsys.readouterr().out
        assert "TOPSECRET" not in out and "found" in out  # file contents are never printed
    finally:
        get_settings.cache_clear()


def test_event_settings_defaults_bounds_and_bootstrap(tmp_path):
    from backend.core.config import Settings
    from backend.core.security import PermissionStatus
    from voice.bootstrap import _build_conversation, build_event_tools_for

    base = {"DATABASE_URL": "postgresql+psycopg2://u:p@localhost/x", "JARVIS_MEMORY_ENABLED": False, "JARVIS_RAG_ENABLED": False,
            "JARVIS_KG_ENABLED": False, "JARVIS_TASKS_ENABLED": False, "JARVIS_REMINDERS_ENABLED": False, "JARVIS_TIMEZONE": "Asia/Kolkata", "JARVIS_BRIEFING_ENABLED": False}
    s = Settings(_env_file=None, **base)
    assert (s.JARVIS_EVENTS_ENABLED, s.JARVIS_EVENT_DEFAULT_LOOKAHEAD_DAYS, s.JARVIS_EVENT_MAX_RESULTS) == (True, 7, 20)
    for bad in ({"JARVIS_EVENT_DEFAULT_LOOKAHEAD_DAYS": 0}, {"JARVIS_EVENT_DEFAULT_LOOKAHEAD_DAYS": 366}, {"JARVIS_EVENT_MAX_RESULTS": 0}, {"JARVIS_EVENT_MAX_RESULTS": 101}):
        with pytest.raises(ValueError):
            Settings(_env_file=None, **base, **bad)

    off = Settings(_env_file=None, **base, JARVIS_EVENTS_ENABLED=False)
    assert build_event_tools_for(off, None) == [] and _build_conversation(off, None)._actions is None

    engine = _build_conversation(s, None)  # Gmail disabled: no event_extract source except none, so seven tools
    perms = engine._permissions
    assert perms.request_permission("event_create", "execute").status is PermissionStatus.APPROVED
    assert perms.request_permission("event_cancel", "execute").status is PermissionStatus.PENDING
    assert perms.request_permission("event_update", "execute").status is PermissionStatus.PENDING
    for unknown in ("calendar_create", "event_delete", "gmail_send"):
        assert perms.request_permission(unknown, "execute").status is PermissionStatus.DENIED
