# Integration troubleshooting

Start with the dashboard's **Integrations** cards (`http://127.0.0.1:8000/dashboard`) or say "What integrations are connected?". Each card shows the real status, last sync, permissions and the last error.

| Status | Meaning | What to do |
|---|---|---|
| 🟢 healthy / connected | last sync succeeded / connected, not synchronized yet | nothing |
| 🔄 syncing | a sync is running | wait |
| 🟡 degraded | temporary problem (network, rate limit, server error); JARVIS retries with backoff | wait, or "Sync now" later; the message says which |
| 🟡 authenticating | waiting for you to finish signing in (a browser window may be open) | finish the consent |
| ⚪ disconnected — "not set up" | the integration is not enabled/configured | set the flag in `.env` (`JARVIS_GMAIL_ENABLED`, …) and connect |
| ⚪ disconnected — message about signing in | `AUTH_ERROR`: token expired/revoked (Google: an unverified app's tokens last 7 days) | Connect again (or `python scripts/gmail_cli.py auth`, `calendar_cli.py auth`, `github_cli.py token`) |
| 🔴 error | something unexpected | see the message and `logs/jarvis.log` (JSON logs: `JARVIS_LOG_JSON=true`) |
| ⚫ disabled | you switched it off | "Turn on Gmail" / the card's Turn on |

## By error kind

| Kind | Spoken as | Fix |
|---|---|---|
| `AUTH_ERROR` | "…access was revoked or has expired. Please run … auth again." / GitHub: "rejected the access token" | reconnect |
| `PERMISSION_ERROR` | "… hasn't been given the CREATE_EVENT permission." / "GitHub didn't allow that" | grant the permission on the card ("Allow JARVIS to create calendar events"), or give the GitHub token access to that repository |
| `RATE_LIMIT` | "… is rate limiting requests" | JARVIS waits at least the provider's Retry-After; nothing to do |
| `NETWORK_ERROR` | "I can't reach …" | check the internet; searches fall back to saved copies and say so |
| `SERVER_ERROR` | "… sent back something I couldn't understand" | usually transient |
| `CONFIGURATION_ERROR` | "It isn't set up yet" / "Google Calendar isn't set up" | follow the setup guide for that integration |
| `NOT_FOUND` | "That … can't be found any more" | the item was deleted at the source |

## Common cases

* **"Gmail is switched off, so I'm not accessing it."** You (or the dashboard) turned it off: "Turn on Gmail".
* **Calendar "I need your permission to create calendar events."** Write access is opt-in: say "Allow JARVIS to create calendar events" or tick CREATE_EVENT on the card. Each event still needs your yes.
* **A repository is not found / "Which repository?"** Associate it: "Remember that owner/name is my main JARVIS repository". The token may not include that repository.
* **GitHub says rate limited immediately.** The reset time is remembered; no request is sent until it passes.
* **Documents are not indexed.** Check `JARVIS_DOCUMENT_DIRS`, `JARVIS_RAG_ENABLED`, that PostgreSQL is migrated (`alembic -c database/alembic.ini upgrade head`, needed for `hub_items` too), and the card's error. Unsupported/hidden files are skipped; a file the parser rejects is listed with `outcome: failed`.
* **Nothing is stored ("items stored: 0").** The `hub_items` table needs migration `0007_hub`; without it syncs fail with a recorded error.
* **Telegram shows nothing.** The bot only sees messages sent **to the bot** (or its groups); your personal chats are not readable through the official API. WhatsApp is unsupported (see `MESSAGING_INTEGRATION.md`).
* **After Disconnect it still shows old data.** Disconnect keeps synchronized items unless you choose to delete them (dashboard prompt / "…and delete its data"). A disabled source is not read anyway.
* **I don't trust a token file.** `python scripts/secret_scan.py`; `.jarvis/` is git-ignored; on Windows new tokens are DPAPI-encrypted. Delete a token: Disconnect, or `python scripts/github_cli.py forget`.
