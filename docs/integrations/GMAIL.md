# Gmail integration (read-only, OAuth 2.0)

JARVIS reads your Gmail through Google's official OAuth 2.0 installed-app flow and the Gmail REST API. It is a normal adapter of the Integration Hub
(`integrations/gmail/`), not a separate app. See also [GMAIL_INTEGRATION.md](../GMAIL_INTEGRATION.md) (intelligence layer), [OAUTH_SECURITY.md](../OAUTH_SECURITY.md)
and [INTEGRATION_ARCHITECTURE.md](../INTEGRATION_ARCHITECTURE.md).

## Architecture

```
voice / dashboard / API  ->  HubRouter | /integrations/gmail/* | Personal Operator
                                   |
                     Integration Hub tools (HubTools.call)   <- enabled + connected + permission gate, input validation
                                   |
                         GmailAdapter  (status, normalize, paging)
                                   |
                         GmailService  (query validation, limits)
                                   |
                         HttpGmailClient  (GET only, fixed host, bounded retries)
                                   |
                         GoogleAuthenticator (token refresh, secret storage)
```

The language model never calls Gmail. It can only reach mail through the hub tools above, and everything returned is normalized, size-bounded and marked untrusted.

## Scope

Only `https://www.googleapis.com/auth/gmail.readonly` is requested. JARVIS cannot send, delete, modify, label, archive or compose mail; no such tool exists
(`tests/integrations/test_gmail_oauth_integration.py::test_no_write_tool_exists`).

## Google Cloud setup

1. Enable the Gmail API in a Google Cloud project.
2. Google Auth Platform: audience External, add your own account as a test user, add the `gmail.readonly` scope.
3. Create an OAuth client of type **Desktop app** and download its JSON.
4. Put the JSON in the `secrets/` folder of the JARVIS repository. Keep the file name Google gave it.

## Credential placement

* JARVIS looks for the Desktop client JSON by **structure** (a top-level `installed` object with `client_id`, `client_secret`, `auth_uri`, `token_uri`), not by
  file name, in `JARVIS_SECRETS_DIR` (default `secrets`). An explicit `JARVIS_GMAIL_CREDENTIALS_PATH` file wins if it exists.
* `GMAIL_CLIENT_ID` / `GMAIL_CLIENT_SECRET` in `.env` are an alternative to the file.
* `secrets/`, `client_secret*.json`, `credentials.json`, `token.json` and `.env` are git-ignored. The file is never printed, copied or logged.
* The authorization token is stored under `.jarvis/gmail/token.json`, encrypted with Windows DPAPI when `JARVIS_ENCRYPT_TOKENS=true`.
* Enable the integration with `JARVIS_GMAIL_ENABLED=true` in `.env`.

Without a client file JARVIS reports **"Gmail OAuth credentials not configured."** and keeps running.

## Connection flow

Nothing connects on its own. On an explicit request (dashboard "Connect Gmail", `POST /integrations/gmail/connect`, or "connect Gmail" by voice):

1. the hub checks Gmail is enabled and the client file/credentials exist;
2. the Google consent page opens in your browser on a local loopback port (offline access, consent prompt, 5 minute limit);
3. **you** sign in and allow read-only access (JARVIS never handles your password or MFA);
4. the token is stored securely and validated with a real `users/me/profile` call;
5. the hub records Gmail as connected. The response contains status only, never a token.

## Status

`GET /integrations/gmail/status` makes one live, read-only profile call and returns one of:
`CONNECTED`, `NOT_CONFIGURED`, `AUTH_REQUIRED`, `TOKEN_EXPIRED`, `PERMISSION_DENIED`, `RATE_LIMITED`, `NETWORK_ERROR`, `API_ERROR`, `DISCONNECTED`,
with a safe message, a redacted account (`h***@gmail.com`), scope `Read-only`, and last check time.

## Capabilities

| Hub tool | API | Purpose |
|---|---|---|
| gmail_status | GET /integrations/gmail/status, POST /test | live connection status |
| gmail_unread_count | GET /integrations/gmail/unread/count | exact counts (total, inbox, important) from label counters |
| gmail_labels | GET /integrations/gmail/labels | labels |
| gmail_list_messages | GET /integrations/gmail/messages?query=&limit=&page_token= | paged, normalized messages |
| search_email | GET /integrations/gmail/search?q= | Gmail query syntax search |
| read_email | GET /integrations/gmail/messages/{id} | one message (sanitized, bounded, untrusted) |
| gmail_list_threads | GET /integrations/gmail/threads | paged threads |
| gmail_get_thread | GET /integrations/gmail/threads/{id} | messages of a thread |

Connect and disconnect use `POST /integrations/gmail/connect` and `/disconnect`. All endpoints need the `X-JARVIS-Token` header and a local Host.
Limits: page size 1-25, queries validated by `GmailService`, attachments are never downloaded automatically (opt-in `READ_ATTACHMENT`).

Normalized message: `source_id`, `title` (subject), `summary` (snippet), `timestamp`, metadata `thread_id`, `sender` (display name), `recipients` (names, no addresses),
`labels`, `unread`, `important`, `has_attachment`, `attachments` (metadata only), `injection_suspected`.

## Security model

* Least privilege: read-only scope; write permissions do not exist for Gmail.
* Tokens and the client secret are never in API responses, dashboard, voice replies, LLM prompts or logs (the authenticator logs only exception type names).
* Email is untrusted data. Subject, body, sender display name, HTML part and attachment names are all scanned for prompt injection; text is sanitized and wrapped as
  external content. Reading a hostile email cannot trigger any other action.
* Personal Context items carry provenance (`source=gmail`, message id, thread id); nothing is ingested beyond the existing bounded, opt-in sync.
* Voice never reads sensitive content unless asked; unread answers are short counts.

## Voice

* "How many unread emails do I have?" -> "There are 7 unread emails. 2 are marked important. Would you like me to summarize them?" then "yes" -> sender and subject of the newest five.
* "Connect Gmail" starts the browser sign-in. "Is Gmail connected?", "Disconnect Gmail" (asks for confirmation), "Find emails about ..." work as before.

## Dashboard

The Gmail card shows Connected/Disconnected/Error, redacted account, status, OAuth state, scope (Read-only), last check, and buttons Connect Gmail, Disconnect, Test connection, Refresh status.

## Operations

* Start JARVIS (`scripts/windows/start_jarvis.ps1` or the tray), open the dashboard, press **Connect Gmail**.
* CLI alternative: `python scripts/gmail_cli.py auth` and `python scripts/gmail_cli.py status`.
* Disconnect deletes the local token and revokes it at Google when possible; the client JSON is kept. Reconnect any time.
* The token refreshes automatically; a revoked or expired grant shows `TOKEN_EXPIRED` and needs Connect Gmail again.

## Troubleshooting

| Status | Meaning / fix |
|---|---|
| NOT_CONFIGURED | no client JSON in `secrets/` (it must be a Desktop app client) |
| AUTH_REQUIRED | client found, press Connect Gmail |
| TOKEN_EXPIRED | grant revoked/expired (test-mode grants expire after 7 days): connect again |
| PERMISSION_DENIED | Gmail API not enabled, account not a test user, or read-only scope not granted |
| RATE_LIMITED | wait a minute |
| NETWORK_ERROR | no internet / Google unreachable |
| access_denied at Google | your account must be listed as a test user of the OAuth app |

## Testing

`python -m pytest tests/integrations/test_gmail_oauth_integration.py tests/test_gmail_auth_client.py tests/test_gmail_runtime.py -q` (fakes and `httpx.MockTransport`; no network).
The real authorization is a manual step: Connect Gmail, sign in, allow read-only access.

## Limitations

* Read-only by design; no send/reply/delete/label.
* Google test-mode apps expire refresh tokens after 7 days until the app is published/verified.
* One Gmail account.
* No database tables were added: connection state reuses the hub's records, tokens live in the secret store (so no migration).
