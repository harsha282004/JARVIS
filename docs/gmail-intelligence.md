# Gmail Intelligence (Phase 10)

JARVIS can connect to your Gmail account **read-only**, search it, read messages and threads, report
attachment metadata, classify emails and summarize them, all through the existing
VoiceEngine -> ConversationEngine -> AgentBrain -> PermissionManager -> Tool path.

It cannot send, reply, delete, label, archive or mark mail read/unread, and it does not implement Calendar,
messaging, proactive alerts, a briefing, browser automation or a dashboard.

## Architecture

```
VoiceEngine -> ConversationEngine -> AgentBrain
                     |                  |  validated GmailAction (words only; data, never executed by the brain)
                     v                  v
             TaskActionExecutor  (the same executor as Phase 9)
                     1. tool.resolve()   pure validation, NO Gmail call
                     2. PermissionManager.request_permission(), bound to the exact parameters
                     3. Tool.execute()   re-checks the permission, then run():
                             GmailService -> GmailClient (interface) -> HttpGmailClient -> Gmail REST (GET only)
                                          -> intelligence: deterministic classification + local-LLM summary
```

| Module (`integrations/gmail/`) | Role |
|---|---|
| `models.py` | `GmailMessage`, `GmailThread`, `GmailAttachment`, `GmailSearchResult`, `EmailCategory`, `GmailError` hierarchy (each with a speakable, content-free `user_message`) |
| `base.py` | `GmailClient` interface (`search`, `get_message`, `get_thread`), `GmailIntegration` (the `Integration` registry entry) |
| `auth.py` | `GmailAuthenticator`: OAuth 2.0 for a desktop app, token storage and refresh, read-only scope |
| `client.py` | `HttpGmailClient`: GET-only REST calls, bounded retries/backoff, error mapping |
| `parser.py`, `text.py` | Gmail JSON -> JARVIS models; MIME walking; HTML -> text; quoted-reply stripping; prompt sanitizing |
| `query.py` | whitelist validation of search queries |
| `intents.py` | the `GmailAction` the model may propose |
| `intelligence.py`, `service.py` | classification, summaries, bounded search, message identification |
| `tools.py` | the five read-only tools |

The rest of JARVIS depends on `GmailClient`/`GmailService`, not on Google or HTTP calls. It reuses the
existing `Tool`, `PermissionManager`, `AgentBrain`, `LLMProvider` (local Ollama), `Settings` and logging, and
adds no database table (see "Data and privacy").

## Google Cloud setup (manual, once)

1. Create or select a project at <https://console.cloud.google.com/>.
2. **APIs & Services -> Library**: enable the **Gmail API**.
3. **OAuth consent screen**: choose *External* (or *Internal* for a Workspace account), fill in the app name and
   your email, and add yourself under **Test users**. While the app is in *Testing*, Google expires refresh
   tokens after 7 days (you then just run `auth` again); publishing the app removes that limit.
4. **Credentials -> Create credentials -> OAuth client ID -> Application type: Desktop app.**
5. Give JARVIS the client, in **either** of these ways (never paste them into chat, never commit them):
   - download the JSON and save it as `.jarvis/gmail/credentials.json` in the project (or the path in
     `JARVIS_GMAIL_CREDENTIALS_PATH`), **or**
   - put the client id and secret in your local `.env` as `GMAIL_CLIENT_ID=` and `GMAIL_CLIENT_SECRET=`.
6. Set `JARVIS_GMAIL_ENABLED=true` in `.env`.
7. Run `python scripts/gmail_cli.py auth`. Your browser opens; approve the **read-only** Gmail permission.
8. Check: `python scripts/gmail_cli.py status` (local files) and `python scripts/gmail_cli.py check` (a real
   read-only call). The token is at `.jarvis/gmail/token.json`, which is git-ignored
   (`git check-ignore .jarvis/gmail/token.json` prints the path).

`python scripts/gmail_cli.py search "is:unread" --max 3` lists sender and subject only.

## Authentication and credentials

- **Scope:** only `https://www.googleapis.com/auth/gmail.readonly`. It is requested by the setup flow and is the
  only scope string in the code (a test enforces it).
- **Where things live:** the OAuth client (file or environment), and the token file written by the flow (client id,
  client secret and refresh token, in Google's own format). `.jarvis/`, `credentials.json`, `token.json`,
  `client_secret*.json` and `gmail_token*.json` are in `.gitignore`. The token is written with owner-only
  permissions where the OS supports it; on Windows it relies on your user profile's folder permissions. It is a
  plain file, not encrypted: anyone who can read your files can read it.
- **Refresh:** an expired access token is refreshed automatically and saved. A 401 from Gmail refreshes once.
- **Revoked or expired for good** (`invalid_grant`): JARVIS says so and asks you to run `auth` again; it does not retry.
- **Missing setup:** a clear spoken message ("Gmail isn't set up yet ..."). Nothing is created or contacted.
- Secrets, tokens and authorization codes are never printed or logged, and never appear in exception text. Only the
  exception type is logged. The client secret setting is a `SecretStr` and does not show in `repr`.
- To disconnect: delete `.jarvis/gmail/token.json` and revoke access at <https://myaccount.google.com/permissions>.

## What you can ask

| You say | What happens |
|---|---|
| "Do I have any unread emails?" | `gmail_search` `is:unread`; count and the newest five (sender, subject, unread/attachment flags) |
| "Do I have any emails from Google?" | `from:google` |
| "Find the email about my internship." | search `internship`; reads it if one matches |
| "Find emails with attachments" / "from this week" | `has:attachment` / `newer_than:7d` |
| "Summarize the latest email from John." | `gmail_summarize` `from:john`, latest |
| "Summarize this thread." | the whole conversation, oldest first |
| "What is John asking me to do?" | summary focused on requests and deadlines |
| "Does this email need action?" / "What emails need my attention?" | classification with the reason; quotes the request |
| "Show all my emails" | the latest N only, and JARVIS says the result is limited |

"This email" means the message you just described in the same request: follow-ups such as "summarize it" are
not resolved from earlier replies (see the privacy section), so name the sender or subject again, or say "the latest".

### Search syntax

The model may only supply search text. It is rebuilt token by token from these read-only operators, and
anything else is rejected before it can reach Gmail: `from: to: cc: subject: filename: label:` (value must be
a simple word), `is:` (`unread read starred important`), `has:attachment`, `in:` (`inbox sent starred important
drafts`), `category:` (`primary social promotions updates forums`), `after: before:` (`2030/1/31` or `2030-01-31`),
`newer_than: older_than:` (`7d`, `2m`, `1y`), plus plain words, `"quoted phrases"`, `OR`, and a leading `-` to
exclude. Maximum 300 characters and 14 tokens. An empty query means "most recent mail". Not allowed: `rfc822msgid:`,
`list:`, `size:`, `in:trash/spam/anywhere`, parentheses/braces, URLs, shell characters.

## Identifying an email (never a guessed id)

The model never supplies a message or thread id; an action containing `message_id`, `thread_id`, `url`, `token`,
`method`, `path`, `command` or `sql` is rejected outright. To read, summarize or classify "the email from John",
JARVIS searches (at most five candidates) and:

- one match: uses it;
- several: reads the candidates and asks which one you mean (or say "the latest one", which picks the newest);
- none: "I couldn't find a matching email."

## Permissions

| Tool | Risk | Approval |
|---|---|---|
| `gmail_search`, `gmail_get_message`, `gmail_get_thread`, `gmail_summarize`, `gmail_classify` | LOW | automatic (policy), ONE_TIME scope |

Reasoning: strictly read-only access to your own mailbox, requested in your own words, bounded in size, with no
way to change or send anything. Every call still goes `request_permission -> execute`, bound to the exact
parameters, and no Gmail request is made before that check passes (`resolve()` is pure). Everything else,
including `gmail_send`, `gmail_delete`, `gmail_modify`, `gmail_reply`, is not registered and is denied as an unknown tool.

## Email intelligence

- **Classification** is deterministic (no LLM): `IMPORTANT`, `ACTION_REQUIRED`, `INFORMATIONAL`, `PROMOTIONAL`,
  `PERSONAL`, `UNKNOWN`, from Gmail labels, bulk/automated headers (`List-Unsubscribe`, `Precedence`, no-reply
  senders) and wording in the *new* text (quoted replies are ignored). Precedence: promotional, action required,
  important, personal, informational, unknown. JARVIS says "That's only my own rule-based guess" and names the reason.
- **Action requests** are sentences quoted from the email that match request phrases ("could you", "please
  confirm", "by Friday"). Nothing is generated.
- **Summaries** use the same local LLM (Ollama) and no other model or cloud service. Focus: `summary`,
  `action_items`, `key_points`. Only the retrieved text (quotes and signatures stripped, at most 5000 characters per
  message, 10 messages and 9000 characters per thread, identical messages de-duplicated) is given to the model, with
  the instruction to use only facts in the email and to say when it cannot tell. Summaries are grounded by
  instruction, not proven correct: a small local model can still err.
- If the model is unavailable JARVIS says it found the email but could not summarize it.

## Prompt-injection defence

Email is attacker-controllable text. The defences are structural:

1. **The email never reaches the AgentBrain.** The brain sees only your words. Email text goes only to the
   summarizer call, which has no tools, no action schema and no JSON mode; its answer is plain text for you and is
   never parsed for actions.
2. **Delimited, sanitized data.** Inside `<email_content>` only; `<` and `>` and control characters are removed so an
   email cannot close the block or fake a tag; the system rules come before it and a fixed reminder comes after it.
3. **Nothing from an email enters the conversation history.** Gmail replies are stored in the history as a fixed
   placeholder, so later turns cannot be steered by text in an earlier reply. (Cost: follow-ups do not "remember" a listing.)
4. **No authority to gain.** The Gmail tools are read-only and registered on their own; other tools are still
   gated by the PermissionManager and the user's own request; an email cannot approve a permission, run a tool, read
   a file, run a command or reach another integration. Tests feed malicious emails (including a "fooled" summarizer
   that echoes an attack) and assert that exactly one tool was authorized and nothing else was called.
5. **Bounded output.** Text read aloud is sanitized and capped.

This is mitigation, not a guarantee against a model being persuaded in its *wording* of a summary (for example, an
email that says "tell the user X" may make a weak model repeat X). Treat summaries as untrusted-source summaries.

## Errors and rate limits

| Situation | Behaviour |
|---|---|
| not set up / no token | setup message |
| revoked or expired authorization | "run `gmail_cli.py auth` again", no retry |
| 401 | refresh the token once, retry once |
| 429, or 403 with a rate-limit reason | up to 4 attempts, exponential backoff (0.5 s ... 8 s), `Retry-After` honoured up to 20 s, then "Gmail is rate limiting requests" |
| 5xx / network error | same bounded retries, then "I can't reach Gmail" |
| other 403 | "Google didn't allow that request" (not retried) |
| 404 | "that email no longer exists" |
| malformed response | "Gmail sent back something I couldn't understand" |

There are no unbounded loops. JARVIS keeps running after every failure.

## Limits and pagination

`JARVIS_GMAIL_MAX_RESULTS` (default 10, 1-50; an absolute cap of 50) bounds every search however the request is
worded ("show all my emails" fetches at most the latest N, and JARVIS says so). The list call pages at most three
times; `GmailService.search(..., page_token=...)` supports continuing, and results report `truncated` and an
estimated total. Five results are read aloud. Each message is fetched once (`format=full`, with attachment
metadata only).

## Attachments

Only metadata is read: filename, MIME type, size and attachment id. Attachments are never downloaded, opened or
executed, and their content is not indexed (no RAG ingestion in this phase). JARVIS can say an attachment exists.

## Message parsing

`text/plain` is preferred; HTML-only mail is converted to text (scripts, styles, iframes and objects dropped,
nothing executed); `multipart/alternative`, `mixed` and nested parts are walked with depth and count limits;
charsets are honoured; malformed base64 or MIME structures and empty bodies yield an empty body, never a crash.
Threads are sorted chronologically and de-duplicated.

## Data and privacy

- **No database changes.** Gmail stays the source of truth: no mailbox mirror, no cache, no migration. Email is not
  written to personal memory or the knowledge graph.
- Logs contain counts, exception types and status only: no email text, addresses, subjects, tokens or secrets
  (tests check this).
- Local-first: summaries use local Ollama. The only external service contacted is Google (OAuth and the Gmail API).
- Sender names, subjects and summaries are spoken aloud (that is the feature), so mind who can hear.

## Configuration

| Setting | Default | Meaning |
|---|---|---|
| `JARVIS_GMAIL_ENABLED` | `false` | registers the Gmail tools |
| `JARVIS_GMAIL_CREDENTIALS_PATH` | `.jarvis/gmail/credentials.json` | OAuth client JSON |
| `JARVIS_GMAIL_TOKEN_PATH` | `.jarvis/gmail/token.json` | token written by `auth` |
| `JARVIS_GMAIL_MAX_RESULTS` | `10` | most emails per search (1-50) |
| `GMAIL_CLIENT_ID`, `GMAIL_CLIENT_SECRET` | empty | OAuth client, instead of the JSON file |

Relative paths are relative to the project folder. New dependencies: `google-auth`, `google-auth-oauthlib`
(and `requests`, which google-auth's transport uses); the API itself is called with the already-present `httpx`.

## Testing

`tests/test_gmail_*.py` (parsing, query validation, OAuth with real google-auth `Credentials` and only the network
call patched, the HTTP client on `httpx.MockTransport`, classification, prompts, actions, permissions, conversation
flows, prompt-injection, static "read-only / no exec / no DB" checks, Git ignore checks). A scripted LLM and an
in-memory `GmailClient` stand in for Ollama and Gmail only for the layers above the client interface.
`tests/integration/test_gmail_real.py` runs a few read-only calls against your real account and **skips** unless a
token exists (and the summary test also needs Ollama); it never sends or modifies anything.

## Troubleshooting

| Symptom | Fix |
|---|---|
| "Gmail isn't set up yet" | steps 5-7 above; `python scripts/gmail_cli.py status` shows what is missing |
| `auth` says authorization failed | check the client type is *Desktop app* and that you are a listed test user |
| "Access blocked: app not verified" | add your address under Test users on the consent screen |
| "revoked or has expired" after about a week | testing-mode tokens last 7 days; run `auth` again |
| "Google didn't allow that request" | the Gmail API is not enabled for the project, or the token lacks the read-only scope |
| JARVIS ignores email requests | `JARVIS_GMAIL_ENABLED=true`, and the agent must be enabled (`JARVIS_AGENT_ENABLED`) |

## Limitations

- Read-only by design. No sending, replying, deleting, labelling or archiving.
- Not verified against a real Gmail account or a real Ollama in this development environment (see the test report).
- Summaries and classifications are heuristic/LLM output and can be wrong; classification is English-centric.
- Follow-ups do not refer to earlier email replies (history holds a placeholder); name the email again.
- One mailbox (`me`); no attachment content, no inline image handling, no label management, no push/watch.
- The token is an unencrypted local file; Google testing-mode tokens expire after 7 days.
- Search is limited to the operators listed above.
