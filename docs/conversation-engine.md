# Conversation Engine (Phase 3)

Phase 3 upgrades the one-shot voice interaction into a multi-turn
conversation: follow-up questions are answered using the earlier turns.
It adds conversation/session context only. There is no memory, agent,
tool use or integration.

## Architecture

```
Windows runtime (Phase 2)
      v
VoiceEngine            microphone, wake word, STT, TTS, voice state
      v  (plain text in, plain text out)
ConversationEngine     session, history, context building, timeout, reset
      v  (list of Message)
LLMProvider            common interface: chat(messages)
      v
OllamaProvider         Ollama /api/chat
```

| Module | Responsibility |
|--------|----------------|
| `backend/core/llm/messages.py` | `Role` (system/user/assistant) and `Message` (role, content, timestamp): provider-neutral |
| `backend/core/llm/base.py` | `LLMProvider.chat(messages)` (abstract); `generate(prompt, system)` is a single-turn wrapper over it |
| `backend/core/llm/ollama_provider.py` | Adapts `chat` to Ollama's `/api/chat` |
| `backend/core/conversation/models.py` | `ConversationSession`, `SessionState` |
| `backend/core/conversation/engine.py` | `ConversationEngine` |
| `backend/core/conversation/prompts.py` | The centralized `SYSTEM_PROMPT` |
| `voice/engine.py` | Multi-turn voice flow (calls `ConversationEngine.respond`) |

`ConversationEngine` never touches audio and knows nothing about Ollama;
`VoiceEngine` never holds history. `voice/bootstrap.py` wires them together.

## Message model

`Message(role, content, timestamp)`, immutable. Roles: `system`, `user`,
`assistant`. The system prompt is added when a request is built and is not
stored in the session history.

## Session and lifecycle

A `ConversationSession` has `session_id` (random UUID4, no user-identifying
data), `created_at`, `last_activity`, `messages` and a state (`active` /
`ended`).

```
IDLE (no session) --first successful turn--> ACTIVE --turn--> ACTIVE
ACTIVE --inactivity timeout / reset()--> ENDED (history cleared) --next turn--> new ACTIVE session
```

- A session is created only when its first turn succeeds.
- Each successful turn updates `last_activity`.
- If more than `JARVIS_CONVERSATION_TIMEOUT_SECONDS` pass without a turn, the
  session ends (checked lazily when it is next read or used) and the next turn
  starts a new one with a new id and no history.
- `ConversationEngine.reset()` ends the session immediately and discards its
  history. It touches nothing else (no config, no VoiceEngine, nothing
  persisted). It is safe to call repeatedly or with no session. Nothing in
  Phase 3 calls it from the UI; it is the API for later phases.
- Independent `ConversationEngine` instances have independent sessions.

## Context construction and window policy

Each request is `[system prompt] + recent history + current user message`.
History is bounded by `JARVIS_MAX_CONVERSATION_MESSAGES` (default 20,
minimum 2), a message count rather than a token count (exact token counting
would need a model-specific tokenizer; a count is simple and predictable):

- the newest messages are kept, older ones are dropped, both in the request and
  in the stored session, so memory stays bounded;
- the window always ends with the current user message and always starts on a
  user message, so an assistant reply whose question was trimmed away is
  dropped rather than sent without context;
- the current user message and the latest assistant reply are never dropped.

Limitation: long individual messages are not size-limited, so a tight context
window on a small local model can still be exceeded by very long replies.

## Errors

`ConversationEngine.respond` changes nothing until the LLM has answered. If
the LLM fails (`LLMProviderError`), no assistant message is added, the failed
user message is not kept (so history never contains an unanswered turn), an
existing session is kept as it was, and the next turn proceeds normally. The
error is raised to the caller; `VoiceEngine` ends that activation and the
Phase 2 runtime records it as `last_error` and keeps listening.

## Voice flow

```
Wake word -> "Yes?" -> capture -> transcribe -> ConversationEngine.respond -> speak
   -> (conversation still active?) yes: capture next utterance, no wake word needed
   -> silence (nothing transcribed) or session not active: activation ends -> WAITING
```

- Voice states are unchanged (WAITING/LISTENING/TRANSCRIBING/THINKING/SPEAKING);
  LISTENING..SPEAKING now repeat per turn before returning to WAITING.
- The microphone is closed while JARVIS thinks and speaks, so you cannot
  interrupt it mid-answer (no barge-in in this phase).
- A follow-up window is the same fixed `AUDIO_LISTEN_SECONDS` capture as before.
  If you say nothing, the activation ends. The *session* survives until the
  timeout, so saying "Hey JARVIS" again within `JARVIS_CONVERSATION_TIMEOUT_SECONDS`
  continues the same conversation with its context.
- Silence must transcribe to an empty string for this to work, so the
  Faster-Whisper provider now enables its built-in voice-activity filter
  (`vad_filter=True`), which stops Whisper inventing text for silence.
- Pausing/stopping from the Phase 2 tray is honoured between turns.
- Restarting the runtime rebuilds the engine, which starts with no session;
  pause/resume keeps it.

## Privacy

- History exists only in process memory: never written to a file, PostgreSQL,
  or any log; never sent anywhere except to the configured local LLM as the
  request context. No analytics.
- Logs record session ids, message counts and text lengths, not what was said.
  (Phase 3 also removed the spoken text from the `TTS_STARTED` log line.)
- Ending a session clears its history; closing JARVIS loses it. Durable memory
  is a later phase.

## LLM integration

`LLMProvider.chat(messages)` is the abstract method; every provider implements
it. `generate(prompt, system=None)` remains available on all providers as a
one-message convenience. `OllamaProvider` now calls `/api/chat` (it previously
used `/api/generate`) and needs a chat-capable model.

The system prompt (`backend/core/conversation/prompts.py`) gives JARVIS its
identity, keeps replies short for speech, tells it to use earlier turns for
follow-ups, and, as in Phase 1, tells it to say plainly that it has no access
to email, calendar, messages, files, tasks or personal memory. This is a
prompt-level safeguard, not a guarantee.

## Configuration

Existing `Settings` class; `.env.example` has both:

| Setting | Default | Meaning |
|---------|---------|---------|
| `JARVIS_CONVERSATION_TIMEOUT_SECONDS` | `120` | Inactivity after which a session ends (must be > 0) |
| `JARVIS_MAX_CONVERSATION_MESSAGES` | `20` | Recent messages kept as context (minimum 2) |

## Testing

Automated (`pytest`, fake LLM + fake clock; deterministic):
`tests/test_conversation_engine.py` (session creation, UUIDs, message order,
context construction, follow-ups for "What is Python?/Who created it?" and
"Bengaluru/its population" checking what the LLM *received*, history limiting,
timeout, reset, failed-LLM handling, independent sessions, provider
compatibility), `tests/test_voice_engine.py` (multi-turn voice flow with fake
providers), `tests/test_ollama_provider.py` (request shape with mocked HTTP).
These prove the engine passes the right history to the LLM; they say nothing
about how well a real model uses it.

Integration (`tests/integration`, self-skipping): a real Ollama two-turn
conversation (`test_real_ollama_multi_turn_conversation_in_one_session`). It
runs only when Ollama is reachable and the configured model is pulled.

Manual: with Ollama running, run `python -m desktop.launcher` (or
`python scripts/run_voice.py`), say "Hey JARVIS", "What is the capital of
France?", then, without the wake word, "How far is it from London?"; then stay
silent and confirm JARVIS returns to waiting.

## Limitations

- In-memory only; lost on exit or runtime restart.
- Follow-up listening is a fixed window (no live end-of-speech detection), and
  there is no barge-in.
- Message-count context limit, not token-based.
- The follow-up flow has been verified with fakes and with real STT/TTS
  components individually, not yet by a person speaking through the whole loop
  against a real LLM.
