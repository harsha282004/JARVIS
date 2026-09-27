"""Gmail tools. Read-only: search, read, summarize, classify. There is no send, delete, label or archive tool.

Same two-part shape as the task tools (agent/tasks/tools.py):
  resolve(args) -> Ready   pure: validates the arguments; NO Gmail call happens here.
  run(**params) -> str     the only place Gmail is called, reached only through Tool.execute, i.e. after the
                           PermissionManager authorized exactly these parameters.

The model never supplies a message id. run() searches Gmail, identifies the email by code, and asks the user
when more than one matches. Replies are plain text for the user; they are not fed back to the model as
instructions, and the conversation history keeps only a placeholder (see `history_placeholder`).

Permission policy: LOW risk, no approval, ONE_TIME scope. Rationale: strictly read-only access to the user's
own mailbox, requested by the user's own words, bounded in size, with no way to change or send anything.
Anything else stays denied (unknown tool).
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from pydantic import BaseModel

from agent.tasks.formatting import format_when
from agent.tasks.tools import Ready
from agent.tools.base import Tool
from backend.core.security import PermissionScope, RiskLevel
from integrations.gmail.intelligence import SummaryFocus, find_action_requests
from integrations.gmail.intents import (
    GmailActionName,
    GmailClassifyArgs,
    GmailGetMessageArgs,
    GmailGetThreadArgs,
    GmailSearchArgs,
    GmailSummarizeArgs,
)
from integrations.gmail.models import GmailMessage
from integrations.gmail.service import GmailService
from integrations.gmail.text import one_line, sanitize_for_prompt, strip_quoted_reply

MAX_SPOKEN_ITEMS = 5
READ_ALOUD_CHARS = 500
PLACEHOLDER = "[Gmail results were read to the user. Email content is deliberately not kept in the conversation history.]"
_REFERENCE_TTL_SECONDS = 600  # a "what was it about?" ten minutes later is not "it" anymore; ask again instead of guessing


class GmailReferenceTracker:
    """Remembers, per conversation session, the id of the last email a Gmail tool actually resolved -- so a follow-up
    like "What was it about?" or "Who sent it?" (Phase 10: multi-turn) can refer back to it without the user repeating
    the sender or subject. Only an opaque (message_id, thread_id) pair is kept, never any content: the email's text
    still never re-enters the conversation history or this model's context (see `history_placeholder` above), and a
    follow-up re-fetches the message fresh from Gmail by id rather than reusing anything cached from before."""

    def __init__(self, clock: Any, ttl_seconds: float = _REFERENCE_TTL_SECONDS):
        self._clock = clock
        self._ttl = ttl_seconds
        self._last: dict[str, tuple[str, float]] = {}  # session_id -> (message_id, expires_at)

    def remember(self, session_id: str | None, message_id: str) -> None:
        if session_id:
            self._last[session_id] = (message_id, self._clock().timestamp() + self._ttl)

    def recall(self, session_id: str | None) -> str | None:
        if not session_id:
            return None
        entry = self._last.get(session_id)
        if entry is None:
            return None
        message_id, expires_at = entry
        if self._clock().timestamp() > expires_at:
            del self._last[session_id]
            return None
        return message_id

    def forget(self, session_id: str | None) -> None:
        if session_id:
            self._last.pop(session_id, None)


@dataclass(frozen=True)
class GmailToolContext:
    service: GmailService
    zone: ZoneInfo
    clock: Any  # Callable[[], datetime]
    references: GmailReferenceTracker | None = None


def _when(context: GmailToolContext, value: datetime | None) -> str:
    return format_when(value, context.clock(), context.zone) if value else "at an unknown time"


def _flags(message: GmailMessage) -> str:
    bits = []
    if message.is_unread:
        bits.append("unread")
    if message.has_attachments:
        bits.append("with an attachment" if len(message.attachments) == 1 else f"with {len(message.attachments)} attachments")
    return f" ({', '.join(bits)})" if bits else ""


def _item(message: GmailMessage) -> str:
    return f"{one_line(message.sender.display if message.sender else 'an unknown sender', 60)}: {one_line(message.subject or '(no subject)', 90)}{_flags(message)}"


def _candidates(messages: list[GmailMessage]) -> str:
    listed = "; ".join(_item(m) for m in messages[:MAX_SPOKEN_ITEMS])
    return (
        f"I found {len(messages)} emails that could match: {listed}. Which one do you mean? "
        "Tell me the sender or a word from the subject, or say the latest one."
    )


class GmailTool(Tool, ABC):
    allowed_scopes = (PermissionScope.ONE_TIME,)
    requires_permission = False
    risk = RiskLevel.LOW
    action: GmailActionName
    history_placeholder = PLACEHOLDER

    def __init__(self, context: GmailToolContext):
        self._ctx = context
        self.name = self.action.value

    @abstractmethod
    def resolve(self, args: BaseModel) -> Ready:
        raise NotImplementedError

    def _pick(self, query: str, latest: bool, same_email: bool = False, origin_session: str | None = None) -> GmailMessage | str:
        """One message, or the sentence to say instead (nothing found / which one? / no earlier email to refer to).
        `same_email` (Phase 10 multi-turn) refers back to the last message this session resolved, re-fetched fresh by
        its id -- never anything cached from before -- rather than searching again by words."""
        if same_email:
            message_id = self._ctx.references.recall(origin_session) if self._ctx.references is not None else None
            if message_id is None:
                return "I'm not sure which email you mean -- we haven't talked about one recently. Which one do you mean?"
            try:
                found = [self._ctx.service.get_message(message_id)]
            except Exception:  # noqa: BLE001 - the message may have been deleted/moved since; ask again rather than fail oddly
                return "I couldn't find that email anymore. Which one do you mean?"
        else:
            found = self._ctx.service.find(query)
        if not found:
            return "I couldn't find a matching email."
        if same_email or latest or len(found) == 1:
            chosen = found[0]
            if self._ctx.references is not None:
                self._ctx.references.remember(origin_session, chosen.message_id)
            return chosen
        return _candidates(found)


class GmailSearchTool(GmailTool):
    action = GmailActionName.SEARCH
    description = "Search the user's Gmail (read-only): unread mail, mail from someone, about a topic, with attachments."
    input_schema = {
        "query": (
            "string: Gmail search words, e.g. 'is:unread', 'from:john', 'internship', 'has:attachment', "
            "'newer_than:7d', 'subject:invoice'. Empty = the most recent mail"
        ),
        "max_results": "integer 1-50, optional (never more than the configured limit)",
    }

    def resolve(self, args: GmailSearchArgs) -> Ready:
        return Ready({"query": args.query, "max_results": args.max_results})

    def run(self, *, query: str, max_results: int | None, origin_session: str | None = None) -> str:
        result = self._ctx.service.search(query, max_results)
        if not result.messages:
            plain_unread = query.split() in (["is:unread"], ["is:unread", "in:inbox"], ["in:inbox", "is:unread"])
            return "You have no unread emails." if plain_unread else "I didn't find any emails matching that."
        shown = result.messages[:MAX_SPOKEN_ITEMS]
        total = result.estimated_total
        head = f"I found about {total} matching emails" if total > result.count else f"I found {result.count} matching email{'s' if result.count != 1 else ''}"
        text = f"{head}. The {'most recent ' if result.count > 1 else ''}{len(shown)}: " + "; ".join(_item(m) for m in shown) + "."
        if result.truncated or total > result.count or result.count > len(shown):
            text += f" I only look at the latest {self._ctx.service.clamp(max_results)} at a time, so there may be more."
        return text


class GmailGetMessageTool(GmailTool):
    action = GmailActionName.GET_MESSAGE
    description = "Read one email the user describes (sender, subject, date, attachments and the start of its text)."
    input_schema = {
        "query": "string: words identifying the email, e.g. 'from:john internship'",
        "latest": "boolean: true = the newest matching email",
        "same_email": "boolean: true = the email already discussed earlier in this conversation (a follow-up), not a new search",
    }

    def resolve(self, args: GmailGetMessageArgs) -> Ready:
        return Ready({"query": args.query, "latest": args.latest, "same_email": args.same_email})

    def run(self, *, query: str, latest: bool, same_email: bool = False, origin_session: str | None = None) -> str:
        message = self._pick(query, latest, same_email, origin_session)
        if isinstance(message, str):
            return message
        ctx = self._ctx
        body = sanitize_for_prompt(strip_quoted_reply(message.plain_text_body or message.snippet), READ_ALOUD_CHARS)
        text = f"Email from {one_line(message.sender.display if message.sender else 'an unknown sender', 60)}, subject {one_line(message.subject or 'none', 100)}, received {_when(ctx, message.timestamp)}{_flags(message)}."
        if message.attachments:
            text += " Attachments: " + ", ".join(f"{one_line(a.filename, 60)}" for a in message.attachments[:5]) + "."
        return f"{text} It says: {body}" if body else f"{text} It has no text."


class GmailGetThreadTool(GmailTool):
    action = GmailActionName.GET_THREAD
    description = "Read an email conversation (thread) the user describes: participants, count and the latest message."
    input_schema = GmailGetMessageTool.input_schema

    def resolve(self, args: GmailGetThreadArgs) -> Ready:
        return Ready({"query": args.query, "latest": args.latest, "same_email": args.same_email})

    def run(self, *, query: str, latest: bool, same_email: bool = False, origin_session: str | None = None) -> str:
        message = self._pick(query, latest, same_email, origin_session)
        if isinstance(message, str):
            return message
        thread = self._ctx.service.get_thread(message.thread_id)
        if not thread.messages:
            return "That conversation has no readable messages."
        last = thread.messages[-1]
        names = ", ".join(one_line(a.display, 40) for a in thread.participants[:4])
        body = sanitize_for_prompt(strip_quoted_reply(last.plain_text_body or last.snippet), READ_ALOUD_CHARS)
        return (
            f"The conversation '{one_line(thread.subject or 'no subject', 100)}' has {len(thread.messages)} "
            f"message{'s' if len(thread.messages) != 1 else ''} involving {names}. The latest, from "
            f"{one_line(last.sender.display if last.sender else 'an unknown sender', 60)} {_when(self._ctx, last.timestamp)}, says: {body}"
        )


class GmailSummarizeTool(GmailTool):
    action = GmailActionName.SUMMARIZE
    description = (
        "Summarize one email or its whole thread, or say what the sender is asking (focus action_items). "
        "Grounded only in the email's own text."
    )
    input_schema = {
        "query": "string: words identifying the email",
        "latest": "boolean: true = the newest matching email",
        "same_email": "boolean: true = the email already discussed earlier in this conversation (a follow-up), not a new search",
        "thread": "boolean: true = summarize the whole conversation",
        "focus": "summary | action_items | key_points",
    }

    def resolve(self, args: GmailSummarizeArgs) -> Ready:
        return Ready({"query": args.query, "latest": args.latest, "same_email": args.same_email, "thread": args.thread, "focus": args.focus})

    def run(self, *, query: str, latest: bool, thread: bool, focus: str, same_email: bool = False, origin_session: str | None = None) -> str:
        message = self._pick(query, latest, same_email, origin_session)
        if isinstance(message, str):
            return message
        service, chosen = self._ctx.service, SummaryFocus(focus)
        sender = one_line(message.sender.display if message.sender else "an unknown sender", 60)
        if thread:
            summary = service.summarize_thread(service.get_thread(message.thread_id), chosen)
            return f"Summary of the conversation '{one_line(message.subject or 'no subject', 100)}': {summary}"
        summary = service.summarize_message(message, chosen)
        return f"Email from {sender}, '{one_line(message.subject or 'no subject', 100)}': {summary}"


class GmailClassifyTool(GmailTool):
    action = GmailActionName.CLASSIFY
    description = "Say how JARVIS would categorize an email (important, needs action, informational, promotional, personal)."
    input_schema = GmailGetMessageTool.input_schema

    def resolve(self, args: GmailClassifyArgs) -> Ready:
        return Ready({"query": args.query, "latest": args.latest, "same_email": args.same_email})

    def run(self, *, query: str, latest: bool, same_email: bool = False, origin_session: str | None = None) -> str:
        message = self._pick(query, latest, same_email, origin_session)
        if isinstance(message, str):
            return message
        result = self._ctx.service.classify(message)
        label = result.category.value.replace("_", " ")
        text = (
            f"The email from {one_line(message.sender.display if message.sender else 'an unknown sender', 60)}, "
            f"'{one_line(message.subject or 'no subject', 100)}', looks {label} to me ({'; '.join(result.reasons)}). "
            "That's only my own rule-based guess."
        )
        if result.category.value == "action_required":
            asks = find_action_requests(message, 2)
            if asks:
                text += " It says: " + " ".join(one_line(a, 200) for a in asks)
        return text


def build_gmail_tools(context: GmailToolContext) -> list[GmailTool]:
    return [
        GmailSearchTool(context), GmailGetMessageTool(context), GmailGetThreadTool(context),
        GmailSummarizeTool(context), GmailClassifyTool(context),
    ]
