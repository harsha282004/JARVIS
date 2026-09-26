# Voice architecture (Phase 19)

One voice pipeline. Phase 19 extended it; nothing was duplicated. `docs/voice-system.md` is the Phase 1 setup guide (model downloads, hardware); this document is the current architecture.

## 1. Flow: what existed, what it is now

| Stage | Before Phase 19 | Now |
|---|---|---|
| Microphone | `AudioInput` opened for the wake loop; a device error crashed the worker (then the Supervisor restarted it) | `VoiceEngine` owns open/retry: unplugged or missing device → `MICROPHONE_DISCONNECTED`, retry every 2 s, `MICROPHONE_CONNECTED` on return; permission errors → `MICROPHONE_PERMISSION_DENIED`; paused/private → `MICROPHONE_CLOSED`. Device list: `voice.audio.list_input_devices()` (names only) |
| Wake word | openWakeWord, fixed threshold | same model; **runtime-adjustable sensitivity**, `last_score`, `reset()` after each activation, 1 s refractory window, health ("noisy" after many activations with no speech) |
| Speech capture | fixed `AUDIO_LISTEN_SECONDS` window | **VAD** (`voice/vad.py`): waits for speech, ends after `silence_seconds` of quiet, `max_utterance_seconds` cap, `min_utterance_seconds` blip filter, pre-roll so the first syllable is kept. The fixed window remains only with `VOICE_USE_VAD=false` |
| STT | text only | `Transcription(text, confidence, audio_seconds, language)`; command-vocabulary hint (`initial_prompt`); failure recorded, never crashes the loop |
| Normalisation | none | `voice/normalize.py`: wake word, fillers, spoken punctuation, ellipses, self-corrections; **control words** (Stop / Cancel / Wait) classified before the agent |
| Conversation | `ConversationEngine` (+ Phase 17/18 routers) | unchanged entry point (`respond(text)`, the only call); adds `awaiting_answer()`, `cancel_pending()`, clarification answers, reminder corrections, follow-up context |
| Response | full text spoken | `voice/policy.py::spoken_version`: short answers whole, long answers cut at a sentence + "The full details are on your dashboard."; full text kept on `VoiceStatus` |
| TTS | one blocking synthesis + non-cancellable `sd.wait()` | sentence-by-sentence synthesis, cancellable playback (`AudioOutput.start/stop/is_playing`), speed (`PiperProvider.set_speed`), volume, no overlap (single thread), states `TTS_IDLE/GENERATING/SPEAKING/INTERRUPTED/ERROR` |
| Barge-in | none | while speaking, the microphone stays open: wake word ("Hey JARVIS, stop"), optional VAD mode (loud speech; use a headset), tray/dashboard "Stop speaking"; then the utterance is read: `Stop` is a control word, a new request is processed |
| Notifications | text queued, spoken between conversations | priorities `low/normal/high/critical`, Do Not Disturb, mute, switch; critical may interrupt a conversation |

```
Mic ──► AudioInput ──► wake word ─┐(or tray/dashboard "Talk to JARVIS")
                                  ▼
        VAD utterance detector ──► STT (confidence) ──► normalise / control words
                                  │                        │Stop/Cancel/Wait: handled here, never an agent task
                                  ▼
        low-confidence yes/no guard ──► ConversationEngine.respond(text)     ◄── the ONLY path to the agent
                                            │ clarification / correction / follow-up context / confirmations
                                            ▼
                     Agent ─► Tool Router ─► PermissionManager / ConfirmationEngine ─► Tool ─► verification
                                            ▼
        response policy (spoken summary) ──► sentence TTS ──► AudioOutput (cancellable) ──► speakers
                                                                   ▲ barge-in monitor reads the mic while playing
```

## 2. Components

| File | Role |
|---|---|
| `voice/engine.py` | the pipeline, states, mic lifecycle, barge-in, control handling, error recovery |
| `voice/vad.py` | `EnergyVAD` (adaptive noise floor) and `UtteranceDetector` |
| `voice/normalize.py` | `normalize()`, `control_of()` |
| `voice/policy.py` | `VoicePolicy` (DND/mute/priority), `spoken_version`, sentence splitting |
| `voice/settings.py` | `VoiceSettings` + `VoiceSettingsStore` (validated, clamped, persisted to `.jarvis/voice_settings.json`, live-applied) |
| `voice/status.py` | `VoiceStatus` (dashboard snapshot), `VoiceLog` (structured, redacted, no audio), vocabulary of mic/TTS/overall states |
| `voice/control.py` | `VoiceControl`: the one object the engine, API and tray share |
| `voice/audio.py` | `AudioInput`, `AudioOutput` (start/stop/volume), `list_input_devices` |
| `agent/tasks/notifications.py` | `AnnouncementQueue` with priorities, `VoiceNotifier` |
| `agent/intelligence/followups.py` | follow-up resolution over the last spoken list |
| `agent/tasks/executor.py`, `tools.py` | pending clarification, correction planning (through the same PermissionManager path) |

Threading: one voice thread runs everything audio (the RuntimeManager worker). Tray/API threads only set an `Event` (`interrupt()`, `request_activation()`) or read `VoiceStatus` under its lock. Nothing blocks the UI thread; models load on the worker.

## 3. States

* Overall: `IDLE`, `LISTENING`, `PROCESSING`, `SPEAKING`, `ERROR` (engine states waiting / listening / transcribing+thinking / speaking; `ERROR` when the microphone is unavailable).
* Microphone: `MICROPHONE_CONNECTED`, `MICROPHONE_DISCONNECTED`, `MICROPHONE_PERMISSION_DENIED`, `MICROPHONE_CLOSED`, `MICROPHONE_UNKNOWN` (engine not built yet).
* TTS: `TTS_IDLE`, `TTS_GENERATING`, `TTS_SPEAKING`, `TTS_INTERRUPTED`, `TTS_ERROR`.
* Provider readiness is `true` / `false` / `null` (unknown until the engine has been built): unknown is never shown as healthy.

## 4. Conversation behavior

**Active conversation.** After the wake word, each answer is followed by a listen window of `conversation_timeout_seconds` (default 20 s) with no wake word needed; silence ends the activation and returns to wake-word listening. The session history timeout (`JARVIS_CONVERSATION_TIMEOUT_SECONDS`, 120 s) is separate.

**Follow-ups, deterministic, no LLM.** `HubRouter` remembers the last schedule/email list it spoke for 4 minutes:
"What's my schedule today?" → "What meeting is first?" / "Which one is the longest?" / "How many?" / "And tomorrow?"; after an email list: "Who sent the first one?"; for a single item: "When is it?" / "How long is it?". "It" that is more than one thing is not guessed (it asks, or the normal path answers). With no recent context these sentences are not treated as follow-ups. Answers keep their provenance ("Where did you get that?").

**Clarification.** When a tool needs a value ("What time tomorrow?") the executor keeps the question (90 s, ≤3 attempts). A short reply ("Morning." → 9 AM, "6 PM.") completes the original request through the same resolve → PermissionManager → tool path. Anything that looks like a new request ("what time is it") drops the question and is handled normally. "Cancel"/"Never mind" also drops it. The dashboard shows "waiting for your confirmation/clarification".

**Corrections.** "Make that 7 PM", "No, I meant tomorrow" (normalised to `change that to …`) replace the reminder created in this session in the last 5 minutes: cancel + create in one permission-checked step, keeping the part the user did not change. Only a still-scheduled, non-recurring reminder is touched; a time already past is refused, not moved.

**Confirmations are not weakened.** Destructive/external actions still need the strict spoken yes (`ConfirmationEngine`, `TaskActionExecutor`). New: a yes/no with STT confidence below `stt_min_confidence` (0.35) cannot answer a pending confirmation ("I wasn't sure I heard that. Please say yes or no again."). Reminder creation stays a LOW-risk action as before; the spoken read-back ("Okay, I'll remind you today at 6:00 PM: study.") is its confirmation.

## 5. Interruption

| Input | Result |
|---|---|
| "Stop", "JARVIS stop", "That's enough", "Be quiet" (+ common mis-hearings such as "Top") | speech stops; never an agent request; conversation stays open |
| "Wait", "Hold on", "One moment" | "Sure, take your time."; listens 15 s |
| "Cancel", "Never mind", "Actually don't do that" | drops the pending confirmation/clarification: "Okay, cancelled." (nothing is executed) |
| "cancel my dentist appointment" (a longer sentence) | a real request: agent + confirmation as usual |
| tray "Stop speaking", `POST /voice/interrupt` | speech stops |

Interruption modes (`barge_in`): `wake_word` (default: the wake-word model listens while JARVIS talks), `vad` (any loud speech, needs a headset because the speaker's own voice reaches the microphone), `off`. A 0.3 s grace ignores the start of playback.

## 6. Notifications and Do Not Disturb

Sources (reminders → `high`, proactive alerts → their task priority, NotificationCenter levels LOW/NORMAL/IMPORTANT/CRITICAL → low/normal/high/critical) share one bounded `AnnouncementQueue` (critical first; a full queue drops the lowest, never a critical). `VoicePolicy.may_speak`:

| Setting | Effect |
|---|---|
| `voice_muted` | nothing is spoken (not even critical); text stays on the dashboard |
| `voice_notifications=false` | only critical is spoken |
| DND (manual, or scheduled `dnd_start`–`dnd_end`, crossing midnight supported) | non-critical held; critical spoken if `dnd_allow_critical` |
| mid-conversation | only critical may interrupt; the rest waits until the conversation ends |

Held announcements are not lost: they appear under "held back" on the dashboard (last 20), and the NotificationCenter still records every event and desktop popups are unaffected.

## 7. Failure behavior (all injected in tests)

| Failure | Behavior |
|---|---|
| microphone missing/unplugged (start or mid-run) | states above, retry every 2 s, spoken "I lost the microphone…" if it happens mid-conversation; health check DEGRADED |
| wake-word model missing | engine build fails with the clear provider error (unchanged); health DISABLED/FAILED |
| STT crashes | "I can't process speech right now. You can still use the dashboard."; `stt.ready=false`; loop continues |
| TTS / audio output fails | `TTS_ERROR`; the answer is kept as text on the dashboard; loop continues |
| LLM down | "I can't reach my language model right now. I can still help with reminders, your calendar and your email."; deterministic answers (schedule, follow-ups, hub) keep working |
| Gmail/Calendar/GitHub unavailable | the hub's classified message is spoken ("Gmail isn't connected yet."); nothing is invented |
| speaking while JARVIS talks | barge-in as above |
| empty/unintelligible speech | ends the activation quietly; counted as a false activation only for the wake word (not for a tray activation) |

## 8. Observability

`VoiceLog` (`.jarvis/voice_log.jsonl`, rotated) records: `ts, event, session_id, state, transcription, intent, tool, latency_ms, result` (+ confidence/duration for transcriptions). Secrets are redacted (`backend.core.redaction`); there is no audio field. Metrics (`/metrics`, `/voice`): `wake_to_prompt_ms`, `speech_detection_ms`, `stt_ms`, `conversation_ms` (agent incl. tools; `tool.<name>_ms` for hub tools), `tts_synthesis_ms`, `time_to_first_audio_ms`, `end_to_end_ms` (utterance end → response spoken).

## 9. Privacy

Audio lives in memory for one utterance and is never written, logged or sent anywhere except the local STT model. Tested: no file is created during a session; `voice/` contains no file-write, subprocess, shell or eval calls, and imports no integration, tool or security module (it reaches the agent only through `ConversationEngine.respond`).

## Phase 21: autonomous tasks and "Stop"

`ConversationEngine.cancel_task()` / `task_active()` expose the autonomy manager to the voice layer. A spoken **Stop** or **Cancel** now first cancels a running autonomous task (future actions do not run, the browser is asked to stop) and says "Okay, I stopped the task."; only then does it behave as before (silence speech / drop a pending question). Task answers arrive as normal conversation turns: a waiting question ("Which one do you mean?", "Shall I go ahead?") is answered by the next utterance, and confirmations use the same `ConfirmationEngine` and low-confidence guard as before. Task progress ("I found your repository.") and results are `announcements`: results at `high` priority, progress at `normal`, both subject to Do Not Disturb and mute; nothing is spoken per internal step. See `AUTONOMOUS_AGENT_ARCHITECTURE.md`.

## Phase 22: workflows by voice

Workflow requests, answers and controls arrive through the same path as every spoken command. "Stop", "Cancel this", "Never mind" and "Cancel the workflow" end a running workflow (and clear a question it was waiting on) — `IntelligenceRouter.cancel_task()` cancels workflows as well as autonomous tasks, and the cancel words are intercepted before the confirmation engine so they can never be mistaken for a "no" that lets the workflow continue. A spoken "yes" confirms through the same single-use, step-bound `ConfirmationEngine`; nothing about the voice path lowers a requirement. Results of long workflows are announced through the notification queue (priority and Do-Not-Disturb per Phase 19), deduplicated. The morning briefing wording is "Good morning. You have three meetings today, two high-priority emails, and one upcoming deadline." followed by the most pressing items with their reasons; "Tell me more" (within 30 minutes) speaks the reasons behind them.

## Microphone capture on Windows (device manager)

Backend: `sounddevice` / PortAudio (blocking `InputStream`). Windows exposes one physical microphone as several endpoints under four host APIs (MME, DirectSound, WASAPI, WDM-KS) with near-identical names and unstable indexes.

```
Windows input endpoint(s)
  -> voice.mic.MicrophoneDeviceManager   list (output-only removed) | default input | ordered candidates (config, default, same-named endpoints, other real mics; virtual mixes/sound mapper never)
  -> voice.audio.AudioInput.open()       per candidate: pipeline format (16 kHz mono) if accepted, else the device's native rate/channels
                                          + a 0.3 s probe: an endpoint that opens but is digitally silent (all zeros) is skipped for the next one
  -> to_pipeline()                       (native format only) channel mean + 3:1 average / linear resample -> int16 mono frames of 1280 samples @ 16 kHz
  -> wake word / VAD / STT               unchanged; the wake word and the utterance recorder read the SAME AudioInput
```

Nothing is hard-coded to an index: the default input is resolved on every `open()`, so a reconnect (the engine's existing microphone-error/retry path) re-discovers the device. WDM-KS endpoints (blocking API unsupported) are last resort. Logged metadata only (`Microphone: {mode, device, host_api, sample_rate, channels, input_channels, resampled, status}`); audio is never stored, logged or sent.

### Windows microphone troubleshooting
1. **List and test:** `python scripts/voice_real_check.py --devices` (lists, opens nothing) and `python scripts/voice_real_check.py --mic-only --mic --seconds 5` (speak during it). PASS = real samples received; FAIL = zeros or a noise floor of at most 4/32768; WARN = works but very quiet. Values are fractions of full scale (a quiet room is ~0.00002, so it looks like `0.0000` at four decimals — the old script printed exactly that; the raw integer peak is now shown).
2. **Default device:** Windows Settings > System > Sound > Input: pick the microphone, check the level bar moves. JARVIS follows the Windows default in `auto` mode.
3. **Privacy:** Settings > Privacy & security > Microphone: "Let desktop apps access your microphone" must be on (a blocked app gets exact zeros; JARVIS then reports `no_signal`).
4. **Pick a device explicitly:** `MICROPHONE_DEVICE=Microphone Array (Realtek(R) Audio)` (name; preferred) or an index; `auto` or empty = default.
5. **Very low level:** raise Input > Properties > Levels (and Microphone Boost); WASAPI shared mode cannot open 16 kHz, which is handled by the native-format path.
6. **Unplug/disable:** the engine reports its existing microphone error state and reopens when the device is back (no app restart).

## Wake policy, sleep and the male voice (strict wake update)

**Root cause of the spontaneous "Yes?"** (from the logs and the code): "Yes?" is spoken only after `_wait_for_wake` returns, and it returned on (1) any single 80 ms frame whose openWakeWord score reached the threshold (0.5) - ambient speech, TV, the speaker's own echo and other words that sound like "hey jarvis" score that high for one frame; the log shows `WAKE_WORD_DETECTED` followed by `No speech captured` several times; and (2) a stale tray/API "Talk to JARVIS" request: `request_activation()` set a flag that stayed set until the next time the engine listened, so a click made while paused (or an unconsumed API call) produced a "Yes?" later with nobody speaking. The barge-in monitor also used the same one-frame trigger while JARVIS was speaking.

**Supported wake phrases: exactly "Hey JARVIS" and "JARVIS"** (`voice/wake.py`; case, punctuation and spacing ignored; no fuzzy matching: "Okay Jarvis", "Yes Jarvis", "Hey Travis", "jarvice", "hey jarvis play music" do not wake it).

```
microphone frame -> openWakeWord score -> WakeGate
   blocked (debounce / after JARVIS spoke / after a rejection)              -> ignored
   rising score watched until it ends or becomes sustained (2 frames >= WAKE_DIRECT_THRESHOLD)
   candidate (>= WAKE_WORD_THRESHOLD, or >= 2 frames >= WAKE_CANDIDATE_FLOOR) or a strong wake
        -> short LOCAL speech check of the last ~2.4 s (memory only)  -> must normalise to exactly "hey jarvis" | "jarvis"
        -> WakeEvent (validated) -> "Yes?" -> conversation
   anything else -> nothing is spoken; wake_rejected is logged (score, reason, transcript LENGTH only)
```
With `WAKE_DIRECT_ACCEPT=false` (default) even a strong score needs the phrase check (the model fires on "Okay Jarvis" and "Yes Jarvis" too); `WAKE_STT_CONFIRM=false` removes the second stage (then only persistent scores count). If speech recognition is down, no wake happens (fail safe). The acknowledgement is only reachable through a `WakeEvent` (a test pins this); a fresh tray/API request is also a `WakeEvent` but expires after 5 s. Microphone opening, reconnecting, health checks, scheduler ticks, empty or unrelated transcripts cannot cause a "Yes?".

**Debounce and echo.** After an activation `WAKE_DEBOUNCE_SECONDS` (1.5 s) ignore further wake audio; after JARVIS speaks anything `VOICE_POST_TTS_WAKE_BLOCK_SECONDS` (1 s) are ignored; a rejected candidate starts a 2 s STT cooldown. While JARVIS is speaking, barge-in by wake word needs a strong sustained score (echo-level scores are ignored). Wake detection resumes normally afterwards.

**Session.** Wake -> "Yes?" -> conversation; follow-ups need no wake word until `VOICE_SESSION_TIMEOUT_SECONDS` (default 120) of inactivity, measured in **audio time since the last meaningful interaction** (it restarts when JARVIS finishes answering). Ambient noise, empty transcripts and low-confidence speech (below `stt_min_confidence`, unless it answers a pending yes/no) do not reset it. At the timeout the session ends silently (nothing is spoken) and the runtime is wake-only again. `session_state` (`asleep`/`active`), `sleep_reason` (`timeout`/`command`) and the last wake's metadata (source, score, threshold, phrase, STT confirmation, debounce) are on the voice status/dashboard; the voice log records `wake`, `wake_confirmed`, `wake_rejected`, `session_sleep`, `ambient_ignored`.

**Sleep command.** "JARVIS sleep", "Hey JARVIS, sleep", "go to sleep", "sleep" (only those; "sleep well tonight" is a normal sentence) end the session at once: any speech is interrupted, a pending question is dropped, JARVIS says "Going to sleep.", the conversation context is closed, and nothing is sent to the language model or any tool. `VOICE_SLEEP_COMMAND_ENABLED=false` turns it off. "Hey JARVIS sleep" heard while already wake-only is ignored silently.

**Male voice.** `TTS_MODEL_PATH=models/tts/en_US-ryan-medium.onnx`, `TTS_VOICE=en_US-ryan-medium` (Piper's official `ryan` male English voice, medium quality, 22.05 kHz). Install (project downloader, official piper-voices): `python -c "from pathlib import Path; from piper.download_voices import download_voice; download_voice('en_US-ryan-medium', Path('models/tts'))"`. The `tts` health line names the voice actually on disk (read from the model's own `.onnx.json` dataset) and reports `FAILED ... not found: download en_US-ryan-medium` if the file is missing; it never claims a voice that is not installed.

**False-activation troubleshooting.** `logs/voice_log.jsonl` (`.jarvis/voice_log.jsonl`): a `wake` line carries `source` (`model`/`stt_confirmed`/`manual`), `score`, `threshold`, `state_before`, `stt_confirmation`; `wake_rejected` lines give the reason (`not a wake phrase`, `stale manual activation dropped`, `speech recognition unavailable`). Too many rejections: raise `WAKE_WORD_THRESHOLD`/`WAKE_CANDIDATE_FLOOR`; wake missed: lower them (the phrase check still protects against false activations). To change the timeout safely keep it at least 10 s (setting range 3-3600); very long timeouts keep JARVIS listening to a room for longer, and everything said in an open session is transcribed locally and sent to the language model.

**Privacy.** Raw audio is never stored. The wake check keeps a rolling ~2.4 s in-memory ring for one local transcription; nothing before a validated wake is sent to Groq, and rejected transcripts are logged by length only.
