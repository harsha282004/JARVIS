"""REAL Gmail tests: strictly read-only, a handful of requests, against the account you authorized.

They run only when a token exists at JARVIS_GMAIL_TOKEN_PATH (create it with `python scripts/gmail_cli.py auth`)
and otherwise SKIP; they never fake a pass. Nothing is sent, deleted, labelled, archived or modified, no
attachment is downloaded, and nothing from your mailbox is printed or logged by these tests. The summary test
also needs a reachable local Ollama.
"""

import pytest

from backend.core.config import get_settings

pytestmark = pytest.mark.integration


def _real_client():
    from integrations.gmail.client import HttpGmailClient
    from voice.bootstrap import build_gmail_authenticator

    auth = build_gmail_authenticator(get_settings())
    if not auth.is_ready():
        pytest.skip("No Gmail token (run: python scripts/gmail_cli.py auth); real Gmail was NOT tested")
    return HttpGmailClient(auth), auth


def _first_message(client):
    result = client.search("", 1)
    if not result.messages:
        pytest.skip("The mailbox has no messages to read")
    return result.messages[0]


def test_real_authentication_and_token_are_valid():
    _, auth = _real_client()
    assert auth.access_token()  # refreshes if needed; the token itself is never printed


def test_real_search_is_bounded_and_read_only():
    client, _ = _real_client()
    result = client.search("newer_than:30d", 3)
    assert result.count <= 3
    for message in result.messages:
        assert message.message_id and message.thread_id


def test_real_unread_search_and_message_and_thread_retrieval():
    client, _ = _real_client()
    client.search("is:unread", 2)  # must not raise
    message = _first_message(client)
    same = client.get_message(message.message_id)
    assert same.message_id == message.message_id and isinstance(same.plain_text_body, str)
    thread = client.get_thread(message.thread_id)
    assert thread.messages and message.message_id in {m.message_id for m in thread.messages}
    times = [m.timestamp for m in thread.messages if m.timestamp]
    assert times == sorted(times)  # chronological


def test_real_classification_and_local_summary_of_one_message():
    import httpx

    from integrations.gmail.intelligence import classify

    client, _ = _real_client()
    message = _first_message(client)
    assert classify(message).category  # deterministic, no LLM

    settings = get_settings()
    try:
        httpx.get(f"{settings.OLLAMA_BASE_URL.rstrip('/')}/api/tags", timeout=2.0)
    except httpx.HTTPError:
        pytest.skip("Ollama not reachable: the real summary was NOT tested")

    from backend.core.llm.ollama_provider import OllamaProvider
    from integrations.gmail.service import GmailService

    service = GmailService(client, OllamaProvider(base_url=settings.OLLAMA_BASE_URL, model=settings.LLM_MODEL))
    assert service.summarize_message(message).strip()
