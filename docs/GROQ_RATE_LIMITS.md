# Groq rate limits and "I can't reach the language model"

If JARVIS occasionally says **"I'm being rate-limited by my language model right now"** (or, on an older build,
the generic "I can't reach my language model right now"), this page explains why, what was measured, and what
was changed.

## What was actually measured

Probing the real, deployed Groq key directly returned these response headers on an ordinary request:

```
x-ratelimit-limit-tokens: 8000        # tokens allowed per minute, this account/model
x-ratelimit-remaining-tokens: 7918    # left after one small request
x-ratelimit-limit-requests: 1000      # requests allowed per minute (not the bottleneck here)
```

**8000 tokens per minute** is a small budget. JARVIS's agent sends a large system prompt on every single
conversational turn — every tool JARVIS knows about (Gmail, calendar, tasks, reminders, messaging, briefing,
proactive notifications) is described in it so the model can route a request to the right one. That system prompt
alone runs several thousand tokens, and `openai/gpt-oss-20b` is a reasoning model that spends additional hidden
"reasoning" tokens on top of what it visibly answers with, even at `reasoning_effort=low`.

Reproducing five real conversational turns back to back (each one: one routing call plus, when applicable, one
answer call) reliably exhausted this budget and produced genuine `429 rate_limit` responses from Groq — this is
not a bug in wake-word detection, Gmail, or natural-language understanding; it is the account's real per-minute
token ceiling being reached by ordinary use.

## What changed because of this

1. **A structured (JSON-mode) routing call now requests far fewer tokens than a real spoken answer**
   (`LLM_JSON_MAX_TOKENS`, default 800, vs `LLM_MAX_TOKENS`, default 2048 — see `backend/core/llm/groq_provider.py`).
   A routing decision is a few dozen tokens of JSON; there was never a reason to let it request up to 2048. This
   leaves more of the per-minute budget for the part the user actually hears.
2. **A `retry-after` that legitimately reflects the rest of the minute is now honored up to 20 s**, not capped at
   5 s (`_MAX_RETRY_AFTER` in `groq_provider.py`). Retrying before a token-bucket 429 has actually reset only wastes
   the bounded retry count and fails again; a voice conversation's 120 s session timeout comfortably absorbs one
   longer pause instead of a guaranteed failure.
3. **Every `LLMProviderError.kind` gets its own honest spoken message** (`voice/engine.py`,
   `LLM_ERROR_MESSAGES`) — a rate limit, a timeout, an auth problem and a real outage are no longer all reported
   as the same "I can't reach the language model," which made a five-second hiccup indistinguishable from a broken
   API key.

## What this does not fix

No amount of retrying or token trimming can make a *sustained* burst of requests (several real conversations within
one minute) fit inside an 8000 TPM ceiling — that is the account's tier, not a defect. If rate limiting is still
frequent after this change:

- Check current usage: `python scripts/llm_real_check.py` prints the account's live rate-limit headers.
- Ask fewer, more specific questions per minute, or wait a few seconds between turns.
- If this is a sustained problem, Groq's paid tiers raise the tokens-per-minute ceiling substantially; that is an
  account change, not a code change.

## Related

- `docs/VOICE.md` — the voice pipeline and its error handling in general.
- `docs/TROUBLESHOOTING.md` — the "I can't reach the language model" row points here.
