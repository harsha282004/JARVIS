"""Gmail OAuth 2.0 (read-only scope): the shared Google OAuth handling, configured for Gmail.

See `integrations/google_oauth.py` for how credentials, tokens, refresh and revocation are handled.
"""

from pathlib import Path

from integrations.gmail.models import (
    GmailAuthError,
    GmailAuthRevoked,
    GmailNotConfigured,
    GmailUnavailable,
)
from integrations.google_oauth import GoogleAuthenticator, OAuthErrors

# Least privilege: read-only. Nothing in this phase can send, modify, label, archive or delete mail.
GMAIL_READONLY_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
SCOPES = (GMAIL_READONLY_SCOPE,)

_ERRORS = OAuthErrors(
    not_configured=GmailNotConfigured, auth_error=GmailAuthError, revoked=GmailAuthRevoked, unavailable=GmailUnavailable
)


class GmailAuthenticator(GoogleAuthenticator):
    def __init__(
        self,
        credentials_path: str | Path,
        token_path: str | Path,
        client_id: str = "",
        client_secret: str = "",
    ):
        super().__init__(
            credentials_path, token_path, client_id, client_secret, scopes=SCOPES, label="Gmail", errors=_ERRORS
        )
