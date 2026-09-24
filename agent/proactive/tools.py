"""The proactive tool: `proactive_explain` answers "why did you notify me?" from JARVIS's own notification history.

Read-only, LOW risk, no approval, ONE_TIME scope: it only reads the local history table, changes nothing and reaches no
external system. Same two-part shape as the other tools (resolve -> Ready; run only through Tool.execute after the
PermissionManager). The reply may quote an email subject or calendar title, so the conversation history keeps only a
placeholder (see `history_placeholder`) and the model never reads it. It reports the stored, factual reason and source;
it does not expose any internal reasoning.
"""

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from agent.proactive.intents import ProactiveActionName, ProactiveExplainArgs
from agent.proactive.messages import safe_text
from agent.proactive.models import HistoryRecord
from agent.proactive.repository import NotificationRepository
from agent.tasks.formatting import format_when
from agent.tasks.tools import Ready
from agent.tools.base import Tool
from backend.core.security import PermissionScope, RiskLevel

PLACEHOLDER = "[Notification history was read to the user. Its text is deliberately not kept in the conversation history.]"
SCAN = 50
_WORD = re.compile(r"[a-z0-9]+")
_FILLER = frozenset({"the", "a", "an", "that", "this", "about", "notification", "notify", "notified", "why", "did", "you", "me", "of", "for", "my", "it"})

_SOURCE_LABEL = {"task": "task", "event": "event or deadline record", "calendar": "Google Calendar event", "gmail": "Gmail message"}


@dataclass(frozen=True)
class ProactiveToolContext:
    repository: NotificationRepository
    zone: ZoneInfo
    clock: Any  # Callable[[], datetime]


def _matches(record: HistoryRecord, words: list[str]) -> bool:
    have = _WORD.findall(f"{record.message} {record.source_reference}".lower())
    return all(any(h == w or (len(w) >= 4 and h.startswith(w)) for h in have) for w in words)


class ProactiveExplainTool(Tool):
    name = ProactiveActionName.EXPLAIN.value
    description = (
        "Explain why JARVIS recently notified the user on its own: the stored factual reason and where it came from "
        "(source). Read-only. Use for 'why did you notify me?'."
    )
    input_schema = {"query": "string: words from the notification the user means, optional", "limit": "integer 1-5, optional (default 1)"}
    requires_permission = False
    risk = RiskLevel.LOW
    allowed_scopes = (PermissionScope.ONE_TIME,)
    history_placeholder = PLACEHOLDER

    def __init__(self, context: ProactiveToolContext):
        self._ctx = context

    def resolve(self, args: ProactiveExplainArgs) -> Ready:
        return Ready({"query": args.query or "", "limit": args.limit or 1})

    def run(self, *, query: str, limit: int, origin_session: str | None = None) -> str:
        ctx = self._ctx
        now: datetime = ctx.clock()
        words = [w for w in _WORD.findall(query.lower()) if w not in _FILLER]
        found = [r for r in ctx.repository.recent(SCAN) if _matches(r, words)][: max(1, min(limit, 5))]
        if not found:
            return "I haven't sent you any matching proactive notifications." if words else "I haven't sent you any proactive notifications yet."
        parts = []
        for record in found:
            when = format_when(record.delivered_at or record.created_at, now, ctx.zone)
            source = _SOURCE_LABEL.get(record.source_type.value, record.source_type.value)
            ref = f" (reference: {safe_text(record.source_id, 40)})" if record.source_id else ""
            parts.append(
                f"I told you '{safe_text(record.message, 200)}' {when} because {safe_text(record.reason, 250)}. "
                f"The source is {safe_text(record.source_reference, 60) or 'unknown'}: a {source}{ref}."
            )
        return " ".join(parts)


def build_proactive_tools(context: ProactiveToolContext) -> list[ProactiveExplainTool]:
    return [ProactiveExplainTool(context)]
