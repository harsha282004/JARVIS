"""HubRouter: spoken requests about the integrations themselves, answered through HubTools (never a direct API call) and worded from real results.

    "Is Gmail connected?"                       -> the registry's actual status
    "Find emails about hackathons" / "What emails do I have about the project?"
    "What's my schedule today?" / "Do I have anything at 4 PM?"
    "Create a meeting tomorrow at 6 PM"         -> permission check, then a confirmation naming the exact event; created + read back before "Done"
    "Show my repositories" / "What changed in my JARVIS repo?" / "Any open issues?" / "Latest commit?"
    "Remember that owner/name is my main JARVIS repository"
    "Search my documents for ..." / "Search my messages for ..."
    "Turn off GitHub" / "Turn on Gmail" / "Sync Gmail now" / "Disconnect Gmail" (confirmation) / "Allow JARVIS to create calendar events"

Every failure is spoken from the classified error ("Gmail: the connection has expired or was revoked. Please reconnect it."), never as "something went wrong".
Untrusted text (subjects, commit messages, titles) is only quoted as data; replies stay out of the LLM history like the other personal-data replies.
"""

import re
from datetime import datetime, timedelta
from typing import Any

from agent.events.dates import WhenKind, resolve_when
from agent.intelligence.confirmation import ActionReport
from agent.intelligence.followups import FollowUps, Last
from agent.intelligence.findings import Offer
from agent.intelligence.models import Answer, Provenance, SourceKind, Statement, fact
from agent.intelligence.phrasing import clock, day_word, join_and, quoted, when_phrase
from agent.intelligence.prefs_intents import parse_clock
from agent.memory.models import Confidence
from agent.tasks.timeparse import TimeParser
from backend.core.action_audit import ActionResult, Confirmation
from backend.core.logging import get_logger
from backend.core.security.approval import ApprovalClass
from integrations.hub.hub import IntegrationHub
from integrations.hub.models import ErrorKind, IntegrationStatus, Permission, ToolResult
from integrations.github.models import validate_repo

logger = get_logger(__name__)

_NAMES = {"gmail": "gmail", "email": "gmail", "mail": "gmail", "calendar": "calendar", "google calendar": "calendar", "github": "github", "git hub": "github",
          "messaging": "messaging", "messages": "messaging", "telegram": "messaging", "whatsapp": "whatsapp", "documents": "documents", "docs": "documents"}
_NAME_RE = "|".join(sorted(map(re.escape, _NAMES), key=len, reverse=True))
_DAYS = "today|tomorrow|monday|tuesday|wednesday|thursday|friday|saturday|sunday"

_CONNECTED = re.compile(rf"^(?:is|are) (?:my )?(?P<n>{_NAME_RE}) (?:connected|working|set up|linked|online|up)$|^(?:what|which) integrations (?:are|do i have)(?: connected| set up| working)?$|^integrations? status$|^(?:show|list) (?:my )?integrations$")
_EMAIL_SEARCH = re.compile(r"^(?:find|search|show|list|look for|get)(?: me)?(?: all)?(?: my)? (?:emails?|mail|messages from my inbox)(?: (?:about|regarding|on|for|from|mentioning|with))? ?(?P<q>.*)$|^what (?:emails?|mail) do i have(?: (?:about|regarding|on|from))? ?(?P<q2>.*)$|^(?:do i have|any) (?:any )?(?:new |important )?emails?(?: (?:about|regarding|on|from) (?P<q3>.+))?$")
_SCHEDULE = re.compile(rf"^(?:what(?:'s| is)|show|read|tell me)(?: me)? (?:my|the) (?:schedule|calendar|agenda)(?: for)? ?(?P<d>{_DAYS}|this week|next week)?$|^what do i have (?:on|for) (?:my calendar )?(?P<d2>{_DAYS})$|^do i have anything (?:at|around) (?P<t>\d{{1,2}}(?::\d{{2}})?\s*[ap]\.?m\.?)(?: (?P<d3>{_DAYS}))?$")
_CREATE = re.compile(rf"^(?:create|schedule|add|set up|book|make)(?: me)? (?:a |an )?(?P<title>[a-z0-9' \-]{{0,40}}?)\s*(?P<kind>meeting|event|appointment|call|reminder event)\s+(?:for |on |at )?(?P<when>(?:{_DAYS}|next \w+day|on \w+day|\d{{1,2}}(?:st|nd|rd|th)? \w+|in \w+ (?:days?|weeks?)).*)$")
_REPOS = re.compile(r"^(?:show|list|what are)(?: me)? (?:all )?(?:my )?(?:github )?(?:repositories|repos)$")
_CHANGED = re.compile(r"^what(?:'s| has| have)? (?:changed|happened|new)(?: recently)? (?:in|on|with) (?:my )?(?:the )?(?P<n>.+?)(?: (?:github )?(?:repo|repository|project))(?: (?:this week|recently|today|lately))?$|^(?:show|get) (?:me )?(?:the )?(?:recent )?(?:github )?activity(?: (?:for|in|of|on) (?:my )?(?P<n2>.+?))?(?: (?:repo|repository|project))?$")
_ISSUES = re.compile(r"^(?:are there|do i have|any|show(?: me)?|list) (?:any )?(?:my )?open (?P<what>issues|pull requests|prs)(?: (?:in|on|for) (?:my )?(?P<n>.+?))?(?: (?:repo|repository|project))?$|^what(?:'s| are) (?:the )?open (?P<what2>issues|pull requests|prs)(?: (?:in|on|for) (?:my )?(?P<n3>.+?))?(?: (?:repo|repository|project))?$")
_LATEST = re.compile(r"^what(?:'s| is) the (?:latest|last|most recent) commit(?: (?:in|on|for|of) (?:my )?(?P<n>.+?))?(?: (?:repo|repository|project))?$")
_ASSOC = re.compile(r"^(?:remember that |associate |link |connect )(?P<repo>[A-Za-z0-9][\w\-]*/[\w.\-]+)(?: is | with | to | as )(?:my )?(?:main )?(?P<proj>[\w' \-]+?)(?: project| repository| repo)?(?: repository| repo)?$", re.I)
_DOCS = re.compile(r"^(?:search|find|look)(?: through| in)? (?:my )?(?:documents?|docs|files)(?: for| about)? (?P<q>.+)$")
_MSGS = re.compile(r"^(?:search|find|look)(?: through| in)? (?:my )?(?:messages?|telegram)(?: for| about)? (?P<q>.+)$|^(?:check|show|read) (?:my )?(?:latest|recent|new) messages$")
_ONOFF = re.compile(rf"^(?P<a>turn off|switch off|disable|turn on|switch on|enable|stop using|start using) (?:my )?(?P<n>{_NAME_RE})(?: integration)?$")
_SYNC = re.compile(rf"^(?:sync|refresh|update) (?:my )?(?P<n>{_NAME_RE})(?: now)?$")
_DISCONNECT = re.compile(rf"^(?:disconnect|unlink|sign out of|log out of) (?:my )?(?P<n>{_NAME_RE})(?: integration)?(?P<purge> and delete (?:its|the|my) (?:data|everything))?$")
_ALLOW = re.compile(r"^(?P<a>allow|let|stop|don't let|do not let|prevent) (?:jarvis )?(?:from )?(?:to )?(?:creating|create|updating|update|deleting|delete|reading|read|indexing|index) (?P<what>calendar events?|events?|attachments?|documents?|files?)$|^(?P<a2>allow|stop|prevent) jarvis (?:from )?(?P<verb>creating|updating|deleting|reading|indexing) (?P<what2>calendar events|events|attachments|documents)$")


def _canonical(name: str) -> str:
    return _NAMES.get(name.strip().lower(), name.strip().lower())


class HubRouter(FollowUps):
    def __init__(self, hub: IntegrationHub, service, *, remember=None):
        """`service` is the IntelligenceService (context, executor, confirmations, audit, timeline); `remember(text)` stores an explicit user memory."""
        self._hub, self._svc, self._remember = hub, service, remember
        self._tp = TimeParser(service.zone)
        self._last: Last | None = None

    # ---- entry -------------------------------------------------------------------------------------------------------------
    def handle(self, t: str, original: str, session_id: str) -> str | None:
        reg = self._hub.registry
        followed = self._follow_up(t)
        if followed is not None:
            return followed
        m = _CONNECTED.match(t)
        if m:
            return self._connected(_canonical(m.group("n")) if m.groupdict().get("n") else None)
        m = _ONOFF.match(t)
        if m:
            return self._onoff(_canonical(m.group("n")), m.group("a").startswith(("turn on", "switch on", "enable", "start")))
        m = _SYNC.match(t)
        if m:
            return self._sync(_canonical(m.group("n")))
        m = _DISCONNECT.match(t)
        if m:
            return self._disconnect(_canonical(m.group("n")), bool(m.group("purge")), session_id)
        m = _ALLOW.match(t)
        if m:
            return self._allow(t)
        m = _EMAIL_SEARCH.match(t)
        if m and ("email" in t or "mail" in t):
            return self._emails((m.group("q") or m.group("q2") or m.group("q3") or "").strip(), t)
        m = _SCHEDULE.match(t)
        if m:
            return self._schedule(m.group("d") or m.group("d2") or m.group("d3"), m.group("t"))
        m = _CREATE.match(t)
        if m:
            return self._create_event(m.group("title").strip(), m.group("kind"), m.group("when").strip(), session_id)
        if _REPOS.match(t):
            return self._repos()
        m = _ASSOC.match(original.strip().rstrip(".!?"))
        if m:
            return self._associate(m.group("repo"), m.group("proj"))
        m = _LATEST.match(t)
        if m:
            return self._latest(m.group("n"))
        m = _ISSUES.match(t)
        if m:
            return self._issues_prs((m.group("what") or m.group("what2")), m.group("n") or m.group("n3"))
        m = _CHANGED.match(t)
        if m:
            return self._changed(m.group("n") or m.groupdict().get("n2"))
        m = _DOCS.match(t)
        if m:
            return self._docs(m.group("q"))
        m = _MSGS.match(t)
        if m:
            return self._messages(m.groupdict().get("q") or "")
        _ = reg
        return None

    # ---- helpers -----------------------------------------------------------------------------------------------------------
    def _fail(self, r: ToolResult) -> str:
        err = r.error or {"message": "That didn't work."}
        return err["message"] if err["message"].endswith((".", "?", "!")) else err["message"] + "."

    def _record(self, answer_text: str, statements: list[Statement], subject: str) -> str:
        """Keep the evidence so "where did you get that?" / "why?" can answer for hub results too."""
        if statements:
            self._svc.explanations.record("answer", subject, tuple(statements), lead="I told you that")
        return answer_text

    def _prov(self, item: dict[str, Any], label: str) -> Provenance:
        ts = None
        if item.get("source_timestamp"):
            try:
                ts = datetime.fromisoformat(item["source_timestamp"])
            except ValueError:
                pass
        src = {"gmail": SourceKind.EMAIL, "calendar": SourceKind.CALENDAR, "github": SourceKind.GITHUB, "documents": SourceKind.DOCUMENT, "telegram": SourceKind.MESSAGE}.get(item.get("source_type"), SourceKind.DERIVED)
        confidence = Confidence({"low": 1, "medium": 2, "high": 3}.get(item.get("confidence", "high"), 3))
        return Provenance(src, str(item.get("source_id", "")), label, ts, self._svc.now(), confidence)

    def _project_word(self, q: str) -> str:
        """"the project" / "my project" -> the project being discussed (or the only one)."""
        if re.fullmatch(r"(?:the |my |this |that )?project", q.strip()):
            return self._svc.active_project() or ""
        return q

    # ---- status / switches -------------------------------------------------------------------------------------------------
    def _connected(self, name: str | None) -> str:
        if name == "whatsapp":
            from integrations.messaging.adapter import UNSUPPORTED

            return UNSUPPORTED["whatsapp"]
        r = self._hub.tools.call("integration_status", {"name": name} if name else {})
        if not r.success:
            return self._fail(r)
        if name:
            return r.data["sentence"]
        lines = [f"{d['display_name']}: {d['status']}" for d in r.data]
        return "Here's where your integrations stand. " + "; ".join(lines) + "."

    def _onoff(self, name: str, on: bool) -> str:
        if name == "whatsapp" or self._hub.registry.adapter(name) is None:
            return f"I don't have a {name} integration."
        info = self._hub.registry.info(name)
        self._hub.registry.set_enabled(name, on)
        self._svc._audit_record(f"integration.{name}", "enable" if on else "disable", ActionResult.SUCCESS, Confirmation.USER, name, "")  # noqa: SLF001
        self._svc._invalidate()  # noqa: SLF001
        if on:
            return f"Okay. {info.display_name} is switched on again." + ("" if info.configured else " It still needs to be connected.")
        return f"Okay. {info.display_name} is switched off. I won't read it or use anything I stored from it until you turn it back on."

    def _sync(self, name: str) -> str:
        if self._hub.registry.adapter(name) is None:
            return f"I don't have a {name} integration."
        out = self._hub.engine.sync(name, force=True)
        label = self._hub.registry.info(name).display_name
        if out.skipped:
            return f"I didn't synchronize {label}: {out.skipped}"
        if not out.ok:
            return f"{label} didn't synchronize. {out.error}"
        return f"{label} is up to date. {out.created} new and {out.updated} changed item{'s' if out.created + out.updated != 1 else ''}, {out.unchanged} unchanged."

    def _disconnect(self, name: str, purge: bool, session_id: str) -> str:
        adapter = self._hub.registry.adapter(name)
        if adapter is None:
            return f"I don't have a {name} integration."
        label = adapter.display_name
        summary = (f"I'll disconnect {label}: my saved sign-in for it will be deleted{' and everything I stored from it' if purge else ''}. "
                   f"You'd need to authorize it again to use it. Shall I go ahead?")

        def run() -> ActionReport:
            info = self._hub.disconnect(name, revoke=True, purge=purge)
            ok = not info.configured or info.status is IntegrationStatus.DISCONNECTED
            return ActionReport(ok, f"Done. {label} is disconnected." if ok else f"I tried to disconnect {label} but it still looks connected.", verified=ok)

        return self._svc.confirmations.request(action_class=ApprovalClass.CHANGE_INTEGRATION, tool=f"integration.{name}.disconnect", summary=summary,
                                               params={"name": name, "purge": purge}, run=run, session_id=session_id, source="user_request")

    def _allow(self, t: str) -> str:
        allow = not re.match(r"^(?:stop|don't|do not|prevent)", t)
        verb = "create" if re.search(r"creat", t) else "update" if re.search(r"updat", t) else "delete" if re.search(r"delet", t) else "attachment" if "attachment" in t else "index" if re.search(r"index|documents?|files?", t) else "read"
        perm = {"create": (("calendar", Permission.CREATE_EVENT), "create calendar events"), "update": (("calendar", Permission.UPDATE_EVENT), "update calendar events"),
                "delete": (("calendar", Permission.DELETE_EVENT), "delete calendar events"), "attachment": (("gmail", Permission.READ_ATTACHMENT), "read email attachments"),
                "index": (("documents", Permission.INDEX_DOCUMENTS), "index documents"), "read": (None, "")}[verb]
        if perm[0] is None:
            return "Tell me which permission: creating events, updating events, deleting events, reading attachments or indexing documents."
        (name, permission), words = perm
        reg = self._hub.registry
        if reg.adapter(name) is None:
            return f"I don't have a {name} integration."
        if allow:
            reg.grant(name, permission)
        else:
            reg.revoke_permission(name, permission)
        self._svc._audit_record(f"integration.{name}", "grant" if allow else "revoke", ActionResult.SUCCESS, Confirmation.USER, permission.value, "")  # noqa: SLF001
        return (f"Okay. I'm allowed to {words}, and I'll still ask you before every one." if allow else f"Okay. I won't {words}.")

    # ---- email -------------------------------------------------------------------------------------------------------------
    def _emails(self, q: str, t: str) -> str:
        q = self._project_word(re.sub(r"^(?:the |my )", "", q))
        if re.fullmatch(r"(?:new |important |unread |recent)?", q or "") and not q:
            query = "in:inbox is:unread" if ("new" in t or "unread" in t) else "in:inbox"
            about = ""
        else:
            query = q
            about = f" about {q}"
        r = self._hub.tools.call("search_email", {"query": query, "limit": 10})
        if not r.success:
            return self._fail(r)
        items = r.data
        if not items:
            return f"I didn't find any emails{about}." + (" (I searched my saved copies because Gmail is unreachable.)" if r.metadata.get("from_cache") else "")
        stmts, lines = [], []
        for i in items[:5]:
            md = i["metadata"]
            when = ""
            if i.get("source_timestamp"):
                d = datetime.fromisoformat(i["source_timestamp"]).astimezone(self._svc.zone)
                when = f", {day_word(d, self._svc.now(), self._svc.zone)}"
            flag = f" ({md.get('topic')}, {str(md.get('importance', '')).lower()})" if md.get("topic") not in (None, "other") else ""
            lines.append(f"from {md.get('sender', 'a sender')}: {quoted(i['title'], 60)}{when}{flag}")
            stmts.append(fact(f"Gmail has an email {quoted(i['title'], 60)}.", self._prov(i, f"email {quoted(i['title'], 40)}")))
        self._last = Last("emails", list(items[:5]), about.strip(), None, self._svc.now())
        more = f" and {len(items) - 5} more" if len(items) > 5 else ""
        note = " These came from my saved copies because Gmail is unreachable right now." if r.metadata.get("from_cache") else ""
        text = f"I found {len(items)} email{'s' if len(items) != 1 else ''}{about}: " + "; ".join(lines) + more + "." + note
        return self._record(text, stmts, f"emails{about}")

    # ---- calendar ----------------------------------------------------------------------------------------------------------
    def _schedule(self, day_word_: str | None, at: str | None) -> str:
        now = self._svc.now()
        from agent.intelligence.router import parse_day

        if day_word_ in ("this week", "next week"):
            start = now.replace(hour=0, minute=0, second=0, microsecond=0)
            if day_word_ == "next week":
                start += timedelta(days=7 - now.weekday())
            end, label = start + timedelta(days=7), day_word_
        else:
            day = parse_day(day_word_ or "today", now, self._svc.zone) or now.date()
            start = datetime.combine(day, datetime.min.time(), tzinfo=self._svc.zone)
            end, label = start + timedelta(days=1), day_word(day, now, self._svc.zone)
        r = self._hub.tools.call("search_calendar", {"start": start.isoformat(), "end": end.isoformat(), "limit": 25})
        if not r.success:
            return self._fail(r)
        events = r.data
        if not at:
            self._last = Last("schedule", list(events[:8]), label, day_word_ or "today", now)
        if at:
            hhmm = parse_clock(at.replace(".", ""))
            if hhmm is None:
                return "I didn't catch the time."
            hour, minute = int(hhmm[:2]), int(hhmm[3:])
            hits = [e for e in events if not e["metadata"].get("all_day") and datetime.fromisoformat(e["source_timestamp"]).astimezone(self._svc.zone) <= start.replace(hour=hour, minute=minute)
                    < datetime.fromisoformat(e["metadata"]["end"]).astimezone(self._svc.zone)]
            if not hits:
                return f"Your calendar shows nothing at {clock(start.replace(hour=hour, minute=minute), self._svc.zone)} {label}."
            events, label = hits, f"{label} at {clock(start.replace(hour=hour, minute=minute), self._svc.zone)}"
        if not events:
            return f"Your calendar shows nothing scheduled {label}."
        stmts, lines = [], []
        for e in events[:8]:
            s = datetime.fromisoformat(e["source_timestamp"]).astimezone(self._svc.zone)
            lines.append(f"{quoted(e['title'], 60)} {'all day' if e['metadata'].get('all_day') else 'at ' + clock(s, self._svc.zone)}")
            stmts.append(fact(f"Your calendar shows {quoted(e['title'], 60)}.", self._prov(e, f"calendar event {quoted(e['title'], 40)}")))
        return self._record(f"Your calendar shows {join_and(lines)} {label}.", stmts, f"schedule {label}")

    def _create_event(self, title: str, kind: str, when_phrase_: str, session_id: str) -> str:
        now = self._svc.now()
        ok, reason = self._hub.registry.allowed("calendar", Permission.CREATE_EVENT)
        if not ok:
            if "permission" in reason:
                return "I need your permission to create calendar events. Say 'allow JARVIS to create calendar events' and ask again. I'll still confirm each one with you."
            return reason
        resolved = resolve_when(self._tp, when_phrase_, now)
        if resolved.kind is WhenKind.AMBIGUOUS:
            return resolved.question or "Which day and time?"
        if not resolved.is_resolved or resolved.value is None:
            return "I didn't understand when. Try 'tomorrow at 6 PM'."
        if not resolved.has_time:
            return "What time should it start?"
        start = resolved.value
        if start < now:
            return "That time has already passed. When should it be?"
        name = (title.title() + " " if title else "") + kind.title()
        if self._svc.executor is None:
            return "Google Calendar isn't set up, so I can't create it."
        offer = Offer("calendar_event", name.strip(), start, start + timedelta(hours=1), False,
                      f"I'll add '{name.strip()}' {when_phrase(start, now, self._svc.zone)} for one hour to your calendar. Nobody will be invited. Shall I go ahead?")
        return self._svc.executor.propose_offer(offer, session_id)

    # ---- github ------------------------------------------------------------------------------------------------------------
    def _repo_for(self, name: str | None) -> tuple[str | None, str | None]:
        """(owner/repo, problem). A project name resolves through the user's own associations; otherwise a repository name is searched."""
        adapter = self._hub.registry.adapter("github")
        name = self._project_word((name or "").strip())
        if name and "/" in name:
            try:
                return validate_repo(name), None
            except Exception:  # noqa: BLE001
                return None, "That isn't a valid repository name."
        if name:
            linked = adapter.projects.repos_of(name) if hasattr(adapter, "projects") else []
            if len(linked) == 1:
                return linked[0], None
            if len(linked) > 1:
                return None, "Which repository: " + join_and(linked) + "?"
            r = self._hub.tools.call("search_github", {"query": name, "limit": 5})
            if not r.success:
                return None, self._fail(r)
            if len(r.data) == 1:
                return r.data[0]["source_id"], None
            if len(r.data) > 1:
                return None, "Which repository: " + join_and([d["title"] for d in r.data[:4]]) + "?"
            return None, f"I couldn't find a repository matching '{name}' on GitHub."
        repos = adapter.projects.all_repos() if hasattr(adapter, "projects") else []
        if len(repos) == 1:
            return repos[0], None
        return None, "Which repository do you mean?"

    def _repos(self) -> str:
        r = self._hub.tools.call("search_github", {"query": "", "limit": 10})
        if not r.success:
            return self._fail(r)
        if not r.data:
            return "I don't see any repositories on your GitHub account."
        lines = [f"{d['title']}" + (f" ({d['metadata'].get('language')})" if d["metadata"].get("language") else "") for d in r.data[:8]]
        return self._record("Your repositories: " + join_and(lines) + ".", [fact("GitHub lists these repositories for you.", self._prov(r.data[0], "GitHub repositories"))], "your repositories")

    def _changed(self, name: str | None) -> str:
        repo, problem = self._repo_for(name)
        if repo is None:
            return problem or "Which repository?"
        r = self._hub.tools.call("get_repository_activity", {"repo": repo, "days": 14})
        if not r.success:
            return self._fail(r)
        commits = [i for i in r.data if i["metadata"].get("sha")]
        issues = [i for i in r.data if i["metadata"].get("state") and "number" in i["metadata"] and i["source_id"].count("#")]
        pulls = [i for i in r.data if "!" in i["source_id"]]
        if not (commits or issues or pulls):
            return f"GitHub shows no commits in the last two weeks, and no open issues or pull requests, for {repo}."
        parts, stmts = [], []
        if commits:
            listing = "; ".join(f"{quoted(c['title'], 60)} by {c['metadata'].get('author', 'someone')}" for c in commits[:4])
            parts.append(f"{len(commits)} commit{'s' if len(commits) != 1 else ''} in the last two weeks, most recent: {listing}")
            stmts.append(fact(f"GitHub shows {len(commits)} recent commits in {repo}.", self._prov(commits[0], f"GitHub commit in {repo}")))
        if issues:
            parts.append(f"{len(issues)} open issue{'s' if len(issues) != 1 else ''}")
        if pulls:
            parts.append(f"{len(pulls)} open pull request{'s' if len(pulls) != 1 else ''}")
        return self._record(f"In {repo}: " + "; ".join(parts) + ".", stmts, f"{repo} activity")

    def _issues_prs(self, what: str, name: str | None) -> str:
        repo, problem = self._repo_for(name)
        if repo is None:
            return problem or "Which repository?"
        r = self._hub.tools.call("get_repository_activity", {"repo": repo, "days": 1})
        if not r.success:
            return self._fail(r)
        want_pr = what.startswith(("pull", "pr"))
        found = [i for i in r.data if ("!" in i["source_id"]) == want_pr and i["metadata"].get("number") is not None and i["metadata"].get("state") == "open"]
        label = "pull requests" if want_pr else "issues"
        if not found:
            return f"GitHub shows no open {label} in {repo}."
        listing = "; ".join(f"#{i['metadata']['number']} {quoted(i['title'], 60)}" for i in found[:6])
        return self._record(f"{repo} has {len(found)} open {label[:-1] if len(found) == 1 else label}: {listing}.", [fact(f"GitHub lists {len(found)} open {label} in {repo}.", self._prov(found[0], f"GitHub {label} in {repo}"))], f"{repo} {label}")

    def _latest(self, name: str | None) -> str:
        repo, problem = self._repo_for(name)
        if repo is None:
            return problem or "Which repository?"
        r = self._hub.tools.call("get_repository_activity", {"repo": repo, "days": 60})
        if not r.success:
            return self._fail(r)
        commits = sorted((i for i in r.data if i["metadata"].get("sha")), key=lambda i: i.get("source_timestamp") or "", reverse=True)
        if not commits:
            return f"GitHub shows no commits in {repo} in the last two months."
        c = commits[0]
        when = datetime.fromisoformat(c["source_timestamp"]).astimezone(self._svc.zone) if c.get("source_timestamp") else None
        return self._record(f"The latest commit in {repo} is {quoted(c['title'], 80)} by {c['metadata'].get('author', 'someone')}" + (f", {day_word(when, self._svc.now(), self._svc.zone)}." if when else "."),
                            [fact(f"GitHub shows this as the latest commit in {repo}.", self._prov(c, f"GitHub commit in {repo}"))], f"latest commit in {repo}")

    def _associate(self, repo: str, project: str) -> str:
        adapter = self._hub.registry.adapter("github")
        if adapter is None or not hasattr(adapter, "projects"):
            return "GitHub isn't set up, so I can't link a repository."
        try:
            adapter.projects.associate(project.strip(), repo)
        except Exception:  # noqa: BLE001
            return "That isn't a valid repository name. Use owner/name."
        if self._remember is not None:
            try:
                self._remember(f"{repo} is the main repository for the {project.strip()} project.")
            except Exception as exc:  # noqa: BLE001 - a memory failure must not undo the association
                logger.warning("Could not store the repository memory (%s)", type(exc).__name__)
        self._svc._invalidate()  # noqa: SLF001
        return f"Okay. I'll treat {repo} as the repository for your {project.strip()} project, and remember that."

    # ---- documents / messages ----------------------------------------------------------------------------------------------
    def _docs(self, q: str) -> str:
        r = self._hub.tools.call("search_documents", {"query": q})
        if not r.success:
            return self._fail(r)
        if not r.data:
            return "I couldn't find that information in your authorized documents."
        stmts, lines = [], []
        for d in r.data[:4]:
            page = f", page {d['metadata']['page']}" if d["metadata"].get("page") else ""
            lines.append(f"{d['metadata'].get('filename') or d['title']}{page}")
            stmts.append(fact(f"Your document {quoted(d['title'], 50)} matches.", self._prov(d, f"document {quoted(d['title'], 40)}")))
        return self._record(f"I found matches in {join_and(lines)}.", stmts, f"documents about {q}")

    def _messages(self, q: str) -> str:
        r = self._hub.tools.call("search_messages", {"query": q or " ", "limit": 8} if q else {"query": "the", "limit": 8})
        if not r.success:
            return self._fail(r)
        if not r.data:
            return "I didn't find any matching messages."
        lines = [quoted(i["title"], 70) for i in r.data[:4]]
        return self._record(f"I found {len(r.data)} message{'s' if len(r.data) != 1 else ''}: " + "; ".join(lines) + ".", [fact("A message matches.", self._prov(r.data[0], "a message"))], "messages")
