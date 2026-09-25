"""HubTools: the one door through which the agent reaches an integration.

    agent / router  ->  HubTools.call(name, args)  ->  schema validation  ->  registry gate (enabled? connected? permission granted?)
                    ->  adapter  ->  external service  ->  ToolResult { success, source, data, metadata, error }

The agent never calls an external API. Every result has the same shape; a failure carries a classified error (AUTH_ERROR, RATE_LIMIT, ...) and a message that is
safe to say aloud ("Gmail: the connection has expired or was revoked. Please reconnect it."). Reads are automatic once permitted. Writes never happen here: they are
requested (`request_*`), the exact action is described to the user, and only the ConfirmationEngine, after a clear yes, runs them and reads the result back before anyone
says "Done".

Search falls back to what was synchronized earlier when the live service is unreachable, and says so (`metadata.from_cache`); it never presents cached data as fresh.
"""

import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from backend.core.logging import get_logger
from backend.core.metrics import metrics
from integrations.hub.models import ErrorKind, HubError, ItemKind, NormalizedItem, Permission, ToolResult, TRANSIENT_KINDS, classify_error
from integrations.hub.registry import IntegrationRegistry
from integrations.hub.repository import HubRepository

logger = get_logger(__name__)

MAX_QUERY = 200
_README_CONTROL = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\x7f\u200b-\u200f\u2060\ufeff]")
MAX_LIMIT = 25


@dataclass(frozen=True)
class ToolSpec:
    name: str
    source: str  # integration name
    permission: Permission | None
    description: str
    required: tuple[str, ...] = ()
    optional: tuple[str, ...] = ()


TOOL_SPECS: dict[str, ToolSpec] = {s.name: s for s in [
    ToolSpec("integration_status", "hub", None, "Real connection status of one or all integrations", optional=("name",)),
    ToolSpec("search_email", "gmail", Permission.SEARCH_EMAIL, "Search email (live, with a labelled cache fallback)", ("query",), ("limit",)),
    ToolSpec("read_email", "gmail", Permission.READ_EMAIL, "Read one email by id (sanitized, bounded) with its topic, importance and extracted dates", ("message_id",)),
    ToolSpec("search_calendar", "calendar", Permission.READ_EVENTS, "Events matching a query, or the schedule between two times", optional=("query", "start", "end", "limit")),
    ToolSpec("calendar_issues", "calendar", Permission.READ_EVENTS, "Overlaps and duplicates in a date range", optional=("start", "end")),
    ToolSpec("search_github", "github", Permission.READ_REPOSITORIES, "Repositories matching words, or owner/name (no query lists them)", (), ("query", "limit")),
    ToolSpec("get_repository_activity", "github", Permission.READ_COMMITS, "Recent commits, open issues and pull requests of a repository", ("repo",), ("days",)),
    ToolSpec("read_repository_readme", "github", Permission.READ_REPOSITORIES, "The README of one repository (owner/name), bounded and sanitized; untrusted text", ("repo",)),
    ToolSpec("search_documents", "documents", Permission.READ_DOCUMENTS, "Search indexed documents (file and page provenance)", ("query",), ("limit",)),
    ToolSpec("read_document", "documents", Permission.READ_DOCUMENTS, "Read the start of one indexed document", ("document_id",)),
    ToolSpec("search_messages", "messaging", Permission.SEARCH_MESSAGES, "Search messages", ("query",), ("limit",)),
    ToolSpec("search_all", "hub", None, "Search everything synchronized so far (offline capable)", ("query",), ("limit",)),
]}


class HubTools:
    def __init__(self, registry: IntegrationRegistry, repo: HubRepository, clock: Callable[[], datetime], zone, *, calendar_issues_fn=None):
        self._reg, self._repo, self._clock, self._zone = registry, repo, clock, zone
        self._handlers: dict[str, Callable[..., ToolResult]] = {
            "integration_status": self._status, "search_email": self._search_email, "read_email": self._read_email, "search_calendar": self._search_calendar,
            "calendar_issues": self._calendar_issues, "search_github": self._search_github, "get_repository_activity": self._repo_activity, "read_repository_readme": self._read_readme,
            "search_documents": self._search_documents, "read_document": self._read_document, "search_messages": self._search_messages, "search_all": self._search_all,
        }

    # ---- entry point -------------------------------------------------------------------------------------------------------
    def call(self, name: str, args: dict[str, Any] | None = None) -> ToolResult:
        spec = TOOL_SPECS.get(name)
        if spec is None:
            return ToolResult.fail("hub", ErrorKind.INVALID_REQUEST, f"There is no tool called {name}.")
        args = dict(args or {})
        problem = self._validate(spec, args)
        if problem:
            return ToolResult.fail(spec.source, ErrorKind.INVALID_REQUEST, problem)
        if spec.source != "hub":
            ok, reason = self._reg.allowed(spec.source, spec.permission)
            if not ok:
                kind = ErrorKind.PERMISSION_ERROR if "permission" in reason else ErrorKind.CONFIGURATION_ERROR
                return ToolResult.fail(spec.source, kind, reason)
        try:
            with metrics.timer(f"tool.{name}_ms"):
                return self._handlers[name](**args)
        except Exception as exc:  # noqa: BLE001 - every failure becomes a classified ToolResult, never a raw exception into the conversation
            adapter = self._reg.adapter(spec.source)
            err = classify_error(exc, adapter.display_name if adapter else "")
            logger.warning("Tool %s failed (%s)", name, err.kind.value)
            return ToolResult.fail(spec.source, err.kind, err.message)

    @staticmethod
    def _validate(spec: ToolSpec, args: dict[str, Any]) -> str | None:
        allowed = set(spec.required) | set(spec.optional)
        extra = set(args) - allowed
        if extra:
            return f"Unexpected argument: {sorted(extra)[0]}."
        for key in spec.required:
            value = args.get(key)
            if not isinstance(value, str) or not value.strip():
                return f"'{key}' is required."
        for key, value in args.items():
            if isinstance(value, str) and len(value) > MAX_QUERY:
                return f"'{key}' is too long."
            if key in ("limit", "days") and (not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= (MAX_LIMIT if key == "limit" else 365)):
                return f"'{key}' must be a whole number in range."
        return None

    def _adapter(self, name: str):
        return self._reg.adapter(name)

    # ---- status ------------------------------------------------------------------------------------------------------------
    def _status(self, name: str | None = None) -> ToolResult:
        if name:
            if self._reg.adapter(name) is None:
                return ToolResult.fail("hub", ErrorKind.NOT_FOUND, f"I don't have a {name} integration.")
            info = self._reg.info(name)
            connected, sentence = self._reg.is_connected(name)
            return ToolResult.ok("hub", {**info.to_dict(), "connected": connected, "sentence": sentence})
        return ToolResult.ok("hub", [i.to_dict() for i in self._reg.all_info()])

    # ---- email -------------------------------------------------------------------------------------------------------------
    def _search_email(self, query: str, limit: int = 10) -> ToolResult:
        adapter = self._adapter("gmail")
        try:
            items = adapter.search(query, limit)
            cached = False
        except Exception as exc:  # noqa: BLE001
            err = classify_error(exc, "Gmail")
            if err.kind not in TRANSIENT_KINDS:
                raise
            items, cached = self._repo.search(query, source="gmail", kind=ItemKind.EMAIL, limit=limit), True  # the service is unreachable: use what was synced, and say so
            if not items:
                raise
        return ToolResult.ok("gmail", [i.to_dict() for i in items], count=len(items), from_cache=cached, query=query)

    def _read_email(self, message_id: str) -> ToolResult:
        message, analysis = self._adapter("gmail").analysis(message_id)
        from integrations.gmail.adapter import normalize_message
        from backend.core.security.trust import sanitize_external

        items = normalize_message(message, analysis, self._clock())
        body = sanitize_external(message.plain_text_body or message.snippet, 2000)
        return ToolResult.ok("gmail", {"email": items[0].to_dict(), "body_untrusted": body, "extracted": [i.to_dict() for i in items[1:]]},
                             injection_suspected=analysis.flagged, untrusted_fields=["body_untrusted"])

    # ---- calendar ----------------------------------------------------------------------------------------------------------
    def _range(self, start: str | None, end: str | None) -> tuple[datetime, datetime]:
        now = self._clock()
        s = datetime.fromisoformat(start) if start else now
        e = datetime.fromisoformat(end) if end else s + timedelta(days=7)
        if e <= s:
            raise HubError(ErrorKind.INVALID_REQUEST, "The end must be after the start.")
        return s, e

    def _search_calendar(self, query: str | None = None, start: str | None = None, end: str | None = None, limit: int = 20) -> ToolResult:
        adapter = self._adapter("calendar")
        if query and not start:
            items = adapter.search(query, limit)
        else:
            s, e = self._range(start, end)
            items = adapter.schedule(s, e)[:limit]
        return ToolResult.ok("calendar", [i.to_dict() for i in items], count=len(items))

    def _calendar_issues(self, start: str | None = None, end: str | None = None) -> ToolResult:
        from integrations.calendar.adapter import find_calendar_issues

        s, e = self._range(start, end)
        events = self._adapter("calendar").events(s, e)
        issues = find_calendar_issues(events, self._clock(), self._zone)
        return ToolResult.ok("calendar", [{"kind": i.kind, "text": i.text, "items": list(i.items)} for i in issues], count=len(issues), checked=len(events))

    # ---- github ------------------------------------------------------------------------------------------------------------
    def _search_github(self, query: str = "", limit: int = 10) -> ToolResult:
        items = self._adapter("github").search(query, limit)
        return ToolResult.ok("github", [i.to_dict() for i in items], count=len(items))

    def _repo_activity(self, repo: str, days: int = 14) -> ToolResult:
        adapter = self._adapter("github")
        since = self._clock() - timedelta(days=days)
        activity = adapter.activity(repo, since)
        items = adapter.items_from_activity(activity, self._clock())
        return ToolResult.ok("github", [i.to_dict() for i in items], repo=repo, since=since.isoformat(), commits=len(activity.commits), issues=len(activity.issues), pulls=len(activity.pulls))

    def _read_readme(self, repo: str) -> ToolResult:
        from backend.core.security.trust import scan_for_injection
        from integrations.github.models import validate_repo

        text = self._adapter("github").readme(validate_repo(repo))
        scan = scan_for_injection(text)
        # Keep the line structure (the deterministic summarizer needs the headings) but neutralize everything that could act as markup or hidden text.
        clean = _README_CONTROL.sub("", text).replace("<", "(").replace(">", ")")
        clean = re.sub(r"\n{3,}", "\n\n", clean)[:20_000]
        return ToolResult.ok("github", {"repo": repo, "readme_untrusted": clean, "injection_suspected": scan.flagged}, untrusted_fields=["readme_untrusted"], chars=len(clean))

    # ---- documents / messages / everything ---------------------------------------------------------------------------------
    def _search_documents(self, query: str, limit: int = 8) -> ToolResult:
        items = self._adapter("documents").search(query, limit)
        return ToolResult.ok("documents", [i.to_dict() for i in items], count=len(items))

    def _read_document(self, document_id: str) -> ToolResult:
        return ToolResult.ok("documents", self._adapter("documents").fetch(document_id).to_dict(), untrusted_fields=["summary"])

    def _search_messages(self, query: str, limit: int = 10) -> ToolResult:
        items = self._adapter("messaging").search(query, limit)
        return ToolResult.ok("messaging", [i.to_dict() for i in items], count=len(items))

    def _search_all(self, query: str, limit: int = 10) -> ToolResult:
        """Everything synchronized so far, only from integrations that are currently switched on."""
        found: list[NormalizedItem] = []
        for item in self._repo.search(query, limit=limit * 3):
            adapter = next((a for a in (self._reg.adapter(n) for n in self._reg.names()) if a and item.source in a.sources), None)
            if adapter is not None and self._reg.is_enabled(adapter.name):
                found.append(item)
        return ToolResult.ok("hub", [i.to_dict() for i in found[:limit]], count=len(found[:limit]), from_cache=True)
