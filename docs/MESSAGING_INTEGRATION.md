# Messaging integration (hub)

The Phase 13 document (`docs/messaging-integration.md`) describes the provider layer. This page records what was **investigated** for Phase 18 and what is and is not supported.

## Supported

**Telegram — official Bot API, read-only.** JARVIS reads messages sent to a bot **you create** with BotFather and to groups the bot was added to. Capabilities detected from the provider interfaces: conversations, messages (recent window), no native search (JARVIS filters locally). No send/edit/delete capability exists anywhere in the code.

Through the hub: `MessagingAdapter` (search, fetch, incremental sync by message time), normalized `MESSAGE` items with sender name, conversation, unread flag, attachment names, injection flag; dates found in messages become `EVENT`/`DEADLINE` items (deterministic extractor, evidence sentence, confidence) that reach the context engine with provenance `message`; "Search my messages for …" / "Check my latest messages". Message text is untrusted data.

## Not supported — and why (not faked)

| Platform | Reality | JARVIS |
|---|---|---|
| **WhatsApp (personal account)** | Meta provides **no** API to read a personal account's chats. Scraping WhatsApp Web (browser automation / unofficial libraries) violates its terms, breaks without notice and risks the account. | Not built. "Is WhatsApp connected?" answers with this explanation. Only the WhatsApp **Business Cloud API** (for a business number you own, via Meta approval) is a legitimate route; the provider abstraction is where it would be added. |
| Personal Telegram chats | Bots cannot read them (a user-account "userbot" would be unofficial automation). | Not built. |
| Signal | No API for reading messages. | Not built. |
| iMessage | No supported API. | Not built. |

No scraping dependency exists anywhere in the architecture (a test pins the messaging package contents to the official provider only).

## Requires manual configuration

Create the bot with @BotFather, put the token in `MESSAGING_TELEGRAM_BOT_TOKEN` or the token file, set `JARVIS_MESSAGING_ENABLED=true`, message your bot. Not executed here (no bot).

## Privacy

Switch it off from the dashboard, by voice ("turn off messaging"), or with `JARVIS_MESSAGING_ENABLED=false`: the provider then reports "not set up" and nothing is read. Disconnect can also purge stored message items.
