"""GitHubService (project <-> repository associations, activity) and GitHubAdapter (the hub's view of GitHub).

All results come from GitHub itself; nothing is invented. Associations between a JARVIS project and a repository are explicit statements by the user
("remember that JARVIS/jarvis is my main JARVIS repository"), stored locally, and never guessed from names.
Sync tracks the associated repositories plus the few most recently pushed ones (bounded), reading commits since the cursor time and the open issues and
pull requests. Sync uses ETag conditional requests, so an unchanged repository costs no rate-limit budget.
"""

import json
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path

from backend.core.state_store import JsonFile
from integrations.github.auth import DeviceFlow, GitHubTokenStore
from integrations.github.client import GitHubClient
from integrations.github.models import RepoActivity, validate_repo
from integrations.hub.models import ErrorKind, HubError, ItemKind, NormalizedItem, Permission, utcnow
from integrations.hub.registry import IntegrationAdapter, SyncBatch

MAX_TRACKED = 8
INITIAL_DAYS = 14


class ProjectRepos:
    """project name -> repositories, chosen by the user. Local JSON."""

    def __init__(self, path: Path | None = None):
        self._file = JsonFile(path, {}) if path else None
        self._mem: dict[str, list[str]] = {}

    def _all(self) -> dict[str, list[str]]:
        return self._mem if self._file is None else self._file.read()

    def associate(self, project: str, full_name: str) -> None:
        full_name = validate_repo(full_name)
        data = self._all()
        repos = data.setdefault(project.strip().lower(), [])
        if full_name not in repos:
            repos.append(full_name)
        if self._file is None:
            self._mem = data
        else:
            self._file.write(data)

    def dissociate(self, project: str, full_name: str) -> None:
        data = self._all()
        data[project.strip().lower()] = [r for r in data.get(project.strip().lower(), []) if r != full_name]
        if self._file is None:
            self._mem = data
        else:
            self._file.write(data)

    def repos_of(self, project: str) -> list[str]:
        return list(self._all().get(project.strip().lower(), []))

    def all(self) -> dict[str, list[str]]:
        return {k: list(v) for k, v in self._all().items()}

    def all_repos(self) -> list[str]:
        return sorted({r for repos in self._all().values() for r in repos})

    def project_of(self, full_name: str) -> list[str]:
        return [p for p, repos in self._all().items() if full_name in repos]


class GitHubAdapter(IntegrationAdapter):
    name = "github"
    display_name = "GitHub"
    permissions = frozenset({Permission.READ_REPOSITORIES, Permission.READ_COMMITS, Permission.READ_ISSUES, Permission.READ_PULL_REQUESTS})
    sync_interval_seconds = 900.0
    manual_connect = True

    def __init__(self, client: GitHubClient, store: GitHubTokenStore, projects: ProjectRepos, clock: Callable[[], datetime] = utcnow,
                 oauth_client_id: str = "", oauth_scope: str = "read:user", on_device_code: Callable[[str, str], None] | None = None):
        self._client, self._store, self.projects, self._clock = client, store, projects, clock
        self._client_id, self._scope, self._on_code = oauth_client_id, oauth_scope, on_device_code

    # ---- state / operations ------------------------------------------------------------------------------------------------
    def is_configured(self) -> bool:
        return self._store.is_ready()

    def authenticate(self) -> None:
        """Device flow when an OAuth client id is configured; otherwise a token must be saved with scripts/github_cli.py."""
        if self.is_configured():
            return
        if not self._client_id:
            raise HubError(ErrorKind.CONFIGURATION_ERROR, "GitHub needs a read-only access token. Run: python scripts/github_cli.py token")
        flow = DeviceFlow(self._client_id, self._scope)
        code = flow.start()
        if self._on_code:
            self._on_code(code.user_code, code.verification_uri)
        self._store.save(flow.poll(code))

    def health_check(self) -> str:
        return f"GitHub reachable as {self._client.viewer()}"

    def disconnect(self) -> None:
        self._store.forget()

    # ---- normalization -----------------------------------------------------------------------------------------------------
    def _repo_item(self, r, now) -> NormalizedItem:
        return NormalizedItem(ItemKind.REPOSITORY, "github", r.full_name, r.pushed_at or r.updated_at, r.full_name, r.description,
                              {"private": r.private, "default_branch": r.default_branch, "language": r.language, "open_issues": r.open_issues, "archived": r.archived,
                               "projects": self.projects.project_of(r.full_name)}, "high", r.full_name, now)

    def items_from_activity(self, activity: RepoActivity, now: datetime) -> list[NormalizedItem]:
        out = []
        projects = self.projects.project_of(activity.repo)
        for c in activity.commits:
            out.append(NormalizedItem(ItemKind.COMMIT, "github", f"{c.repo}@{c.sha}", c.date, c.message or "(no message)", "", {"repo": c.repo, "author": c.author, "sha": c.sha[:12], "projects": projects}, "high", c.sha, now))
        for i in activity.issues:
            out.append(NormalizedItem(ItemKind.ISSUE, "github", f"{i.repo}#{i.number}", i.updated_at or i.created_at, i.title, "", {"repo": i.repo, "number": i.number, "state": i.state, "author": i.author, "labels": list(i.labels), "projects": projects}, "high", str(i.number), now))
        for p in activity.pulls:
            out.append(NormalizedItem(ItemKind.PULL_REQUEST, "github", f"{p.repo}!{p.number}", p.updated_at or p.created_at, p.title, "", {"repo": p.repo, "number": p.number, "state": p.state, "draft": p.draft, "merged": p.merged, "author": p.author, "projects": projects}, "high", str(p.number), now))
        return out

    def activity(self, full_name: str, since: datetime | None = None, *, commits: bool = True, issues: bool = True, pulls: bool = True) -> RepoActivity:
        return RepoActivity(full_name, self._client.commits(full_name, since, 15) if commits else [], self._client.issues(full_name, "open", None, 15) if issues else [],
                            self._client.pulls(full_name, "open", 15) if pulls else [], since)

    # ---- search / fetch / sync ---------------------------------------------------------------------------------------------
    def search(self, query: str, limit: int) -> list[NormalizedItem]:
        """Repositories the token can see, matched by name/description/language words. `owner/name` fetches that repository."""
        now = self._clock()
        q = query.strip()
        if "/" in q:
            return [self._repo_item(self._client.repo(q), now)]
        words = [w for w in q.lower().split() if len(w) > 1]
        repos = self._client.repos(60)
        hits = [r for r in repos if not words or all(w in f"{r.full_name} {r.description} {r.language}".lower() for w in words)]
        return [self._repo_item(r, now) for r in hits[:limit]]

    def fetch(self, source_id: str) -> NormalizedItem:
        return self._repo_item(self._client.repo(source_id), self._clock())

    def tracked(self) -> list[str]:
        repos = self.projects.all_repos()
        if len(repos) < MAX_TRACKED:
            for r in self._client.repos(MAX_TRACKED):
                if r.full_name not in repos and not r.archived:
                    repos.append(r.full_name)
                if len(repos) >= MAX_TRACKED:
                    break
        return repos[:MAX_TRACKED]

    def sync(self, cursor: str | None, limit: int) -> SyncBatch:
        now = self._clock()
        since = datetime.fromisoformat(json.loads(cursor)["since"]) if cursor else now - timedelta(days=INITIAL_DAYS)
        items: list[NormalizedItem] = []
        for full_name in self.tracked():
            repo = self._client.repo(full_name)
            items.append(self._repo_item(repo, now))
            items.extend(self.items_from_activity(self.activity(full_name, since), now))
        return SyncBatch(items, json.dumps({"since": (now - timedelta(minutes=5)).astimezone(timezone.utc).isoformat()}))
