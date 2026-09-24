"""Messaging configuration, bootstrap wiring, the setup CLI and Git hygiene. No network, no real account."""

import importlib.util
import subprocess
from pathlib import Path

import pytest

from backend.core.config import Settings, get_settings
from backend.core.security import PermissionStatus
from integrations.messaging.intents import MESSAGE_ACTION_NAMES, parse_message_action
from tests.messaging_helpers import TOKEN
from voice.bootstrap import _build_conversation, build_messaging_registry, build_messaging_tools_for

ROOT = Path(__file__).resolve().parents[1]


def settings(tmp_path=None, **overrides) -> Settings:
    base = {
        "DATABASE_URL": "postgresql+psycopg2://jarvis:jarvis@localhost:5432/jarvis_test",
        "JARVIS_MEMORY_ENABLED": False, "JARVIS_RAG_ENABLED": False, "JARVIS_KG_ENABLED": False,
        "JARVIS_TASKS_ENABLED": False, "JARVIS_REMINDERS_ENABLED": False, "JARVIS_EVENTS_ENABLED": False, "JARVIS_BRIEFING_ENABLED": False,
        "JARVIS_GMAIL_ENABLED": False, "JARVIS_CALENDAR_ENABLED": False, "JARVIS_TIMEZONE": "Asia/Kolkata",
        "MESSAGING_TELEGRAM_BOT_TOKEN": "",
    }
    if tmp_path is not None:
        base["JARVIS_MESSAGING_TELEGRAM_TOKEN_PATH"] = str(tmp_path / "telegram_token")
    base.update(overrides)
    return Settings(_env_file=None, **base)


def test_messaging_settings_have_safe_defaults_and_bounds():
    s = settings()
    assert s.JARVIS_MESSAGING_ENABLED is False and s.JARVIS_MESSAGING_MAX_RESULTS == 20
    assert s.JARVIS_MESSAGING_TELEGRAM_TOKEN_PATH == ".jarvis/messaging/telegram_token"
    for bad in (0, 101, -1):
        with pytest.raises(ValueError):
            settings(JARVIS_MESSAGING_MAX_RESULTS=bad)
    assert "hunter2-secret" not in repr(settings(MESSAGING_TELEGRAM_BOT_TOKEN="hunter2-secret"))  # SecretStr never prints its value


def test_messaging_is_off_by_default_and_nothing_changes_from_phase_12():
    engine = _build_conversation(settings(), None)
    assert engine._actions is None
    assert engine._permissions.request_permission("message_list", "execute").status is PermissionStatus.DENIED
    assert build_messaging_tools_for(settings(), None, None) == []


def test_enabled_messaging_registers_only_the_documented_read_only_tools(tmp_path):
    engine = _build_conversation(settings(tmp_path, JARVIS_MESSAGING_ENABLED=True), None)
    perms = engine._permissions
    for name in MESSAGE_ACTION_NAMES:
        assert perms.request_permission(name, "execute").status is PermissionStatus.APPROVED  # registered, LOW risk
    for name in ("message_send", "message_delete", "message_reply", "telegram_send", "whatsapp_send", "message_forward", "message_edit"):
        assert perms.request_permission(name, "execute").status is PermissionStatus.DENIED
    assert set(engine._actions._tools) == MESSAGE_ACTION_NAMES


def test_messaging_works_alongside_gmail_calendar_and_events(tmp_path):
    from integrations.calendar.intents import CALENDAR_ACTION_NAMES
    from integrations.gmail.intents import GMAIL_ACTION_NAMES

    cfg = settings(tmp_path, JARVIS_MESSAGING_ENABLED=True, JARVIS_GMAIL_ENABLED=True, JARVIS_CALENDAR_ENABLED=True, JARVIS_EVENTS_ENABLED=True)
    tools = set(_build_conversation(cfg, None)._actions._tools)
    assert MESSAGE_ACTION_NAMES <= tools and GMAIL_ACTION_NAMES <= tools and CALENDAR_ACTION_NAMES <= tools and {"event_create"} <= tools
    assert not (MESSAGE_ACTION_NAMES & (GMAIL_ACTION_NAMES | CALENDAR_ACTION_NAMES))  # provider identity stays explicit; no name clashes


def test_the_only_registered_provider_is_the_official_telegram_bot_api():
    registry = build_messaging_registry(settings())
    assert registry.names() == ["telegram"] and registry.get("whatsapp") is None
    assert registry.get("telegram").display_name == "Telegram"


def test_missing_token_gives_a_clear_setup_message_and_touches_nothing(tmp_path):
    engine = _build_conversation(settings(tmp_path, JARVIS_MESSAGING_ENABLED=True), None)
    outcome = engine._actions.execute(parse_message_action({"name": "message_list", "arguments": {}}), "session-1")
    assert "Messaging isn't set up yet" in outcome.reply and "docs/messaging-integration.md" in outcome.reply and not outcome.executed
    assert list(tmp_path.iterdir()) == []  # no file was created and no request could have been made (no token)


def test_token_comes_from_the_environment_setting_or_the_git_ignored_file(tmp_path):
    assert build_messaging_registry(settings(tmp_path)).get("telegram").is_configured() is False
    assert build_messaging_registry(settings(tmp_path, MESSAGING_TELEGRAM_BOT_TOKEN=TOKEN)).get("telegram").is_configured() is True
    (tmp_path / "telegram_token").write_text(f"{TOKEN}\n", encoding="utf-8")
    assert build_messaging_registry(settings(tmp_path)).get("telegram").is_configured() is True
    (tmp_path / "telegram_token").write_text("garbage", encoding="utf-8")
    assert build_messaging_registry(settings(tmp_path)).get("telegram").is_configured() is False


def load_cli():
    spec = importlib.util.spec_from_file_location("messaging_cli", ROOT / "scripts" / "messaging_cli.py")
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)
    return cli


def test_cli_status_reports_missing_and_found_without_secrets(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("JARVIS_MESSAGING_TELEGRAM_TOKEN_PATH", str(tmp_path / "telegram_token"))
    monkeypatch.setenv("MESSAGING_TELEGRAM_BOT_TOKEN", "")
    get_settings.cache_clear()
    try:
        cli = load_cli()
        assert cli.main(["status"]) == 1
        out = capsys.readouterr().out
        assert "MISSING" in out and "capabilities: conversations, messages" in out and "search" not in out.split("capabilities:")[1].split("\n")[0]
        (tmp_path / "telegram_token").write_text(TOKEN, encoding="utf-8")
        assert cli.main(["status"]) == 0
        out = capsys.readouterr().out
        assert TOKEN not in out and TOKEN.split(":")[1] not in out and "found" in out  # the token is never printed
    finally:
        get_settings.cache_clear()


def test_cli_only_reads_it_has_no_mutating_command():
    text = (ROOT / "scripts" / "messaging_cli.py").read_text(encoding="utf-8")
    code = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith(("#", "This script", "    python")))
    for word in ("sendMessage", ".send", "delete_", "edit_", "forward", "offset", "webhook", "getFile"):
        assert word not in code, word
    assert "print(token" not in text and "get_secret_value" not in text and "_call(" not in text


def git(*args) -> str:
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, check=False).stdout


@pytest.mark.skipif(not (ROOT / ".git").exists(), reason="not a git checkout")
def test_bot_tokens_are_git_ignored():
    for path in (".jarvis/messaging/telegram_token", "telegram_token", "telegram_token.txt", ".env", ".env.local"):
        assert subprocess.run(["git", "check-ignore", "-q", path], cwd=ROOT).returncode == 0, path
    assert ".env.example" not in git("check-ignore", ".env.example")


def test_no_token_looking_values_are_committed_in_the_messaging_files():
    import re

    pattern = re.compile(r"\b\d{6,12}:[A-Za-z0-9_-]{30,}\b")
    files = [*(ROOT / "integrations" / "messaging").glob("*.py"), ROOT / ".env.example", ROOT / "scripts" / "messaging_cli.py", ROOT / "docs" / "messaging-integration.md"]
    assert [f.name for f in files if f.exists() and pattern.search(f.read_text(encoding="utf-8"))] == []


def test_no_migration_or_table_was_added_for_messaging():
    versions = ROOT / "database" / "versions"
    text = "".join(p.read_text(encoding="utf-8", errors="ignore") for p in versions.glob("*.py")) if versions.exists() else ""
    assert "messag" not in text.lower() and "telegram" not in text.lower()
