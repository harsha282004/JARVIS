"""GitHubClient: read-only access to the GitHub REST API (api.github.com) over httpx.

* Read-only by construction: the class has GET methods only; no write endpoint exists in it.
* Rate limits: `X-RateLimit-Remaining/Reset` and `Retry-After` are honored. When the budget is exhausted the client refuses to call until the reset time
  (raising GitHubRateLimited with `retry_after`), so a loop can never hammer GitHub.
* Incremental reads: responses are cached with their ETag and re-requested with `If-None-Match`; a 304 answer costs no rate-limit budget.
* Failures are classified (401 auth, 403/429 rate limit vs permission, 404, 5xx retried with backoff then unavailable, network errors, malformed JSON).
* The token comes from a callable and is only ever put in the Authorization header; it is never logged or placed in an exception.
"""

import time
from collections import OrderedDict
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote

import httpx

from backend.core.logging import get_logger
from integrations.github.models import (
    Branch,
    Commit,
    GitHubAuthError,
    GitHubNotConfigured,
    GitHubPermissionDenied,
    GitHubNotFound,
    GitHubRateLimited,
    GitHubResponseError,
    GitHubUnavailable,
    Issue,
    PullRequest,
    Repo,
    _text,
    parse_time,
    validate_repo,
)

logger = get_logger(__name__)

API = "https://api.github.com"
MAX_ATTEMPTS = 3
MAX_CACHE = 200
PER_PAGE = 30
LOW_BUDGET = 3


class GitHubClient:
    def __init__(self, token_source: Callable[[], str], *, http: httpx.Client | None = None, timeout: float = 15.0,
                 sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.time):
        self._token = token_source
        self._http = http or httpx.Client(timeout=timeout, follow_redirects=False)
        self._sleep, self._clock = sleep, clock
        self._cache: OrderedDict[str, tuple[str, Any]] = OrderedDict()
        self._blocked_until = 0.0  # epoch seconds: the rate-limit reset while the budget is exhausted

    def close(self) -> None:
        self._http.close()

    # ---- endpoints ---------------------------------------------------------------------------------------------------------
    def viewer(self) -> str:
        data = self._get("user")
        login = data.get("login") if isinstance(data, dict) else None
        if not isinstance(login, str):
            raise GitHubResponseError("no login in the response")
        return login

    def repos(self, limit: int = 30) -> list[Repo]:
        data = self._get("user/repos", {"sort": "pushed", "direction": "desc", "per_page": min(limit, 100), "affiliation": "owner,collaborator"})
        return [r for item in self._list(data)[:limit] if (r := self._repo(item)) is not None]

    def repo(self, full_name: str) -> Repo:
        data = self._get(f"repos/{quote(validate_repo(full_name), safe='/')}")
        parsed = self._repo(data)
        if parsed is None:
            raise GitHubResponseError("malformed repository")
        return parsed

    def readme(self, full_name: str, max_chars: int = 60_000) -> str:
        """The repository's README as text (GitHub decodes nothing for us: the API returns base64). Read-only; bounded; untrusted content."""
        import base64

        data = self._get(f"repos/{quote(validate_repo(full_name), safe='/')}/readme")
        if not isinstance(data, dict) or not isinstance(data.get("content"), str):
            raise GitHubResponseError("malformed README response")
        if data.get("encoding") != "base64":
            raise GitHubResponseError("unexpected README encoding")
        try:
            return base64.b64decode(data["content"]).decode("utf-8", "replace")[:max_chars]
        except ValueError as exc:
            raise GitHubResponseError("malformed README content") from exc

    def commits(self, full_name: str, since: datetime | None = None, limit: int = PER_PAGE) -> list[Commit]:
        params: dict[str, Any] = {"per_page": min(limit, 100)}
        if since:
            params["since"] = since.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        out = []
        for item in self._list(self._get(f"repos/{quote(validate_repo(full_name), safe='/')}/commits", params))[:limit]:
            commit = item.get("commit") if isinstance(item, dict) else None
            if not isinstance(commit, dict) or not isinstance(item.get("sha"), str):
                continue
            author = commit.get("author") if isinstance(commit.get("author"), dict) else {}
            out.append(Commit(full_name, item["sha"][:40], _text((commit.get("message") or "").split("\n", 1)[0], 200), _text(author.get("name"), 60) or "unknown", parse_time(author.get("date"))))
        return out

    def issues(self, full_name: str, state: str = "open", since: datetime | None = None, limit: int = PER_PAGE) -> list[Issue]:
        params: dict[str, Any] = {"state": state if state in ("open", "closed", "all") else "open", "per_page": min(limit * 2, 100), "sort": "updated", "direction": "desc"}
        if since:
            params["since"] = since.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        out = []
        for item in self._list(self._get(f"repos/{quote(validate_repo(full_name), safe='/')}/issues", params)):
            if not isinstance(item, dict) or "pull_request" in item or not isinstance(item.get("number"), int):
                continue  # the issues endpoint also lists pull requests: those come from pulls()
            out.append(Issue(full_name, item["number"], _text(item.get("title"), 200), str(item.get("state", "open")), _text((item.get("user") or {}).get("login"), 40),
                             parse_time(item.get("created_at")), parse_time(item.get("updated_at")),
                             tuple(_text(l.get("name"), 30) for l in item.get("labels", []) if isinstance(l, dict))[:8]))
            if len(out) >= limit:
                break
        return out

    def pulls(self, full_name: str, state: str = "open", limit: int = PER_PAGE) -> list[PullRequest]:
        params = {"state": state if state in ("open", "closed", "all") else "open", "per_page": min(limit, 100), "sort": "updated", "direction": "desc"}
        out = []
        for item in self._list(self._get(f"repos/{quote(validate_repo(full_name), safe='/')}/pulls", params))[:limit]:
            if not isinstance(item, dict) or not isinstance(item.get("number"), int):
                continue
            out.append(PullRequest(full_name, item["number"], _text(item.get("title"), 200), str(item.get("state", "open")), bool(item.get("draft")),
                                   _text((item.get("user") or {}).get("login"), 40), parse_time(item.get("created_at")), parse_time(item.get("updated_at")),
                                   bool(item.get("merged_at"))))
        return out

    def branches(self, full_name: str, limit: int = PER_PAGE) -> list[Branch]:
        data = self._get(f"repos/{quote(validate_repo(full_name), safe='/')}/branches", {"per_page": min(limit, 100)})
        return [Branch(full_name, _text(b.get("name"), 100), bool(b.get("protected"))) for b in self._list(data)[:limit] if isinstance(b, dict) and b.get("name")]

    # ---- parsing helpers ---------------------------------------------------------------------------------------------------
    @staticmethod
    def _list(data: Any) -> list:
        if not isinstance(data, list):
            raise GitHubResponseError("expected a list")
        return data

    @staticmethod
    def _repo(item: Any) -> Repo | None:
        if not isinstance(item, dict) or not isinstance(item.get("full_name"), str):
            return None
        try:
            name = validate_repo(item["full_name"])
        except Exception:  # noqa: BLE001 - a malformed name from the API is skipped, not trusted
            return None
        return Repo(name, _text(item.get("description"), 200), bool(item.get("private")), _text(item.get("default_branch"), 60), _text(item.get("language"), 30),
                    parse_time(item.get("pushed_at")), parse_time(item.get("updated_at")), int(item.get("open_issues_count") or 0), bool(item.get("archived")))

    # ---- transport ---------------------------------------------------------------------------------------------------------
    def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        token = self._token()
        if not token:
            raise GitHubNotConfigured("no token")
        now = self._clock()
        if now < self._blocked_until:
            raise GitHubRateLimited("rate limit budget exhausted", retry_after=self._blocked_until - now)
        key = path + "?" + "&".join(f"{k}={v}" for k, v in sorted((params or {}).items()))
        headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "JARVIS-personal-assistant"}
        cached = self._cache.get(key)
        if cached:
            headers["If-None-Match"] = cached[0]
        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                response = self._http.get(f"{API}/{path}", params=params, headers=headers)
            except httpx.HTTPError as exc:
                logger.warning("GitHub network error (%s), attempt %d", type(exc).__name__, attempt)
                if attempt == MAX_ATTEMPTS:
                    raise GitHubUnavailable("network failure") from None
                self._sleep(min(0.5 * 2 ** (attempt - 1), 4.0))
                continue
            self._note_budget(response)
            status = response.status_code
            if status == 304 and cached:
                self._cache.move_to_end(key)
                return cached[1]
            if status == 200:
                try:
                    data = response.json()
                except ValueError:
                    raise GitHubResponseError("not JSON") from None
                etag = response.headers.get("ETag")
                if etag:
                    self._cache[key] = (etag, data)
                    while len(self._cache) > MAX_CACHE:
                        self._cache.popitem(last=False)
                return data
            if status == 401:
                raise GitHubAuthError("token rejected")
            if status == 404:
                raise GitHubNotFound("not found")
            if status in (403, 429):
                wait = self._retry_after(response)
                if status == 429 or response.headers.get("X-RateLimit-Remaining") == "0" or wait is not None:
                    self._blocked_until = max(self._blocked_until, self._clock() + (wait or 60.0))
                    raise GitHubRateLimited("rate limited", retry_after=wait or 60.0)
                raise GitHubPermissionDenied("forbidden")
            if status >= 500:
                if attempt == MAX_ATTEMPTS:
                    raise GitHubUnavailable(f"server error {status}")
                self._sleep(min(0.5 * 2 ** (attempt - 1), 4.0))
                continue
            raise GitHubResponseError(f"unexpected status {status}")
        raise GitHubUnavailable("retries exhausted")

    def _note_budget(self, response: httpx.Response) -> None:
        """Stop early when the budget is nearly used up, so the next request is not the one that gets rejected."""
        try:
            remaining, reset = int(response.headers.get("X-RateLimit-Remaining", "")), float(response.headers.get("X-RateLimit-Reset", ""))
        except ValueError:
            return
        if remaining <= 0:
            self._blocked_until = max(self._blocked_until, reset)

    def _retry_after(self, response: httpx.Response) -> float | None:
        value = response.headers.get("Retry-After")
        if value:
            try:
                return max(1.0, min(float(value), 3600.0))
            except ValueError:
                pass
        if response.headers.get("X-RateLimit-Remaining") == "0":
            try:
                return max(1.0, min(float(response.headers.get("X-RateLimit-Reset", "0")) - self._clock(), 3600.0))
            except ValueError:
                return 60.0
        return None
