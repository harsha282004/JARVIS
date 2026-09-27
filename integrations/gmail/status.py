"""Gmail connection status: what is actually true, from a real (lightweight) API call, never from "a credential file exists".

    NOT_CONFIGURED     no Google OAuth client (JSON file or client id/secret) is available
    AUTH_REQUIRED      a client exists but nobody has authorized yet (or the stored token is unusable)
    TOKEN_EXPIRED      the stored authorization was revoked or expired for good: authorize again
    PERMISSION_DENIED  Google refused the request (scope not granted, API not enabled for the project, account not a test user)
    RATE_LIMITED       Google is throttling this account/project right now
    NETWORK_ERROR      Google could not be reached
    API_ERROR          Google answered with something unusable
    DISCONNECTED       switched off in JARVIS (or disconnected by the user)
    CONNECTED          `users/me/profile` succeeded just now

Every message here is safe to speak or show: no token, no client secret, no response body.
"""

from datetime import datetime
from enum import StrEnum
from typing import Any

from integrations.gmail.models import (
    GmailAuthError,
    GmailAuthRevoked,
    GmailError,
    GmailNotConfigured,
    GmailPermissionDenied,
    GmailRateLimited,
    GmailUnavailable,
    redact_address,
)

READ_ONLY_SCOPE_LABEL = "Read-only (gmail.readonly)"


class GmailConnection(StrEnum):
    CONNECTED = "CONNECTED"
    NOT_CONFIGURED = "NOT_CONFIGURED"
    AUTH_REQUIRED = "AUTH_REQUIRED"
    TOKEN_EXPIRED = "TOKEN_EXPIRED"
    PERMISSION_DENIED = "PERMISSION_DENIED"
    RATE_LIMITED = "RATE_LIMITED"
    NETWORK_ERROR = "NETWORK_ERROR"
    API_ERROR = "API_ERROR"
    DISCONNECTED = "DISCONNECTED"


MESSAGES = {
    GmailConnection.CONNECTED: "Gmail is connected (read-only).",
    GmailConnection.NOT_CONFIGURED: "Gmail OAuth credentials not configured.",
    GmailConnection.AUTH_REQUIRED: "Gmail needs to be connected: sign in with Google and allow read-only access.",
    GmailConnection.TOKEN_EXPIRED: "Gmail access expired or was revoked. Connect Gmail again.",
    GmailConnection.PERMISSION_DENIED: "Google refused access to Gmail. Check that the Gmail API is enabled, your account is a test user and read-only access was granted.",
    GmailConnection.RATE_LIMITED: "Gmail is rate limiting requests right now. Try again in a minute.",
    GmailConnection.NETWORK_ERROR: "I couldn't reach Gmail. Check the internet connection.",
    GmailConnection.API_ERROR: "Gmail returned an error I couldn't use.",
    GmailConnection.DISCONNECTED: "Gmail is switched off or disconnected.",
}


def classify_exception(exc: BaseException) -> GmailConnection:
    if isinstance(exc, GmailNotConfigured):
        return GmailConnection.NOT_CONFIGURED
    if isinstance(exc, GmailAuthRevoked):
        return GmailConnection.TOKEN_EXPIRED
    if isinstance(exc, GmailAuthError):
        return GmailConnection.AUTH_REQUIRED
    if isinstance(exc, GmailPermissionDenied):
        return GmailConnection.PERMISSION_DENIED
    if isinstance(exc, GmailRateLimited):
        return GmailConnection.RATE_LIMITED
    if isinstance(exc, GmailUnavailable):
        return GmailConnection.NETWORK_ERROR
    return GmailConnection.API_ERROR if isinstance(exc, GmailError) else GmailConnection.API_ERROR


def build_status(status: GmailConnection, *, account: str | None = None, messages_total: int | None = None, checked_at: datetime | None = None,
                 last_ok: datetime | None = None, client_configured: bool = False, authorized: bool = False) -> dict[str, Any]:
    """The one shape shared by the API, the dashboard, the voice reply and the health check."""
    return {
        "status": status.value,
        "connected": status is GmailConnection.CONNECTED,
        "message": MESSAGES[status],
        "account": redact_address(account) if account else None,
        "scope": READ_ONLY_SCOPE_LABEL,
        "oauth": "authorized" if authorized else ("client configured, not authorized" if client_configured else "not configured"),
        "messages_total": messages_total,
        "checked_at": checked_at.isoformat(timespec="seconds") if checked_at else None,
        "last_success_at": last_ok.isoformat(timespec="seconds") if last_ok else None,
    }
