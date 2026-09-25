"""GitHub credentials: an encrypted token file, and the OAuth device flow.

Two ways to connect, both read-only:
  1. a fine-grained personal access token that you create yourself with read-only repository permissions (recommended: you choose exactly which
     repositories it can see); save it with `python scripts/github_cli.py token`;
  2. the OAuth *device flow* for an OAuth App you registered (needs only its public client id, no client secret): JARVIS shows a code, you enter it at
     github.com/login/device, and JARVIS receives a token. The default scope is `read:user` (public data); private repositories need `repo`, which GitHub
     cannot narrow to read-only, so prefer option 1 for private repositories.

The token is stored with Windows DPAPI (backend/core/secrets.py), read on demand, and never logged, printed or placed in an exception message.
"""

import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import httpx

from backend.core.logging import get_logger
from backend.core.secrets import SecretError, is_encrypted, read_secret, write_secret
from integrations.github.models import GitHubAuthError, GitHubNotConfigured, GitHubUnavailable

logger = get_logger(__name__)

DEVICE_URL = "https://github.com/login/device/code"
TOKEN_URL = "https://github.com/login/oauth/access_token"
_TOKEN_PREFIXES = ("ghp_", "github_pat_", "gho_", "ghu_", "ghs_", "ghr_")


class GitHubTokenStore:
    def __init__(self, path: Path, env_token: Callable[[], str] = lambda: "", encrypt: bool = True):
        self._path = Path(path)
        self._env = env_token  # a token supplied through the environment (GITHUB_TOKEN); the file wins when both exist
        self._encrypt = encrypt

    def token(self) -> str:
        """The token, or "" when there is none. A file that cannot be decrypted raises GitHubAuthError (reconnect), never a traceback with content."""
        try:
            return read_secret(self._path).strip()
        except FileNotFoundError:
            return self._env().strip()
        except SecretError:
            raise GitHubAuthError("stored token could not be decrypted") from None

    def is_ready(self) -> bool:
        return self._path.is_file() or bool(self._env().strip())

    def save(self, token: str) -> bool:
        token = token.strip()
        if not token or not any(token.startswith(p) for p in _TOKEN_PREFIXES) or len(token) < 20 or any(c.isspace() for c in token):
            raise GitHubNotConfigured("that does not look like a GitHub token")
        return write_secret(self._path, token, encrypt=self._encrypt)

    def forget(self) -> bool:
        if self._path.is_file():
            self._path.unlink()
            return True
        return False

    def encrypted(self) -> bool:
        return is_encrypted(self._path)


@dataclass(frozen=True)
class DeviceCode:
    device_code: str
    user_code: str
    verification_uri: str
    expires_in: int
    interval: int


class DeviceFlow:
    """GitHub's OAuth device flow. Only a public client id is needed."""

    def __init__(self, client_id: str, scope: str = "read:user", http: httpx.Client | None = None, sleep: Callable[[float], None] = time.sleep,
                 clock: Callable[[], float] = time.monotonic):
        self._client_id, self._scope = client_id.strip(), scope
        self._http = http or httpx.Client(timeout=15.0)
        self._sleep, self._clock = sleep, clock

    def start(self) -> DeviceCode:
        if not self._client_id:
            raise GitHubNotConfigured("no OAuth client id")
        data = self._post(DEVICE_URL, {"client_id": self._client_id, "scope": self._scope})
        try:
            return DeviceCode(data["device_code"], data["user_code"], data["verification_uri"], int(data.get("expires_in", 900)), int(data.get("interval", 5)))
        except (KeyError, TypeError, ValueError):
            raise GitHubAuthError("unexpected device-flow response") from None

    def poll(self, code: DeviceCode) -> str:
        """Block until the user approves (returns the token) or the code expires/is denied (raises). Honors GitHub's requested interval."""
        deadline = self._clock() + code.expires_in
        interval = max(1, code.interval)
        while self._clock() < deadline:
            self._sleep(interval)
            data = self._post(TOKEN_URL, {"client_id": self._client_id, "device_code": code.device_code, "grant_type": "urn:ietf:params:oauth:grant-type:device_code"})
            if isinstance(data.get("access_token"), str):
                return data["access_token"]
            error = data.get("error")
            if error == "authorization_pending":
                continue
            if error == "slow_down":
                interval += 5
                continue
            raise GitHubAuthError(f"device flow ended: {error}")  # expired_token, access_denied, ...
        raise GitHubAuthError("the sign-in code expired")

    def _post(self, url: str, form: dict) -> dict:
        try:
            response = self._http.post(url, data=form, headers={"Accept": "application/json"})
            data = response.json()
        except httpx.HTTPError:
            raise GitHubUnavailable("network failure") from None
        except ValueError:
            raise GitHubAuthError("response was not JSON") from None
        if not isinstance(data, dict):
            raise GitHubAuthError("unexpected response")
        return data
