# LLM provider (Groq)

JARVIS talks to its language model only through the `LLMProvider` abstraction (`backend/core/llm/base.py`: `chat(messages, json_mode) -> str`). The default provider is **Groq** (`GroqProvider`, OpenAI-compatible API). Ollama remains available as an alternative behind the same interface; nothing else depends on it and it is not a dependency.

## Setup
1. Create a key at https://console.groq.com/keys.
2. Put it **only** in your local `.env` (git-ignored): `GROQ_API_KEY=<your key>`. Never in `.env.example`, code, tests, docs, logs or screenshots.
3. `.env` (see `.env.example`):

```
LLM_PROVIDER=groq
LLM_MODEL=openai/gpt-oss-20b
GROQ_BASE_URL=https://api.groq.com/openai/v1
GROQ_API_KEY=
```

4. Verify: `python scripts/llm_real_check.py` (staged health, multi-turn chat, JSON mode; prints NOT VERIFIED and exits 2 if no key is set; never prints the key).

| Setting | Default | Meaning |
|---|---|---|
| `LLM_PROVIDER` | `groq` | `groq` or `ollama` (switching providers is only this setting) |
| `LLM_MODEL` | `openai/gpt-oss-20b` | model id (any chat model your key can use, e.g. `llama-3.3-70b-versatile`) |
| `GROQ_API_KEY` | empty | secret (`SecretStr`: hidden from reprs/logs; redaction also masks `gsk_…` strings) |
| `GROQ_BASE_URL` | `https://api.groq.com/openai/v1` | |
| `GROQ_REASONING_EFFORT` | `low` | gpt-oss models only; low keeps voice replies fast |
| `LLM_TEMPERATURE` / `LLM_MAX_TOKENS` / `LLM_TIMEOUT_SECONDS` / `LLM_MAX_RETRIES` | 0.3 / 2048 / 30 / 2 | reasoning tokens count toward the limit; raise `LLM_MAX_TOKENS` if replies are cut off |
| `OLLAMA_BASE_URL` | `http://localhost:11434` | only for `LLM_PROVIDER=ollama` |

## Behaviour
- System/user/assistant messages and the full multi-turn history are sent as-is; the reply text is returned in the same shape every provider returns.
- **Structured output:** the agent brain still asks for JSON (`response_format: json_object`) and still validates every reply against its schema (bounded retries, then a safe fallback). If a model rejects `response_format` the provider retries once without it — validation is the safety net; model JSON is never trusted.
- **Tools:** unchanged. The model only *proposes* a decision; execution still goes Tool Router → Permission Manager → tool → verification, and an unknown or malformed action is refused. The provider has no tool-calling side channel.
- **Time/date:** "What time is it?" / "What's the date?" are answered from the clock by the intelligence router (`agent/intelligence/router.py`), not by the model.
- **Errors** are classified (`LLMProviderError.kind`): `config` (key missing), `auth`, `network`, `timeout`, `rate_limit`, `model`, `bad_request`, `server`, `bad_response`. Rate limits, 5xx and transient network errors are retried with backoff (`Retry-After` respected, capped at 5 s); auth/model/config problems never are. Nothing is fabricated: an error leaves the conversation unchanged and the voice runtime keeps listening (`last_error` = `LLM provider error [kind]: …`, distinct from microphone/STT/wake-word/TTS errors).
- Requests are bounded by `LLM_TIMEOUT_SECONDS`; an in-flight HTTP call is not interrupted mid-flight by "stop".

## Health
`GET /llm` (token + loopback, like every dashboard route) and the `llm` service in the health table report, in stages: provider configured → API key configured → provider reachable → authentication → model available → inference. The default check uses `GET /models` (no tokens spent); `GET /llm?probe=true` and `scripts/llm_real_check.py` also run one tiny real chat. Example: `Groq · openai/gpt-oss-20b · reachable · authenticated · model available (inference not probed)`. Failures read `Groq API key missing`, `Groq authentication failed`, `Groq network unavailable`, `Groq model unavailable`, `Groq rate limit reached`, `Groq request timed out`. Startup logs `llm=groq:openai/gpt-oss-20b` (never the key).

## Troubleshooting
| Symptom | Fix |
|---|---|
| `llm failed · Groq API key missing` | add `GROQ_API_KEY` to `.env`, restart |
| `Groq authentication failed` | key wrong/revoked: create a new one |
| `Groq model unavailable` | `LLM_MODEL` is not offered to your key; check `/llm` or console.groq.com |
| `Groq rate limit reached` | free-tier limits: wait or pick a smaller model; retries are automatic |
| `network unavailable` / `timed out` | connectivity/firewall; deterministic features (tasks, briefings, workflows, time) keep working offline |
| replies empty or cut off | raise `LLM_MAX_TOKENS` |

## Security rules
The key is read from settings only, sent only in the `Authorization` header, and never included in errors, health output, logs, reprs or docs. `scripts/secret_scan.py` and a test scan for `gsk_…` keys in source and docs; `.env` is git-ignored and `.env.example` holds a blank placeholder. Prompts contain personal context, so they are sent to Groq: use `LLM_PROVIDER=ollama` (local) or `JARVIS_OFFLINE_MODE=true` if that is not acceptable. The deterministic layers (intelligence, workflows, autonomy) make no model call.
