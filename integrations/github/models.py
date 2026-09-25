"""GitHub domain models and errors. Everything from GitHub (repository descriptions, commit messages, issue and pull-request titles) is untrusted text
written by other people: it is shown as data, sanitized and bounded, and is never an instruction."""

import re
from dataclasses import dataclass, field
from datetime import datetime

from backend.core.security.trust import sanitize_external


class GitHubError(Exception):
    """Base class. `user_message` is safe to say aloud; exception text never contains a token or content."""

    user_message = "Something went wrong while talking to GitHub."

    def __init__(self, detail: str = "", user_message: str | None = None, retry_after: float | None = None):
        super().__init__(detail or user_message or self.user_message)
        if user_message:
            self.user_message = user_message
        self.retry_after = retry_after


class GitHubNotConfigured(GitHubError):
    user_message = "GitHub isn't connected yet. Add a read-only access token (see docs/GITHUB_INTEGRATION.md)."


class GitHubAuthError(GitHubError):
    user_message = "GitHub rejected the access token. It may have expired or been revoked. Please reconnect GitHub."


class GitHubPermissionDenied(GitHubError):
    user_message = "GitHub didn't allow that. The token may not have access to that repository."


class GitHubRateLimited(GitHubError):
    user_message = "GitHub is rate limiting requests. I'll try again later."


class GitHubUnavailable(GitHubError):
    user_message = "I can't reach GitHub right now."


class GitHubNotFound(GitHubError):
    user_message = "GitHub can't find that repository or item."


class GitHubResponseError(GitHubError):
    user_message = "GitHub sent back something I couldn't understand."


class GitHubInvalid(GitHubError):
    user_message = "That isn't a valid GitHub repository name."


_REPO = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9\-]{0,38})/[A-Za-z0-9_.\-]{1,100}$")


def validate_repo(full_name: str) -> str:
    """`owner/name` only: this string becomes part of a URL path, so anything else is refused."""
    if not isinstance(full_name, str) or not _REPO.match(full_name) or ".." in full_name:
        raise GitHubInvalid("invalid repository name")
    return full_name


def _text(value: object, limit: int) -> str:
    return sanitize_external(value if isinstance(value, str) else "", limit)


@dataclass(frozen=True)
class Repo:
    full_name: str
    description: str
    private: bool
    default_branch: str
    language: str
    pushed_at: datetime | None
    updated_at: datetime | None
    open_issues: int
    archived: bool = False


@dataclass(frozen=True)
class Commit:
    repo: str
    sha: str
    message: str  # first line only
    author: str  # a display name, never an address
    date: datetime | None


@dataclass(frozen=True)
class Issue:
    repo: str
    number: int
    title: str
    state: str
    author: str
    created_at: datetime | None
    updated_at: datetime | None
    labels: tuple[str, ...] = ()


@dataclass(frozen=True)
class PullRequest:
    repo: str
    number: int
    title: str
    state: str
    draft: bool
    author: str
    created_at: datetime | None
    updated_at: datetime | None
    merged: bool = False


@dataclass(frozen=True)
class Branch:
    repo: str
    name: str
    protected: bool = False


@dataclass(frozen=True)
class RepoActivity:
    repo: str
    commits: list[Commit] = field(default_factory=list)
    issues: list[Issue] = field(default_factory=list)
    pulls: list[PullRequest] = field(default_factory=list)
    since: datetime | None = None


def parse_time(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
