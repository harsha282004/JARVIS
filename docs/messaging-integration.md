# Messaging Integration (Phase 13)

JARVIS can read, search, understand and summarize your messages from a supported messaging platform, through the
existing VoiceEngine -> ConversationEngine -> AgentBrain -> PermissionManager -> Tool path. It is **read-only**.

It does not send, reply, forward, edit, delete or mark messages, does not monitor messages in the background, does not
notify you, and does not build a briefing, a dashboard, browser automation or a remote/mobile interface.

## What is and is not supported

| Platform | Status | Why |
|---|---|---|
| **Telegram, through the official Bot API** | **Supported (read-only)** | A bot you create with BotFather receives messages people send to it and messages of groups you add it to. `getUpdates` is an official, documented read API. |
| Your personal Telegram chats | Not supported | The Bot API cannot see them. (Telegram's separate user-client API, MTProto/TDLib, could, but it needs a full personal login and is a much larger, riskier integration; it is not part of this phase.) |
| **WhatsApp** | **Not supported** | WhatsApp offers no official API for reading a personal account's chats. Its official WhatsApp Business Platform (Cloud API) is for business numbers and delivers messages to a public HTTPS webhook, which needs inbound infrastructure and remote access that belong to later phases. Automating WhatsApp Web or using unofficial libraries breaks WhatsApp's terms, is fragile and is deliberately **not** implemented. |
| Discord, Slack, SMS, Signal, others | Not implemented | No provider exists for them. The abstraction below can host one later. |
| Email | Not part of this phase | Gmail (Phase 10) stays separate. Messaging never reads, merges or shadows Gmail; every result names its provider. |

Nothing pretends to work: a provider that lacks a capability reports it, and with nothing configured JARVIS says how to
set messaging up.

## Architecture

```
VoiceEngine -> ConversationEngine -> AgentBrain
                     |                  |  validated MessageAction (words only; data, never executed by the brain)
                     v                  v
             TaskActionExecutor  (the same executor as Phases 9-12)
                     1. tool.resolve()   pure validation (day words via the Phase 9/11 parsers); NO provider call
                     2. PermissionManager.request_permission(), bound to the exact parameters
                     3. Tool.execute()   re-checks the permission, then run():
                             MessagingService -> ProviderRegistry -> MessagingProvider (capability interfaces)
                                              -> TelegramProvider -> Telegram Bot API (getMe / getUpdates only)
                                              -> intelligence: rule-based classification/action detection + local-LLM summary
```

| Module (`integrations/messaging/`) | Role |
|---|---|
| `models.py` | `Message`, `Conversation`, `Person`, `Attachment` (metadata), `ReplyRef`, `MessageQuery`, `MessagePage`, `MessageCategory`, `Capability`, and the `MessagingError` hierarchy (each with a speakable, content-free `user_message`) |
| `base.py` | capability-based provider interfaces and the `ProviderRegistry`; `MessagingIntegration` (registry entry) |
| `telegram.py`, `telegram_parser.py` | the Telegram Bot API provider (httpx) and its response normalization |
| `service.py` | bounded retrieval, local filtering, conversation lookup, intelligence entry points |
| `intelligence.py` | classification, action-request detection, grounded summary prompts |
| `intents.py`, `tools.py` | the `MessageAction` the model may propose, and the six read-only tools |

## Provider abstraction

`MessagingProvider` (name, `is_configured()`, `authenticate()`) plus optional capability interfaces:

- `ConversationProvider`: `list_conversations`, `get_conversation`
- `MessageProvider`: `get_messages`, `get_message`
- `SearchProvider`: `search_messages` (provider-native search)

A provider's capabilities are **detected from the interfaces it implements**, so it cannot claim one it does not have,
and `ProviderRegistry.require(...)` raises `UnsupportedCapability` instead of faking a result. There is **no** send,
edit, delete or mark-read interface: those would have to be added, registered and permission-controlled in a future phase.
Telegram bots cannot search history, so Telegram implements only conversations and messages; the service then filters the
recent window locally and always says so (`scope="recent_window"`).

## Authentication and configuration

1. In Telegram, talk to **@BotFather**, send `/newbot` and copy the token it gives you.
2. Give JARVIS the token, in **either** way (never paste it into chat, never commit it):
   - save it as the only content of `.jarvis/messaging/telegram_token` (git-ignored), **or**
   - put `MESSAGING_TELEGRAM_BOT_TOKEN` in your local `.env` (git-ignored; a `SecretStr`, never printed).
3. Set `JARVIS_MESSAGING_ENABLED=true`.
4. Message your bot (or add it to a group). Telegram only lets the bot read recent messages, see Limitations.
5. Check: `python scripts/messaging_cli.py status` (local), `check` (online: the bot's name), `conversations` (names only).
   The CLI only reads.

| Setting | Default | Meaning |
|---|---|---|
| `JARVIS_MESSAGING_ENABLED` | `false` | registers the messaging tools |
| `JARVIS_MESSAGING_MAX_RESULTS` | `20` (1-100) | most messages read or spoken per request |
| `JARVIS_MESSAGING_TELEGRAM_TOKEN_PATH` | `.jarvis/messaging/telegram_token` | token file |
| `MESSAGING_TELEGRAM_BOT_TOKEN` | empty | alternative to the token file |

If the bot has a webhook set, or another program is polling it, Telegram refuses `getUpdates`; JARVIS says so.

## Models

`Message`: `message_id`, `conversation_id`, `provider`, `sender`, `recipients` (where available), `timestamp` (UTC),
`text`, `attachments` (metadata: filename, MIME type, size, attachment id, kind), `reply_to`, `conversation_title`,
`conversation_kind` (private/group/channel), `source` (small provider metadata, e.g. forwarded), `is_unread`
(`None` when the provider cannot say, as for Telegram bots). `Conversation`: `conversation_id`, `title`, `participants`,
`provider`, `last_message_at`, `unread_count` (`None` when unknown). IDs are namespaced (`telegram:<chat>:<message>`), come
only from provider responses, and are validated before use. Raw provider objects never leave the parser and never reach
the AgentBrain.

## What you can say

"Check my latest messages." / "Show messages from John." / "Show messages from my project group." / "Read the latest
conversation." / "Find messages containing project." / "Find the message about my internship." / "Show messages from
today." / "Summarize my latest messages." / "What is John asking me?" / "What is the project group discussing?" /
"Which conversations do I have?"

## Retrieval, search and conversations

- Six actions: `message_list`, `message_search`, `message_get`, `conversation_list`, `conversation_get`, `message_summarize`.
- Every read is bounded (at most `JARVIS_MESSAGING_MAX_RESULTS`, at most five spoken items), one provider request per
  operation, no history download, no polling, no background monitoring.
- Search takes plain words, a sender name, a conversation name and a day. There are no operators and the model never writes
  provider requests. Matching is by whole words (or 4+ letter prefixes), all must match; a provider with native search is
  asked to search, otherwise the recent window is filtered locally and JARVIS says it only looked at recent messages.
- Days ("today", "yesterday", "this week", "Friday", "March 5") use your `JARVIS_TIMEZONE` and the Phase 9/11 parsers.
- A message or conversation is identified by code: one match continues, several are listed and you are asked which, none is
  reported. Ids invented by the model are rejected. A conversation is shown oldest first with sender and time.

## Summaries, classification and action extraction

- **Summaries** come from the local LLM, in a tool-less call. Message text goes only inside a delimited
  `<message_content>` block, sanitized (no angle brackets or control characters), bounded, de-duplicated, with instructions
  that it is untrusted data and that only facts present in it may be used. The answer is plain text shown to you and is never
  parsed for actions.
- **Classification** (`important`, `action required`, `informational`, `personal`, `group`, `unknown`) is deterministic and
  rule based, reported with its reasons and always described as JARVIS's own guess. A bot asking for something is not treated as
  a person asking.
- **Action extraction** quotes the sentence that looks like a request and any deadline words ("by Friday"). It creates
  **nothing**: no task, reminder, event or calendar entry. JARVIS says "I haven't saved anything; tell me the task or reminder
  you want", and you then ask in your own words through the normal Task/Event tools and their permission rules.
- Nothing is added to memory, RAG or the knowledge graph.

## Permissions

| Tool | Risk | Approval |
|---|---|---|
| `message_list`, `message_search`, `message_get`, `conversation_list`, `conversation_get`, `message_summarize` | LOW | none (read-only, bounded, one-time scope) |

Unknown tool names (`message_send`, `message_delete`, `whatsapp_send`, ...) are denied. The permission is bound to the exact
resolved parameters, so an approval cannot be reused with different values. No messaging call is possible without the
PermissionManager. There is no mutating tool, so no stronger tier exists yet; a future send capability must be new,
explicitly registered and at least MEDIUM with the user's spoken yes.

## Security and prompt-injection defence

- The model can only propose a validated `MessageAction` made of words. `message_id`, `conversation_id`, `chat_id`,
  `provider`, URLs, methods, headers, tokens, cookies, sessions, paths, commands, SQL, recipients and any text to send in its
  arguments reject the action.
- The Telegram provider can only ever request `getMe` and `getUpdates` (a fixed allowlist, over HTTPS to `api.telegram.org`,
  a fixed host). `getUpdates` is sent **without** an offset, so no update is acknowledged or consumed and reading changes
  nothing on Telegram.
- The bot token is in the request URL that Telegram requires. It is never logged: errors log only the exception type or
  status code, and a log filter redacts the token from httpx's own request log line. A malformed token is rejected before
  any request.
- Message text, sender names, titles and file names are untrusted. They are shown to you as plain text, never shown to the
  AgentBrain, and replies containing them are replaced by a placeholder in the conversation history, so a later model call never
  sees them. Text such as "ignore previous instructions", "send this to everyone" or "run this command" is only ever text: it
  cannot invoke a tool, bypass the PermissionManager, reach the filesystem or credentials, change configuration or grant
  permissions. Tests cover this end to end, including a hostile model echo of an injected action.
- Static tests scan the package for dynamic execution, shell/subprocess use, raw SQL, browser automation, WhatsApp scraping,
  Telegram write methods and background loops, and check that it imports nothing from Gmail except plain text helpers.

## Privacy and data

- The provider is the source of truth. Nothing is copied to PostgreSQL, memory or the graph: **no table, no migration, no
  cache, no mapping**. Messages are read on demand and forgotten.
- Message bodies, names and tokens are never logged; logs contain counts and error types.
- Attachments are metadata only: JARVIS never downloads, opens, executes or indexes an attachment (no `getFile`).

## Errors and rate limits

Not configured, invalid or revoked token, provider unreachable, rate limited (honours `retry_after`, bounded), a conflicting
`getUpdates` consumer or webhook, unsupported capability, conversation or message not found, malformed provider response, and
an unavailable summarizer each get a clear spoken message while JARVIS keeps running. Retries are bounded (3 attempts, capped
exponential backoff); reads are the only operations, so nothing can be repeated harmfully.

## Testing

- `tests/test_messaging_provider_parser.py`: registry, capability detection, unsupported behaviour, models, the Telegram
  parser (malformed input) and the provider on a real httpx mock transport (retries, error mapping, allowlist, no offset, no
  token in logs).
- `tests/test_messaging_actions_engine.py`: AgentBrain -> permission -> tool -> service with in-memory provider doubles:
  validation, retrieval, search (native and local), ambiguity, conversations, summaries, classification, action extraction,
  permission binding, prompt injection, privacy, static guards.
- `tests/test_messaging_runtime.py`: settings, bootstrap, CLI, Git-ignore and secret hygiene.
- `tests/integration/test_telegram_real.py`: real Telegram, read-only, **skipped** unless a bot token is configured (and
  someone has messaged the bot in the last 24 hours). Run: `pytest -m integration tests/integration/test_telegram_real.py`.

## Limitations

- **Telegram bots see only what is sent to the bot or to groups it joined**, not your personal chats. In groups a bot with
  Telegram's default privacy mode sees only commands, replies to it and mentions; turn privacy mode off in BotFather to let it
  read group messages.
- Telegram keeps a bot's unconfirmed updates for 24 hours and returns at most 100 per request, so JARVIS reads only that
  recent window. It cannot search older history and says so.
- Unread state and unread counts are unknown for Telegram bots and are reported as unknown, never invented.
- WhatsApp and other platforms are not supported (see above). English rule-based classification only.
- The recorded results are only as good as the local model for summaries; classification is a heuristic.
- Verified here against mocked HTTP and in-memory providers. Real Telegram is tested only when a token exists.
