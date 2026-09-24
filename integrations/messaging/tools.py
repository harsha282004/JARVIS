"""Messaging tools. Read-only: list, search, read, summarize. There is no send, reply, edit, delete or mark-read tool.

Same two-part shape as the Gmail/Calendar tools:
  resolve(args) -> Ready | Clarify   validates arguments and resolves the day words with the Phase 9/11 parsers;
                                     NO provider call happens here.
  run(**params) -> str               the only place a provider is called, reached only through Tool.execute, i.e. after
                                     the PermissionManager authorized exactly these parameters.

The model never supplies a message or conversation id. run() reads the provider, identifies the conversation or message
by code, and asks the user when more than one matches. Replies contain message text (which strangers can write), so the
conversation history keeps only a placeholder (see `history_placeholder`) and the model never reads message content.
Nothing is created from a message: an action request found in one is only reported, and the user must ask for a task,
reminder or event in their own words.

Permission policy: LOW risk, no approval, ONE_TIME scope. Rationale: strictly read-only access to the user's own
messaging account, requested by the user's own words, bounded in size, with no way to change or send anything. Any
other messaging tool name is unknown and therefore denied.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

from pydantic import BaseModel

from agent.events.dates import resolve_when
from agent.tasks.formatting import format_when, local_day_bounds
from agent.tasks.timeparse import TimeParser
from agent.tasks.tools import Clarify, Ready
from agent.tools.base import Tool
from backend.core.security import PermissionScope, RiskLevel
from integrations.gmail.text import one_line, sanitize_for_prompt
from integrations.messaging.intelligence import SummaryFocus
from integrations.messaging.intents import (
    ConversationGetArgs,
    ConversationListArgs,
    MessageActionName,
    MessageGetArgs,
    MessageListArgs,
    MessageSearchArgs,
    MessageSummarizeArgs,
)
from integrations.messaging.models import Conversation, Message, MessagePage
from integrations.messaging.service import MessagingService

MAX_SPOKEN = 5
SNIPPET_CHARS = 120
READ_ALOUD_CHARS = 500
MAX_SUMMARY_MESSAGES = 30
PLACEHOLDER = "[Messages were read to the user. Message content is deliberately not kept in the conversation history.]"
WINDOW_NOTE = " I can only read the recent messages the provider makes available to me, so older ones may exist."


@dataclass(frozen=True)
class MessagingToolContext:
    service: MessagingService
    parser: TimeParser
    clock: Any  # Callable[[], datetime]

    def now(self) -> datetime:
        return self.clock()

    @property
    def zone(self) -> ZoneInfo:
        return self.parser.zone


def _iso(value: datetime | None) -> str | None:
    return value.astimezone(timezone.utc).isoformat() if value else None


def _from_iso(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def _provider_label(name: str) -> str:
    return name[:1].upper() + name[1:]


def _who(message: Message) -> str:
    return one_line(message.sender.display if message.sender else "an unknown sender", 60)


def _where(message: Message) -> str:
    return f" in {one_line(message.conversation_title, 60)}" if message.conversation_title and message.conversation_kind.value != "private" else ""


def _when(ctx: MessagingToolContext, value: datetime | None) -> str:
    return format_when(value, ctx.now(), ctx.zone) if value else "at an unknown time"


def _flags(message: Message) -> str:
    bits = []
    if message.is_unread:
        bits.append("unread")
    if message.attachments:
        bits.append("with an attachment" if len(message.attachments) == 1 else f"with {len(message.attachments)} attachments")
    return f" ({', '.join(bits)})" if bits else ""


def _snippet(message: Message, limit: int = SNIPPET_CHARS) -> str:
    text = one_line(message.text, limit)
    if not text and message.attachments:
        first = message.attachments[0]
        text = f"[{one_line(first.filename or first.kind, 50)}]"
    return text or "(no text)"


def _item(ctx: MessagingToolContext, message: Message) -> str:
    return f"{_who(message)}{_where(message)}, {_when(ctx, message.timestamp)}{_flags(message)}: {_snippet(message)}"


def _spoken(items: list[str]) -> str:
    shown = items[:MAX_SPOKEN]
    return "; ".join(shown) + (f"; and {len(items) - len(shown)} more" if len(items) > len(shown) else "")


def _end(text: str) -> str:
    """Finish a spoken list with a full stop unless it already ends in punctuation."""
    return text if text.endswith((".", "!", "?")) else text + "."


def _providers(messages: list[Message]) -> str:
    names = sorted({m.provider for m in messages})
    return " and ".join(_provider_label(n) for n in names)


def _note(page: MessagePage) -> str:
    return WINDOW_NOTE if page.scope == "recent_window" else ""


class MessagingTool(Tool, ABC):
    allowed_scopes = (PermissionScope.ONE_TIME,)
    requires_permission = False
    risk = RiskLevel.LOW
    action: MessageActionName
    history_placeholder = PLACEHOLDER

    def __init__(self, context: MessagingToolContext):
        self._ctx = context
        self.name = self.action.value

    @abstractmethod
    def resolve(self, args: BaseModel) -> Ready | Clarify:
        raise NotImplementedError

    # ---- shared helpers ---------------------------------------------------------------------------------------------

    def _window(self, scope: str, on: str | None) -> tuple[datetime | None, datetime | None] | Clarify:
        """[since, until) as aware datetimes for the words the user used; (None, None) means 'the latest'."""
        ctx, now = self._ctx, self._ctx.now()
        if on:
            resolved = resolve_when(ctx.parser, on, now)
            if not resolved.is_resolved or resolved.value is None:
                return Clarify("I couldn't understand that day. Could you say it like 'yesterday', 'Friday' or 'March 5'?")
            return local_day_bounds(0, resolved.value, ctx.zone)
        if scope == "today":
            return local_day_bounds(0, now, ctx.zone)
        if scope == "yesterday":
            return local_day_bounds(-1, now, ctx.zone)
        if scope == "this_week":
            start, _ = local_day_bounds(-now.astimezone(ctx.zone).weekday(), now, ctx.zone)
            return start, local_day_bounds(1, now, ctx.zone)[0]
        return None, None

    def _criteria(self, args: Any) -> dict[str, Any] | Clarify:
        window = self._window(args.scope, args.on)
        if isinstance(window, Clarify):
            return window
        return {"sender": args.sender or "", "conversation": args.conversation or "", "since": _iso(window[0]), "until": _iso(window[1])}

    def _conversation_id(self, name: str) -> str | Clarify:
        """One exact conversation by its name, or the question to ask."""
        found = self._ctx.service.conversations(name)
        if not found:
            return Clarify(f"I couldn't find a conversation called '{one_line(name, 40)}' in the messages I can read.")
        if len(found) > 1:
            return Clarify(
                f"I found {len(found)} conversations that could match: {_spoken([one_line(c.display, 50) for c in found])}. Which one do you mean?"
            )
        return found[0].conversation_id

    def _read(self, *, conversation: str, sender: str, since: str | None, until: str | None, text: str = "", limit: int | None = None) -> MessagePage | str:
        """The messages for a description, or the sentence to say instead (which conversation?)."""
        cid: str | None = None
        if conversation:
            picked = self._conversation_id(conversation)
            if isinstance(picked, Clarify):
                return picked.message
            cid = picked
        return self._ctx.service.messages(
            conversation_id=cid, text=text, sender=sender, since=_from_iso(since), until=_from_iso(until), limit=limit
        )

    def _pick(self, page: MessagePage, latest: bool) -> Message | str:
        if not page.messages:
            return "I couldn't find a matching message." + _note(page)
        if latest or len(page.messages) == 1:
            return page.messages[0]
        listed = "; ".join(_item(self._ctx, m) for m in page.messages[:MAX_SPOKEN])
        return (
            f"I found {len(page.messages)} messages that could match: {listed}. Which one do you mean? "
            "Tell me the sender or a word from the message, or say the latest one."
        )


class MessageListTool(MessagingTool):
    action = MessageActionName.LIST
    description = "Show the user's latest messages (read-only), optionally from one person or conversation, or from a day."
    input_schema = {
        "scope": "latest | today | yesterday | this_week", "on": "string: a day as said ('Friday')",
        "sender": "string: a person's name", "conversation": "string: a conversation or group NAME",
        "limit": "integer 1-50, optional (never more than the configured limit)",
    }

    def resolve(self, args: MessageListArgs) -> Ready | Clarify:
        criteria = self._criteria(args)
        return criteria if isinstance(criteria, Clarify) else Ready({**criteria, "limit": args.limit})

    def run(self, *, conversation: str, sender: str, since: str | None, until: str | None, limit: int | None, origin_session: str | None = None) -> str:
        page = self._read(conversation=conversation, sender=sender, since=since, until=until, limit=limit)
        if isinstance(page, str):
            return page
        if not page.messages:
            return "I didn't find any messages matching that." + _note(page)
        ctx = self._ctx
        shown = page.messages[:MAX_SPOKEN]
        head = f"You have {page.count} message{'s' if page.count != 1 else ''} on {_providers(page.messages)}."
        text = _end(f"{head} The {'latest ' if page.count > 1 else ''}{len(shown)}: " + "; ".join(_item(ctx, m) for m in shown))
        if page.count > len(shown) or page.truncated:
            text += f" I only look at the latest {ctx.service.clamp(limit)} at a time, so there may be more."
        return text + _note(page)


class MessageSearchTool(MessagingTool):
    action = MessageActionName.SEARCH
    description = "Search the user's messages (read-only) for words, optionally by sender, conversation or day."
    input_schema = {
        "query": "string, required: words to look for, e.g. 'internship' (plain words, no operators)",
        "sender": "string: a person's name", "conversation": "string: a conversation or group NAME",
        "scope": "latest | today | yesterday | this_week", "on": "string: a day as said",
        "limit": "integer 1-50, optional",
    }

    def resolve(self, args: MessageSearchArgs) -> Ready | Clarify:
        criteria = self._criteria(args)
        return criteria if isinstance(criteria, Clarify) else Ready({**criteria, "query": args.query, "limit": args.limit})

    def run(self, *, query: str, conversation: str, sender: str, since: str | None, until: str | None, limit: int | None, origin_session: str | None = None) -> str:
        page = self._read(conversation=conversation, sender=sender, since=since, until=until, text=query, limit=limit)
        if isinstance(page, str):
            return page
        if not page.messages:
            return "I didn't find any messages matching that." + _note(page)
        ctx = self._ctx
        shown = page.messages[:MAX_SPOKEN]
        head = f"I found {page.count} matching message{'s' if page.count != 1 else ''} on {_providers(page.messages)}"
        text = _end(f"{head}. The {'latest ' if page.count > 1 else ''}{len(shown)}: " + "; ".join(_item(ctx, m) for m in shown))
        return text + _note(page)


class MessageGetTool(MessagingTool):
    action = MessageActionName.GET
    description = "Read one message the user describes (sender, time, attachments and the start of its text), with a rule-based guess of its category."
    input_schema = {
        "query": "string: words in the message", "sender": "string: a person's name", "conversation": "string: a conversation or group NAME",
        "scope": "latest | today | yesterday | this_week", "on": "string: a day as said", "latest": "boolean: true = the newest matching message",
    }

    def resolve(self, args: MessageGetArgs) -> Ready | Clarify:
        criteria = self._criteria(args)
        return criteria if isinstance(criteria, Clarify) else Ready({**criteria, "query": args.query or "", "latest": args.latest})

    def run(self, *, query: str, latest: bool, conversation: str, sender: str, since: str | None, until: str | None, origin_session: str | None = None) -> str:
        page = self._read(conversation=conversation, sender=sender, since=since, until=until, text=query, limit=5)
        if isinstance(page, str):
            return page
        message = self._pick(page, latest or not (query or sender or conversation))
        if isinstance(message, str):
            return message
        ctx = self._ctx
        body = sanitize_for_prompt(message.text, READ_ALOUD_CHARS)
        text = f"Message from {_who(message)}{_where(message)} on {_provider_label(message.provider)}, {_when(ctx, message.timestamp)}{_flags(message)}."
        if message.attachments:
            text += " Attachments: " + ", ".join(one_line(a.filename or a.kind, 60) for a in message.attachments[:5]) + "."
        text += f" It says: {body}" if body else " It has no text."
        result = ctx.service.classify(message)
        text += f" It looks {result.category.value.replace('_', ' ')} to me ({'; '.join(result.reasons)}); that's only my own rule-based guess."
        asks = ctx.service.action_requests(message)
        if asks:
            ask = asks[0]
            text += f" It seems to ask: {one_line(ask.text, 200)}"
            if ask.deadline_text:
                text += f" A possible deadline: {one_line(ask.deadline_text, 60)}."
            text += " I haven't saved anything; tell me the task or reminder you want and I'll set it up."
        return text


class ConversationListTool(MessagingTool):
    action = MessageActionName.CONVERSATION_LIST
    description = "List the conversations and groups the user can see, most recently active first."
    input_schema = {"query": "string: part of a conversation's name, optional", "limit": "integer 1-50, optional"}

    def resolve(self, args: ConversationListArgs) -> Ready:
        return Ready({"query": args.query or "", "limit": args.limit})

    def run(self, *, query: str, limit: int | None, origin_session: str | None = None) -> str:
        found = self._ctx.service.conversations(query or None, limit)
        if not found:
            return "I didn't find any conversations." + WINDOW_NOTE
        ctx = self._ctx
        items = [f"{one_line(c.display, 50)}{' (' + _when(ctx, c.last_message_at) + ')' if c.last_message_at else ''}" for c in found]
        return f"You have {len(found)} conversation{'s' if len(found) != 1 else ''}: {_spoken(items)}." + WINDOW_NOTE


class ConversationGetTool(MessagingTool):
    action = MessageActionName.CONVERSATION_GET
    description = "Read a conversation or group the user names (the latest messages, oldest first, with sender and time)."
    input_schema = {"conversation": "string: the conversation or group NAME", "latest": "boolean: true = the most recently active one", "limit": "integer 1-50, optional"}

    def resolve(self, args: ConversationGetArgs) -> Ready:
        return Ready({"conversation": args.conversation or "", "latest": args.latest, "limit": args.limit})

    def run(self, *, conversation: str, latest: bool, limit: int | None, origin_session: str | None = None) -> str:
        service, ctx = self._ctx.service, self._ctx
        chosen: Conversation
        if conversation:
            picked = self._conversation_id(conversation)
            if isinstance(picked, Clarify):
                return picked.message
            chosen = service.get_conversation(picked)
        else:
            recent = service.conversations(None, 1)
            if not recent:
                return "I didn't find any conversations." + WINDOW_NOTE
            chosen = recent[0]
        page = service.messages(conversation_id=chosen.conversation_id, limit=min(service.clamp(limit), MAX_SPOKEN))
        ordered = list(reversed(page.messages))  # chronological
        if not ordered:
            return f"The conversation '{one_line(chosen.display, 60)}' has no readable messages." + _note(page)
        lines = "; ".join(f"{_who(m)}, {_when(ctx, m.timestamp)}{_flags(m)}: {_snippet(m, 200)}" for m in ordered)
        return (
            f"The conversation '{one_line(chosen.display, 60)}' on {_provider_label(chosen.provider)}: the latest {len(ordered)} "
            f"message{'s' if len(ordered) != 1 else ''}, oldest first: {lines}." + _note(page)
        )


class MessageSummarizeTool(MessagingTool):
    action = MessageActionName.SUMMARIZE
    description = (
        "Summarize the user's latest messages, or those from one person, conversation, topic or day, or say what someone is "
        "asking (focus action_items). Grounded only in the messages' own text."
    )
    input_schema = {
        "sender": "string: a person's name", "conversation": "string: a conversation or group NAME", "query": "string: a topic word, optional",
        "scope": "latest | today | yesterday | this_week", "on": "string: a day as said",
        "focus": "summary | action_items | key_points",
    }

    def resolve(self, args: MessageSummarizeArgs) -> Ready | Clarify:
        criteria = self._criteria(args)
        return criteria if isinstance(criteria, Clarify) else Ready({**criteria, "query": args.query or "", "focus": args.focus})

    def run(self, *, query: str, focus: str, conversation: str, sender: str, since: str | None, until: str | None, origin_session: str | None = None) -> str:
        service = self._ctx.service
        page = self._read(conversation=conversation, sender=sender, since=since, until=until, text=query, limit=min(service.max_results, MAX_SUMMARY_MESSAGES))
        if isinstance(page, str):
            return page
        if not page.messages:
            return "I didn't find any messages to summarize." + _note(page)
        chosen = SummaryFocus(focus)
        summary = service.summarize(page.messages, chosen)
        text = f"Summary of {page.count} message{'s' if page.count != 1 else ''} on {_providers(page.messages)}: {summary}"
        if chosen is SummaryFocus.ACTION_ITEMS:
            asks = [a for m in page.messages[:10] for a in service.action_requests(m)][:2]
            if asks:
                text += " Quoted from the messages: " + " ".join(one_line(a.text, 200) for a in asks)
                text += " I haven't saved anything."
        return text + _note(page)


def build_messaging_tools(context: MessagingToolContext) -> list[MessagingTool]:
    return [
        MessageListTool(context), MessageSearchTool(context), MessageGetTool(context),
        ConversationListTool(context), ConversationGetTool(context), MessageSummarizeTool(context),
    ]
