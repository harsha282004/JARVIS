# Voice: wake phrases, sleep, and the ON/OFF switch

This is the user-facing reference for the voice pipeline's ON/OFF switch, sleep behavior and wake phrases. For the
pipeline's internals (VAD, wake-word gate, STT/TTS providers) see `docs/VOICE_ARCHITECTURE.md` and `docs/voice-system.md`.

## The ON/OFF switch

JARVIS's voice has one switch, shared by the tray, the dashboard and the API (`voice.switch.VoiceSwitch`):

- **ON** — the microphone is open, wake-word detection, VAD and speech recognition are running, and JARVIS is in
  `VOICE_SLEEPING` (waiting for "Hey JARVIS" / "JARVIS") whenever nothing else is happening.
- **OFF** — the microphone is released and nothing listens: no wake word, no VAD, no STT. The rest of the application
  (dashboard, integrations, reminders) keeps running.

Turn it on/off from:
- the tray menu: **JARVIS — ON/OFF** (top item, click to toggle);
- the dashboard's Voice panel: the **JARVIS: ON/OFF** button;
- the API: `POST /api/voice/enable`, `POST /api/voice/disable` (idempotent — calling either twice changes nothing further).

All three call the same switch, so they can never disagree; `GET /api/voice` reports `enabled` and `mode` from the same
source the tray reads.

The preference is persisted (`voice_enabled` in `.jarvis/voice_settings.json`) and survives a restart: if you last left
JARVIS OFF, it starts with the tray/dashboard running but the microphone closed. If you left it ON, it starts in
`VOICE_SLEEPING` (never mid-conversation).

A privacy mode (Paused/Private) can also hold the microphone closed independently of this switch; both must allow the
microphone for it to actually open. Turning the switch OFF while PRIVATE, then back ON, leaves the microphone closed
until privacy allows it again; turning it back OFF while privacy is blocking it is remembered too, so privacy returning
to normal does not silently reopen a microphone the user just turned off.

## Voice states

| State | Meaning |
|---|---|
| `VOICE_OFF` | the switch is off (or privacy blocks the microphone): nothing is listening |
| `VOICE_SLEEPING` | enabled, microphone open, waiting for a wake phrase |
| `VOICE_LISTENING` | a conversation is open (wake heard, or a follow-up window) |
| `VOICE_PROCESSING` | speech recognition or the agent is working on what you said |
| `VOICE_SPEAKING` | JARVIS is talking |

`GET /api/voice` reports the current state as `mode` (`OFF`/`SLEEPING`/`LISTENING`/`PROCESSING`/`SPEAKING`).

## Wake phrases

Only two phrases activate JARVIS, case-insensitively and independent of punctuation: **"Hey JARVIS"** and **"JARVIS"**.
"Hey, Jarvis." and "JARVIS?" both normalize to a valid phrase; "hey", "hello jarvisian", "computer" and sentences that
merely *contain* the word ("the jarvis project") do not. A candidate wake score is only accepted after a short local
speech check confirms one of the two exact phrases — this is what stops JARVIS's own voice, "Okay Jarvis" or background
chatter from waking it by accident.

## Acknowledgement

A valid wake phrase gets exactly one spoken **"Yes?"**. Saying the wake phrase again *while a conversation is already
open* (a follow-up, or just checking JARVIS is listening) also gets a fresh "Yes?" rather than being silently ignored —
see the "no response" entry in `docs/TROUBLESHOOTING.md` for why this used to fail.

## Sleep (the 2-minute timeout)

Once JARVIS wakes, follow-up questions need no wake word for `VOICE_SESSION_TIMEOUT_SECONDS` (default 120 s) of
inactivity, measured from the last thing you actually said (ambient noise and empty/low-confidence transcripts do not
reset it). When it expires, JARVIS returns to `VOICE_SLEEPING` silently — no announcement, nothing spoken — and a fresh
wake phrase is needed again. Saying "JARVIS sleep" / "go to sleep" ends the conversation immediately instead of waiting
for the timeout.

Sleep never turns the voice OFF: the switch stays ON, the microphone stays open, and "Hey JARVIS" wakes it again at any
time.

## CPU / resource behavior

`VOICE_OFF` is a real shutdown of the pipeline (not a flag the audio loop ignores): the microphone stream, wake-word
model, VAD and STT are not running at all, so CPU usage in OFF is the same as the rest of the application idling.
`VOICE_SLEEPING` runs the microphone stream, VAD and wake-word model continuously (STT only runs on a wake candidate),
so it costs more than OFF but far less than an active conversation, which also runs STT and the agent. Speaking adds
TTS synthesis and playback on top. Approximate figures depend on the machine; measure with
`python scripts/e2e_launcher_check.py` (it reports timing) or Task Manager while toggling the switch.

## TTS volume and the "Yes?" cold start

Piper's own output already peaks near 0 dBFS for every utterance, including "Yes?" (measured — see
`docs/GROQ_RATE_LIMITS.md` for the equivalent LLM-side measurement writeup). A short acknowledgement sounding quiet
is much more often a **device cold start**: the very first time anything is played through the speaker in a fresh
process, opening that output stream has its own startup latency, which a 150 ms "Yes?" can lose most of while a
five-second sentence barely notices. `voice/audio.py`'s `AudioOutput.warm_up()` plays a brief, near-silent clip as
soon as the microphone is acquired (well before any real speech), so that cost is paid silently ahead of time.

`Voice Settings -> TTS volume` (`VOICE_TTS_VOLUME`, 0–2) is a real amplifier above 1.0, not just attenuation: it
soft-clips (never a harsh digital clip) so raising it is safe, but since Piper is already near full scale, only raise
it if things genuinely still sound quiet through your actual speakers after the warm-up fix.

## Natural-language Gmail questions and follow-ups

JARVIS routes Gmail questions ("what's my last email?", "did I get anything about internships?", "read the newest
one") through the same LLM-driven agent as everything else, not a keyword list — the model produces a structured
Gmail action (`gmail_get_message`, `gmail_search`, `gmail_summarize`, ...) that the tool layer executes. Once an
email has been discussed, a follow-up in the same conversation ("What was it about?", "Who sent it?", "Was there an
attachment?") refers back to that same email automatically; only its id is remembered (never its content, which
still never re-enters the conversation history — see `docs/security.md`), and each follow-up re-fetches the message
fresh from Gmail rather than reusing anything cached.

## Troubleshooting: "I say Hey JARVIS and nothing happens"

See `docs/TROUBLESHOOTING.md`. In short: check `GET /api/voice` (`enabled`, `mode`, `microphone`, `wake_word.ready`) and
`.jarvis/voice_log.jsonl` for `wake`/`wake_rejected` lines before assuming the wake word itself is broken — a common
cause is the voice switch being OFF, not a detection failure.
