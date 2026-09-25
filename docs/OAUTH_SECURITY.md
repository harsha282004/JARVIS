# OAuth and credential security

## What each integration uses

| Integration | Credential | Flow | Scopes (least privilege) |
|---|---|---|---|
| Gmail | Google OAuth token | installed-app flow (browser + loopback redirect, state/PKCE handled by google-auth-oauthlib) | `gmail.readonly` |
| Calendar | Google OAuth token | same | `calendar.events`, `calendar.calendarlist.readonly` |
| GitHub | fine-grained personal access token (recommended) **or** OAuth device flow | you paste the token (hidden input) or enter a code at github.com/login/device | token permissions you choose (read-only); device flow default scope `read:user` |
| Telegram | bot token from BotFather | environment variable or token file | Bot API (only messages sent to your bot) |
| Documents | none | local folders you list | n/a |

## Handling rules (enforced in code and tests)

* **Nothing hard-coded, nothing committed.** Secrets come from environment variables or files under `.jarvis/` (git-ignored). `python scripts/secret_scan.py` scans the repository and runs in the test suite; the redaction layer masks `ghp_`/`github_pat_`/`ya29.`/`GOCSPX-`/Telegram tokens/`Bearer …` in logs and the audit log; a test asserts a token never appears in logs, status output or API responses.
* **Encrypted at rest (Windows).** New tokens are written with **DPAPI** (`backend/core/secrets.py`, `JARVIS_ENCRYPT_TOKENS=true`): bound to your Windows account, no key stored by JARVIS. GitHub tokens saved by `scripts/github_cli.py` and Google tokens saved or refreshed after this change are encrypted; an older plaintext token keeps working and is re-saved encrypted the next time it is refreshed. A file that cannot be decrypted (other account/machine) produces a clean "reconnect" error. **Not encrypted:** a token you put in `.env` (`GITHUB_TOKEN`, bot token) and the Google OAuth *client* JSON; both are yours to protect (`.env` is git-ignored). Off Windows there is no DPAPI: tokens are stored plaintext with owner-only permissions.
* **Refresh.** Google access tokens are refreshed automatically on expiry (`GoogleAuthenticator`); a 401 refreshes once; a rejected refresh means access was revoked (→ `AUTH_ERROR`, "please run … auth again"). GitHub tokens do not refresh: a 401 means reconnect.
* **Expired / revoked access** is detected, recorded (`AUTH_ERROR`), shown on the dashboard as disconnected with the message, **not retried automatically** (the sync engine waits for you), and spoken plainly.
* **Disconnect.** The dashboard's Disconnect button or "Disconnect Gmail" by voice (which asks first) deletes the local token, optionally revokes the grant at Google (`oauth2.googleapis.com/revoke`, best effort), optionally purges everything synchronized from it. GitHub: token file deleted (revoke it on github.com/settings too; GitHub offers no revoke call for fine-grained tokens).
* **Never exposed.** Status, tool results and the API contain state and messages, never tokens. Errors carry only a speakable message.
* **State parameter / PKCE.** Delegated to `google-auth-oauthlib`'s `InstalledAppFlow` (random state checked on the loopback redirect). The GitHub device flow has no redirect (no state needed), honors GitHub's polling interval and `slow_down`, and stops on `access_denied` or expiry.

## Manual steps that only you can do

Create the Google Cloud OAuth "Desktop app" client and consent (see `gmail-intelligence.md`, `google-calendar-integration.md`); create the GitHub token and run `python scripts/github_cli.py token`; create the Telegram bot. None of these could be executed here.

## Known gaps

* The Phase 12 calendar write tools (voice: "move my meeting") are gated by the PermissionManager and per-action confirmation, **not** by the hub's `UPDATE_EVENT`/`DELETE_EVENT` permission. The hub permission governs the hub tool router and plan/offer execution.
* Google OAuth client secrets in `.env` are plaintext by nature of `.env`.
* An unverified Google app's refresh tokens expire after 7 days (Google's rule): expect to reconnect weekly until the app is verified.
