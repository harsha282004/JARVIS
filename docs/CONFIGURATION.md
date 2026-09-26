# Configuration (voice, Phase 19)

Two layers. **`.env`** holds the defaults (read at start, validated: an invalid value stops start-up with exit code 2 and a clear message). **`.jarvis/voice_settings.json`** holds what you change at run time (tray, dashboard, `POST /voice/settings`); it wins after a restart and is applied live where possible. Other settings are documented in `.env.example`, `docs/voice-system.md` and the earlier phase documents.

## `.env` defaults

| Variable | Default | Meaning |
|---|---|---|
| `WAKE_WORD_ENABLED` / `WAKE_WORD_PROVIDER` / `WAKE_WORD_MODEL_PATH` | true / openwakeword / `models/wakeword/hey_jarvis_v0.1.onnx` | the wake word model |
| `WAKE_WORD_THRESHOLD` | 0.5 | sensitivity: score needed. Lower = more sensitive (more false activations), higher = fewer |
| `MICROPHONE_DEVICE` | "" | empty = system default; an index or name |
| `STT_MODEL`, `STT_LANGUAGE`, `STT_DEVICE` | base, en, cpu | Faster-Whisper |
| `TTS_MODEL_PATH`, `TTS_VOICE` | `models/tts/en_US-lessac-medium.onnx` | Piper voice |
| `VOICE_USE_VAD` | true | end of speech by silence; `false` = the original fixed `AUDIO_LISTEN_SECONDS` window |
| `VOICE_SILENCE_SECONDS` | 1.0 (0.3–5) | silence that ends an utterance |
| `VOICE_MAX_UTTERANCE_SECONDS` | 15 (2–60) | hard cap |
| `VOICE_SPEECH_THRESHOLD` | 0.015 (0.001–0.5) | microphone RMS (0–1 of full scale) that counts as speech; raise it in a noisy room |
| `VOICE_CONVERSATION_TIMEOUT_SECONDS` | 20 (3–600) | how long follow-ups need no wake word |
| `VOICE_TTS_SPEED` | 1.0 (0.5–2) | 1.25 = 25 % faster |
| `VOICE_TTS_VOLUME` | 1.0 (0–1) | software volume |
| `VOICE_SPOKEN_MAX_CHARS` | 320 (80–2000) | longer answers are summarised aloud |
| `VOICE_DND_ALLOW_CRITICAL` | true | a critical alert may pass Do Not Disturb |
| `JARVIS_CONVERSATION_TIMEOUT_SECONDS` | 120 | conversation *history* timeout (separate from the listening window above) |
| `JARVIS_REMINDER_VOICE_NOTIFICATIONS` | true | reminders may be spoken |

## Persisted voice settings (`voice_settings.json`)

`wake_word`, `wake_sensitivity`, `microphone`, `stt_model`, `stt_language`, `stt_min_confidence` (0.35: below this a yes/no cannot answer a pending confirmation), `tts_voice`, `tts_speed`, `tts_volume`, `silence_seconds`, `max_utterance_seconds`, `min_utterance_seconds` (0.15), `speech_threshold`, `conversation_timeout_seconds`, `spoken_max_chars`, `voice_muted`, `voice_notifications`, `dnd_enabled`, `dnd_schedule_enabled`, `dnd_start` (`HH:MM`), `dnd_end`, `dnd_allow_critical`, `barge_in` (`wake_word` | `vad` | `off`), `barge_in_threshold`.

Applied live: wake sensitivity, TTS speed, volume, VAD threshold, silence/timeouts (read per utterance), mute, DND, notifications. A change of `microphone`, `stt_model`, `tts_voice` or wake word needs "Restart JARVIS" (tray) because those load at engine build.

Rules: every value is type-checked and clamped to a safe range; an invalid value or an unknown key changes nothing (HTTP 400); the file is written atomically; a corrupt file is moved to `.corrupt` and defaults are used. It never holds secrets or audio.

## Where to change things

* Tray: Mute voice, Voice notifications, Do Not Disturb, Stop speaking, Pause/Resume listening, Private mode, Open dashboard, Settings (opens `.env`).
* Dashboard → Voice: the same toggles plus wake sensitivity, speed, volume, silence length.
* API (token required, loopback only): `GET /voice`, `POST /voice/settings`, `POST /voice/interrupt`, `POST /voice/activate`, `GET /voice/log`.

## Tuning

| Symptom | Change |
|---|---|
| activates on TV/conversation | raise `wake_sensitivity` (0.6–0.8); check the dashboard "no speech" counter |
| doesn't wake | lower it (0.35–0.45); check the microphone level with `python scripts/voice_real_check.py --mic` |
| cuts you off mid-sentence | raise `silence_seconds` (1.5) |
| waits too long after you finish | lower `silence_seconds` (0.7) |
| noisy room never ends an utterance | raise `speech_threshold` (0.03–0.05) |
| answers too long to hear | lower `spoken_max_chars`; full text is on the dashboard |
| speakers echo into the microphone | keep `barge_in=wake_word`, or use a headset for `vad` |


# Browser agent (Phase 20)

All in `.env` (see `.env.example`); the browser is never started at launch, only on request.

| Variable | Default | Meaning |
|---|---|---|
| `BROWSER_ENABLED` | true | build the browser agent (tools, router, dashboard panel) |
| `BROWSER_TYPE` | auto | `auto` = installed Edge, then Chrome, then Playwright's Chromium; or `msedge`, `chrome`, `chromium` |
| `BROWSER_HEADLESS` | false | false = a visible window (you watch it work); true = invisible |
| `BROWSER_PROFILE_DIR` | `.jarvis/browser_profile` | a profile dedicated to JARVIS (sign-ins you make persist here); never your everyday profile |
| `BROWSER_DEFAULT_TIMEOUT_SECONDS` | 10 | element waits, clicks |
| `BROWSER_NAVIGATION_TIMEOUT_SECONDS` | 20 | page loads |
| `BROWSER_MAX_TABS` | 8 | more sites navigate in place instead of opening tabs |
| `BROWSER_RETRIES` | 2 | extra attempts for SAFE operations only (loading, reading, finding) |
| `BROWSER_DOWNLOAD_DIR` | `.jarvis/downloads` | the only place downloads are saved |
| `BROWSER_UPLOAD_DIR` | `.jarvis/uploads` | the only folder a file may be uploaded from |
| `BROWSER_SCREENSHOT_MODE` | memory | `off`, `memory` (measured then discarded), `disk` (`.jarvis/screenshots`) |
| `BROWSER_ALLOW_PRIVATE_HOSTS` | false | allow localhost/LAN addresses (off by default: SSRF protection) |
| `BROWSER_SEARCH_URL` | Bing | search template with `{query}` |

Nothing here holds credentials. Runtime controls: tray (Open browser, Close browser, Stop browser action), dashboard Browser panel, `GET /browser`, `POST /browser/open|close|stop`, `GET /browser/log`. `python scripts/browser_real_check.py` exercises the real browser and YouTube and prints what happened.

# Autonomous agent (Phase 21)

| Variable | Default | Meaning |
|---|---|---|
| `AUTONOMY_ENABLED` | true | plan and run multi-step goals |
| `AUTONOMY_MAX_DURATION_SECONDS` | 180 | wall-clock limit per task |
| `AUTONOMY_MAX_STEPS` | 25 | steps executed (retries and replans count) and the maximum plan length |
| `AUTONOMY_MAX_TOOL_CALLS` | 40 | tool calls per task |
| `AUTONOMY_MAX_RETRIES` | 2 | retries per safe step (task-wide cap is twice this) |
| `AUTONOMY_MAX_REPLANS` | 3 | fallbacks/replans per task |
| `AUTONOMY_LOOP_THRESHOLD` | 3 | the same action with the same page state this many times = stop |
| `AUTONOMY_MAX_CONSECUTIVE_FAILURES` | 3 | failed steps in a row before stopping |
| `AUTONOMY_OBSERVATION_TIMEOUT_SECONDS` | 10 | how long to wait for media state to settle before verifying |
| `AUTONOMY_CONFIRMATION_TIMEOUT_SECONDS` | 120 | how long a question or confirmation waits before the task is `BLOCKED` |
| `AUTONOMY_BROWSER_TASK_TIMEOUT_SECONDS` | 60 | a step that already used this much time is not retried |
| `AUTONOMY_INLINE_WAIT_SECONDS` | 25 | a spoken request waits this long for a quick task before "I'm working on it" |
| `AUTONOMY_VOICE_PROGRESS` | true | short progress updates |
| `AUTONOMY_HISTORY_SIZE` | 30 | recent tasks kept (redacted summaries in `.jarvis/autonomy_history.json`) |

Browser limits that also apply to tasks: `BROWSER_MAX_TABS`, `BROWSER_RETRIES`, the browser timeouts. Runtime controls: dashboard *Autonomous task*, tray (Pause/Resume/Stop task), `GET /tasks`, `POST /tasks/pause|resume|cancel`, voice.

# Personal Operator (Phase 22)

| Setting | Default | Meaning |
|---|---|---|
| `WORKFLOWS_ENABLED` | `true` | the switch; off = no operator, no `/workflows`, routers unchanged |
| `WORKFLOW_MAX_CONCURRENT` | `2` (1–6) | running workflows at once (at most three times as many unfinished ones) |
| `WORKFLOW_MAX_DURATION_SECONDS` | `240` | wall-clock limit per workflow |
| `WORKFLOW_MAX_STEPS` | `14` | steps per plan (validator) |
| `WORKFLOW_MAX_TOOL_CALLS` | `30` | tool calls per workflow, retries included |
| `WORKFLOW_MAX_RETRIES` | `2` (0–5) | retries of a *read* after a temporary failure; writes are never retried |
| `WORKFLOW_MAX_SYSTEMS` | `5` | integrations one workflow may touch |
| `WORKFLOW_CONFIRMATION_TIMEOUT_SECONDS` | `120` | how long a question waits; on timeout nothing is done |
| `WORKFLOW_INLINE_WAIT_SECONDS` | `12` | how long a spoken request waits for a quick workflow before "I'll tell you when I'm done" |
| `WORKFLOW_HISTORY_SIZE` | `12` | finished workflows shown on the dashboard |
| `WORKFLOW_PROACTIVE_ENABLED` | `true` | proactive intelligence may start suggestion-only (read-only) workflows |

State lives in `<state>/workflows/` (checkpoints, effect ledger, links, history, redacted audit log). The workflows use the existing integration switches and permissions (`JARVIS_GMAIL_ENABLED`, `JARVIS_CALENDAR_ENABLED`, the hub's per-integration enable/permission state, `JARVIS_MEMORY_ENABLED`, `AUTONOMY_*`/`BROWSER_*` for browser steps); they add no credentials of their own.

# Microphone selection (device manager)

`MICROPHONE_DEVICE` — empty or `auto`: the Windows default input, with the same physical microphone on other host APIs and other real microphones as fallbacks (virtual/stereo mixes, the sound mapper and output-only devices are never chosen automatically). A device **name** (case-insensitive, prefix/contains; stable across reboots — preferred) or a numeric sounddevice **index** (can change) selects explicitly; if it matches nothing JARVIS falls back to `auto`. `AUDIO_SAMPLE_RATE` (default 16000) is the pipeline rate; a device that cannot open at it is opened natively and resampled once. List devices: `python scripts/voice_real_check.py --devices`.

# Wake policy, session and voice (strict wake update)

| Setting | Default | Meaning |
|---|---|---|
| `WAKE_WORD_THRESHOLD` | 0.5 | model score that makes a wake candidate |
| `WAKE_DIRECT_THRESHOLD` / `WAKE_MIN_FRAMES` | 0.85 / 2 | a strong wake: this score on this many consecutive 80 ms frames |
| `WAKE_DIRECT_ACCEPT` | `false` | false (strict): even a strong wake needs the exact phrase from a short local speech check; true accepts strong wakes directly |
| `WAKE_STT_CONFIRM` | `true` | second-stage phrase check ("hey jarvis" / "jarvis" only) |
| `WAKE_CANDIDATE_FLOOR` | 0.3 | weaker sustained scores (>= 2 frames) become phrase-checked candidates |
| `WAKE_DEBOUNCE_SECONDS` | 1.5 | one wake event -> at most one activation |
| `VOICE_POST_TTS_WAKE_BLOCK_SECONDS` | 1.0 | JARVIS's own voice cannot wake it |
| `VOICE_SESSION_TIMEOUT_SECONDS` | 120 (3-3600) | inactivity that ends a conversation silently (old name `VOICE_CONVERSATION_TIMEOUT_SECONDS` still honoured) |
| `VOICE_SLEEP_COMMAND_ENABLED` | `true` | "JARVIS sleep" ends the session at once, without the language model |
| `TTS_MODEL_PATH` / `TTS_VOICE` | `models/tts/en_US-ryan-medium.onnx` / `en_US-ryan-medium` | the male Piper voice (must exist on disk; health reports it otherwise) |
