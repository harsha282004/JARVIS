"""Google OAuth 2.0 for a local desktop application, shared by the Gmail and Calendar integrations.

One implementation of the credential-handling pattern (Phase 10), parameterized by scopes and by the error
classes each integration speaks:
- The OAuth client (a "Desktop app" JSON file, or a client id and secret from the environment) and the token
  are local files/values whose locations come from settings, never from the model. They are git-ignored.
- `authorize()` runs Google's installed-app flow (browser + loopback redirect) and is only ever called from a
  setup CLI, never from a conversation.
- `access_token()` loads the stored token, refreshes it when expired, writes it back, and fails safely with a
  speakable error when anything is missing, revoked or unreachable.
- Secrets, tokens and authorization codes are never logged, printed or put in exception text; only the
  exception type is logged.
"""

import json
import os
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from backend.core.logging import get_logger

logger = get_logger(__name__)


class AuthStatus(BaseModel):
    configured: bool  # an OAuth client is available (file or environment)
    authorized: bool  # a token exists
    detail: str = ""
    extra: dict[str, Any] = Field(default_factory=dict)


@dataclass(frozen=True)
class OAuthErrors:
    """The four failures, as the integration's own exception classes (each takes a detail string)."""

    not_configured: type[Exception]
    auth_error: type[Exception]
    revoked: type[Exception]
    unavailable: type[Exception]


class GoogleAuthenticator:
    def __init__(
        self,
        credentials_path: str | Path,
        token_path: str | Path,
        client_id: str = "",
        client_secret: str = "",
        *,
        scopes: tuple[str, ...],
        label: str,
        errors: OAuthErrors,
    ):
        self._credentials_path = Path(credentials_path)
        self._token_path = Path(token_path)
        self._client_id = client_id.strip()
        self._client_secret = client_secret.strip()  # held in memory only; never logged, printed or written by us
        self._scopes = tuple(scopes)
        self._label = label
        self._errors = errors
        self._lock = threading.Lock()
        self._credentials: Any = None

    # ---- overridable seams (tests replace the network parts; the rest is the real code path) -------------

    def _load(self) -> Any:
        from google.oauth2.credentials import Credentials

        return Credentials.from_authorized_user_file(str(self._token_path), list(self._scopes))

    def _refresh(self, credentials: Any) -> None:
        from google.auth.transport.requests import Request

        credentials.refresh(Request())

    # ---- state ------------------------------------------------------------------------------------------------

    @property
    def scopes(self) -> tuple[str, ...]:
        return self._scopes

    @property
    def credentials_path(self) -> Path:
        return self._credentials_path

    @property
    def token_path(self) -> Path:
        return self._token_path

    @property
    def has_client_config(self) -> bool:
        """An OAuth client is available: the JSON file, or a client id + secret from the environment."""
        return self._credentials_path.is_file() or bool(self._client_id and self._client_secret)

    def status(self) -> AuthStatus:
        """Cheap local check: are the files there? (No network, nothing is refreshed.)"""
        configured = self.has_client_config
        authorized = self._token_path.is_file()
        if not configured and not authorized:
            detail = "no OAuth client and no token"
        elif not authorized:
            detail = "OAuth client found; run the authorization flow"
        else:
            detail = "token file found"
        return AuthStatus(configured=configured, authorized=authorized, detail=detail)

    def is_ready(self) -> bool:
        return self._token_path.is_file()

    # ---- tokens -----------------------------------------------------------------------------------------------

    def access_token(self, force_refresh: bool = False) -> str:
        """A currently valid access token. Raises the integration's not-configured / revoked / auth / unavailable
        error; never returns an expired token."""
        with self._lock:
            if not self._token_path.is_file():
                raise self._errors.not_configured("no token file")
            if self._credentials is None:
                try:
                    self._credentials = self._load()
                except Exception as exc:  # noqa: BLE001 - corrupt/unreadable token file
                    logger.error("%s token file could not be read (%s)", self._label, type(exc).__name__)
                    raise self._errors.auth_error("token file unreadable") from None
            credentials = self._credentials
            if force_refresh or not credentials.valid:
                self._refresh_credentials(credentials)
            token = getattr(credentials, "token", None)
            if not token:
                raise self._errors.auth_error("no access token after refresh")
            return token

    def invalidate(self) -> None:
        """Forget the cached access token (e.g. after a 401) so the next call refreshes."""
        with self._lock:
            if self._credentials is not None:
                self._credentials.token = None
                self._credentials.expiry = None

    def _refresh_credentials(self, credentials: Any) -> None:
        from google.auth.exceptions import RefreshError, TransportError

        if not getattr(credentials, "refresh_token", None):
            raise self._errors.revoked("no refresh token")
        try:
            self._refresh(credentials)
        except RefreshError as exc:
            # invalid_grant = the user revoked access, or the token expired for good (e.g. an unverified
            # app's 7-day limit). Not retryable: the user must authorize again.
            logger.error("%s token refresh was rejected (%s)", self._label, type(exc).__name__)
            self._credentials = None
            raise self._errors.revoked("refresh rejected") from None
        except TransportError as exc:
            logger.warning("%s token refresh could not reach Google (%s)", self._label, type(exc).__name__)
            raise self._errors.unavailable("token refresh unreachable") from None
        except Exception as exc:  # noqa: BLE001
            logger.error("%s token refresh failed (%s)", self._label, type(exc).__name__)
            raise self._errors.auth_error("token refresh failed") from None
        self._save(credentials)
        logger.info("%s access token refreshed", self._label)

    def _save(self, credentials: Any) -> None:
        """Write the token file (client id/secret, refresh token). Best-effort owner-only permissions."""
        try:
            self._token_path.parent.mkdir(parents=True, exist_ok=True)
            data = json.loads(credentials.to_json())
            fd = os.open(self._token_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(data, handle)
            try:
                os.chmod(self._token_path, 0o600)
            except OSError:
                pass  # Windows: rely on the per-user profile directory ACLs
        except Exception as exc:  # noqa: BLE001 - a failed save must not lose the in-memory token
            logger.warning("%s token could not be saved (%s)", self._label, type(exc).__name__)

    def _client_config(self) -> dict[str, Any]:
        """The same structure as Google's downloaded client JSON, built from the environment values."""
        return {"installed": {
            "client_id": self._client_id,
            "client_secret": self._client_secret,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": ["http://localhost"],
        }}

    # ---- first-time setup (CLI only) --------------------------------------------------------------------------

    def authorize(self, open_browser: bool = True) -> None:
        """Interactive OAuth: opens the consent page, receives the code on a loopback port and stores the token.
        Requests ONLY this integration's scopes."""
        if not self.has_client_config:
            raise self._errors.not_configured("no OAuth client file or client id/secret")
        from google_auth_oauthlib.flow import InstalledAppFlow

        try:
            if self._credentials_path.is_file():
                flow = InstalledAppFlow.from_client_secrets_file(str(self._credentials_path), list(self._scopes))
            else:
                flow = InstalledAppFlow.from_client_config(self._client_config(), list(self._scopes))
            credentials = flow.run_local_server(port=0, open_browser=open_browser, prompt="consent")
        except Exception as exc:  # noqa: BLE001 - the message can echo parts of the client file: log the type only
            logger.error("%s authorization failed (%s)", self._label, type(exc).__name__)
            raise self._errors.auth_error("authorization failed") from None
        self._save(credentials)
        with self._lock:
            self._credentials = credentials
        logger.info("%s authorized (scopes: %s)", self._label, ", ".join(s.rsplit("/", 1)[-1] for s in self._scopes))
