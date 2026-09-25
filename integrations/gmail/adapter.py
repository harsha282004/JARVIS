"""GmailAdapter: the hub's view of Gmail. Wraps the existing GmailService/authenticator (nothing is re-implemented).

* search / fetch  -> live, bounded, normalized EMAIL items with topic and importance.
* sync            -> incremental: the cursor is the newest message time already seen, sent as Gmail's `after:` filter, so each run reads only new mail. If a run
                     is cut off by the page bound the cursor keeps the query and the page token so the next run continues where it stopped. Re-reading a message
                     is harmless: items are upserted by message id (idempotent).
* derived items   -> EVENT / DEADLINE items extracted from the email's text (with the evidence sentence and confidence), a registration EVENT for
                     "your registration for X is confirmed", all carrying the message id as provenance.
* attachments     -> metadata is part of the EMAIL item. Downloading is separate, opt-in (READ_ATTACHMENT) and size-capped; nothing is fetched automatically.
Email text is untrusted data: it is pattern-matched, sanitized, size-bounded and scanned for injection; nothing in it is ever executed or followed.
"""

import json
import re
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path

from agent.intelligence.extraction import CommitmentKind, TextExtractor
from backend.core.security.trust import sanitize_external
from integrations.gmail.analysis import EmailAnalysis, analyze_email
from integrations.gmail.models import GmailMessage
from integrations.gmail.service import GmailService
from integrations.hub.models import ErrorKind, HubError, ItemKind, NormalizedItem, Permission, utcnow
from integrations.hub.registry import IntegrationAdapter, SyncBatch

MAX_PAGES = 5
MAX_ATTACHMENT_BYTES = 10 * 1024 * 1024
SAFE_ATTACHMENT_SUFFIXES = frozenset({".pdf", ".docx", ".txt", ".md", ".markdown"})


def _safe_name(name: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._\- ]", "_", Path(name).name).strip(" .") or "attachment"
    return cleaned[:120]


def normalize_message(message: GmailMessage, analysis: EmailAnalysis, retrieved_at: datetime, *, derived: bool = True) -> list[NormalizedItem]:
    sender = (message.sender.name if message.sender and message.sender.name and "@" not in message.sender.name else "a sender")
    subject = sanitize_external(message.subject, 200) or "(no subject)"
    email = NormalizedItem(
        ItemKind.EMAIL, "gmail", message.message_id, message.timestamp, subject, sanitize_external(message.snippet, 240),
        {
            "thread_id": message.thread_id, "sender": sanitize_external(sender, 60), "topic": analysis.topic.value, "importance": analysis.importance.name,
            "importance_reasons": analysis.reasons[:5], "category": analysis.category.value, "unread": message.is_unread,
            "attachments": [{"filename": sanitize_external(a.filename, 120), "mime": a.mime_type, "size": a.size, "attachment_id": a.attachment_id} for a in message.attachments[:20]],
            "injection_suspected": analysis.flagged, "location": analysis.location,
        },
        "high", message.message_id, retrieved_at,
    )
    items = [email]
    if not derived:
        return items
    confidence_names = {1: "low", 2: "medium", 3: "high"}
    for n, c in enumerate(analysis.commitments):
        if c.when is None:
            continue
        kind = ItemKind.EVENT if c.kind is CommitmentKind.EVENT else ItemKind.DEADLINE
        meta = {"message_id": message.message_id, "evidence": c.evidence, "deadline_kind": c.deadline_kind.value if c.deadline_kind else None,
                "event_type": c.event_type.value if c.event_type else None, "all_day": c.all_day, "project_hint": c.project_hint, "status": "pending",
                "is_task": c.kind is CommitmentKind.TASK, "injection_suspected": c.flagged, "topic": analysis.topic.value, "location": analysis.location}
        items.append(NormalizedItem(kind, "gmail", f"{message.message_id}#{kind.value}{n}", c.when, c.title, c.evidence[:240], meta,
                                    confidence_names[int(c.confidence)], message.message_id, retrieved_at))
    if analysis.registration is not None:
        r = analysis.registration
        items.append(NormalizedItem(ItemKind.EVENT, "gmail", f"{message.message_id}#registration", message.timestamp, r.event_name, r.evidence[:240],
                                    {"message_id": message.message_id, "registration": r.state, "topic": analysis.topic.value, "location": analysis.location, "status": "registered" if r.state == "completed" else "mentioned"},
                                    "high" if r.state == "completed" else "low", message.message_id, retrieved_at))
    return items


class GmailAdapter(IntegrationAdapter):
    name = "gmail"
    display_name = "Gmail"
    permissions = frozenset({Permission.READ_EMAIL, Permission.SEARCH_EMAIL, Permission.READ_ATTACHMENT})
    sync_interval_seconds = 600.0
    manual_connect = True  # Google consent happens in the browser

    def __init__(self, service: GmailService, authenticator, zone, clock: Callable[[], datetime] = utcnow, *, initial_days: int = 14,
                 attachments_dir: Path | None = None, max_attachment_bytes: int = MAX_ATTACHMENT_BYTES):
        self._svc, self._auth, self._zone, self._clock = service, authenticator, zone, clock
        self._initial_days = initial_days
        self._attachments_dir = attachments_dir
        self._max_attachment = max_attachment_bytes

    def _extractor(self) -> TextExtractor:
        return TextExtractor(self._zone, self._clock)

    # ---- state -------------------------------------------------------------------------------------------------------------
    def is_configured(self) -> bool:
        return self._svc.is_configured()

    def is_authenticated(self) -> bool:
        return self._auth.is_ready()

    # ---- operations --------------------------------------------------------------------------------------------------------
    def authenticate(self) -> None:
        self._auth.authorize()

    def health_check(self) -> str:
        self._svc.search("in:inbox", 1)
        return "Gmail reachable"

    def disconnect(self) -> None:
        self._auth.forget()

    def revoke(self) -> bool:
        return self._auth.revoke_remote()

    def _normalize(self, message: GmailMessage, derived: bool) -> list[NormalizedItem]:
        now = self._clock()
        return normalize_message(message, analyze_email(message, self._extractor(), now), now, derived=derived)

    def search(self, query: str, limit: int) -> list[NormalizedItem]:
        result = self._svc.search(query, limit)
        return [self._normalize(m, derived=False)[0] for m in result.messages]

    def fetch(self, source_id: str) -> NormalizedItem:
        return self._normalize(self._svc.get_message(source_id), derived=True)[0]

    def analysis(self, source_id: str) -> tuple[GmailMessage, EmailAnalysis]:
        message = self._svc.get_message(source_id)
        return message, analyze_email(message, self._extractor(), self._clock())

    def sync(self, cursor: str | None, limit: int) -> SyncBatch:
        state = json.loads(cursor) if cursor else {}
        after = state.get("after")
        query = state.get("query") or (f"in:inbox after:{max(0, int(after) - 60)}" if after else f"in:inbox newer_than:{self._initial_days}d")
        page = state.get("page")
        messages: list[GmailMessage] = []
        next_token = page
        for _ in range(MAX_PAGES):
            result = self._svc.search_for_sync(query, limit, next_token)
            messages.extend(result.messages)
            next_token = result.next_page_token
            if not next_token:
                break
        items: list[NormalizedItem] = []
        newest = int(after) if after else 0
        for m in messages:
            items.extend(self._normalize(m, derived=True))
            if m.timestamp is not None:
                newest = max(newest, int(m.timestamp.timestamp()))
        if next_token:  # cut off by the page bound: continue the same query next time
            new_state = {"after": after, "query": query, "page": next_token}
            detail = "more mail remains; continuing next sync"
        else:
            new_state, detail = {"after": newest or int(self._clock().timestamp())}, ""
        return SyncBatch(items, json.dumps(new_state), [], detail)

    # ---- attachments (opt-in) ----------------------------------------------------------------------------------------------
    def download_attachment(self, message_id: str, attachment_id: str, filename: str) -> Path:
        """Save one attachment under the JARVIS attachments folder. Only called after the user asked for it (and READ_ATTACHMENT is granted)."""
        if self._attachments_dir is None:
            raise HubError(ErrorKind.CONFIGURATION_ERROR, "No folder is configured for email attachments.")
        safe = _safe_name(filename)
        if Path(safe).suffix.lower() not in SAFE_ATTACHMENT_SUFFIXES:
            raise HubError(ErrorKind.INVALID_REQUEST, "I can only index PDF, Word, text and Markdown attachments.")
        data = self._svc.get_attachment(message_id, attachment_id, self._max_attachment)
        target = self._attachments_dir / re.sub(r"[^A-Za-z0-9_\-]", "_", message_id)[:64] / safe
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        return target
