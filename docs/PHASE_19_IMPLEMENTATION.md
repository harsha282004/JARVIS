# Phase 19 implementation report: advanced voice and natural conversation

Built on the committed Phase 18 tree (`1751c3b`). Nothing was committed or pushed. Architecture: `VOICE_ARCHITECTURE.md`; settings: `CONFIGURATION.md`; security: `security.md` → "Voice security"; decisions and bugs found: `IMPLEMENTATION_LOG.md`.

## Completed

Implemented and tested with scripted hardware (no audio device, model or network needed for the automated tests):

* **Microphone lifecycle** in the one `VoiceEngine`: retry on missing/unplugged device, permission-denied classification, mid-conversation loss handled and announced, states `MICROPHONE_CONNECTED / DISCONNECTED / PERMISSION_DENIED / CLOSED / UNKNOWN`; device names listable without opening anything.
* **VAD capture** replacing the fixed window (configurable threshold, silence, min/max utterance, pre-roll, adaptive noise floor); fixed window kept behind `VOICE_USE_VAD=false`.
* **Wake word**: runtime sensitivity, last score, reset after activation, 1 s refractory, health ("noisy" after many silent activations), false activations counted (not for tray activations).
* **Active conversation mode** with a configurable follow-up window.
* **STT**: `Transcription` (text, confidence, duration, language), failure recorded, command-vocabulary hint; no audio stored.
* **Normalisation**: wake word, fillers, ellipses, spoken punctuation, spoken corrections; **control words** Stop / Cancel / Wait handled by the voice layer (never tasks; "cancel my dentist appointment" remains a real request).
* **Barge-in** (wake word default; VAD mode for headsets; tray/dashboard "Stop speaking"), cancellable sentence-by-sentence TTS, speed and volume, states `TTS_IDLE/GENERATING/SPEAKING/INTERRUPTED/ERROR`, no overlap (single audio thread).
* **Response policy**: short answers whole, long ones summarised with a dashboard pointer; full text kept.
* **Conversation**: deterministic follow-ups over the last spoken schedule/email list (first/last/longest/shortest/count/when/how long/who sent/"and tomorrow"), spoken clarification with a pending state, elliptical answers ("Morning." → 9 AM), corrections that replace the reminder (no duplicate), cancel of a pending question, low-confidence guard for confirmations.
* **Notifications**: priorities `low/normal/high/critical` from reminders, proactive alerts and the NotificationCenter, bounded priority queue, DND (manual + scheduled, midnight-crossing, optional critical override), mute, notification switch, critical can interrupt a conversation, held announcements kept on the dashboard.
* **Settings**: validated, clamped, atomically persisted, live-applied, listeners.
* **Dashboard Voice panel**, `/voice*` API (token + loopback), **tray** items (Talk, Stop speaking, Mute voice, Voice notifications, Do Not Disturb, Open dashboard, Pause/Resume listening, Private mode, Settings, Exit) showing real state and greyed out when not wired; voice-aware **health checks**.
* **Structured redacted voice log** and **per-stage latency metrics**.
* Failure injection (STT crash, TTS crash, speaker unplugged, microphone unplugged/missing/denied, LLM down, integration unavailable, speaking over JARVIS, empty/ambiguous commands, shutdown mid-utterance).

**Requires manual work / not verifiable here:** a human speaking to a real microphone and speakers (see limitations); Ollama for free-form questions; real Gmail/Calendar/GitHub accounts; PostgreSQL.

## Testing (only what was executed)

Full suite: **2428 passed, 618 skipped** (Phase 18 ended at 2251 / 601: +177 passed; the +17 skips are the PostgreSQL variants of new database-backed tests, which cannot run here). `python scripts/secret_scan.py`: clean.

| Test set | Tests | Result |
|---|---|---|
| `test_vad_normalize_settings_policy.py`: VAD (silence-based end, configurable silence, pauses inside a sentence, no speech, click filtered, max length, pre-roll, noise floor, threshold), normalisation and control words (fillers, "JARVIS comma…", corrections, Stop/Wait/Cancel families, longer sentences never controls), settings (persist, invalid rejected, clamped, corrupt file, listeners, no secrets), policy (DND schedule across midnight, critical override, mute, switch, interruption rule), spoken summaries | 56 | pass |
| `test_engine_phase19.py`: full VAD turn and states, utterance is speech not a window, follow-up without wake word, timeout configurable, false activation/noisy health, control words never reach the agent, cancel, wait, repeat, barge-in (wake word, VAD, off, tray thread), microphone unplug/missing/denied/mid-conversation/stop-while-missing, low-confidence guard, summaries, mute, live settings, TTS states, announcements/DND/priorities/critical interruption, STT/TTS/speaker/LLM failures, deadlock guard, log redaction and fields, latency metrics, no files written, shutdown mid-utterance | 55 | pass |
| `test_conversation_natural.py` (real services, SQLite, real permission manager): reminder read-back, clarification with no second LLM call, "6 PM", new request not mistaken for an answer, cancel drops the question, three unusable answers, corrections ("Make that 7 PM", "No, I meant tomorrow"), nothing-to-correct, past time refused, confirmation still required, follow-ups (first/longest/last/count/"and tomorrow"/it/who sent), no-context-no-guess, expiry, provenance, no LLM/no mutation | 21 (13 PostgreSQL variants skipped) | pass |
| `test_e2e_scenarios_api_tray_security.py`: the **8 scenarios** (activation; calendar question → tool → TTS; unread emails; reminder by voice; "What meeting is first?" follow-up; "Stop" interrupts speech; ambiguous → clarification → completion; integration unavailable → honest failure), confirmation + shaky yes, hostile spoken text, voice API auth/fields/settings/interrupt/activate/log, dashboard panel, tray menu state and toggles, static security checks | 23 (4 PostgreSQL variants skipped) | pass |
| `test_providers_health_queue.py`: STT confidence/duration/hint, wake-word sensitivity, Piper speed, volume/stop, health refinement, priority queue, priority mapping, composition wiring, config validation, `.env.example`, manager interrupt, **real Piper → VAD → Whisper** and **real wake-word model** tests (skip when the models are absent) | 22 | pass |
| `test_voice_failures.py` (rewritten failure contract) | 4 | pass |
| Existing voice/tray/scheduler tests | edited on purpose (log) | pass |

### Real-model and real-process checks (executed)

`python scripts/voice_real_check.py --mic` — real Piper speech placed in silence, cut by the real VAD, recognized by real Faster-Whisper `base` (CPU). **Synthetic speech, not a human.**

| Sentence | Heard | Whisper confidence | STT time |
|---|---|---|---|
| What's my schedule today? | What's my schedule today? | 0.68–0.79 | 0.55–0.70 s |
| Remind me at six PM to study. | Remind me at 6 p.m. to study. | 0.59–0.70 | ≈0.6 s |
| Which meeting is the longest? | Which meeting is the longest? | 0.68–0.91 | ≈0.6 s |
| Stop. | "Top." / "Stop." (varies by run) → understood as the Stop control | 0.44–0.53 | ≈0.55 s |

Silence-only input: VAD `no_speech`; Whisper on silence returns `""`. Wake-word model on synthetic "Hey Jarvis": fired (peak 0.99–1.00); on 4.8 s of silence: did not fire. The vocabulary hint was measured separately: lone "Stop." wrong 1/4 without it, 0/4 with; "Cancel." wrong 3/4 without, 0/4 with.

Hardware: input devices enumerated (Realtek array is the default); the default microphone opened and returned frames whose level in a quiet room was ≈1e-5 (essentially silent), so **no human speech reached JARVIS in this session**.

`python scripts/e2e_launcher_check.py --privacy private` and `--privacy active` — the real launcher process with the real models and tray:

| Check | private | active |
|---|---|---|
| API up / runtime ready | 3.7 s / ≈7.6 s | 3.7 s / 7.5 s |
| microphone state truthful (`MICROPHONE_CLOSED` / `CONNECTED`) | pass | pass |
| `/voice` panel fields, settings persisted to `voice_settings.json`, DND reported, invalid setting → 400 | pass | pass |
| live setting applied (wake sensitivity 0.6 shown by the running engine) | – | pass |
| real manual activation: "Yes?" spoken at 5 % volume, VAD listened to the quiet room, no speech, back to idle, no error | – | pass (first audio 72 ms; 1 activation, 0 false alarms) |
| health (microphone/stt/tts/wake word) | disabled/healthy as expected | healthy |
| idle cost | 0.03 % CPU, 478 MB | 0.54 % CPU, 487 MB |
| graceful exit code 0, port released, no ERROR log lines | pass | pass |

### Performance (measured)

Piper synthesis: 28 ms for "Yes?", 240 ms for an 89-character answer (6.3 s of audio); speed 1.5 shortens it to 4.8 s. Faster-Whisper base on CPU: ≈0.55–0.70 s for 2.5–3.3 s of speech. Time to first audio for the activation prompt in the real process: ≈70 ms. Per-stage timers exposed: `wake_to_prompt_ms`, `speech_detection_ms`, `stt_ms`, `conversation_ms` (agent incl. tasks tools), `tool.<name>_ms` (hub tools), `tts_synthesis_ms`, `time_to_first_audio_ms`, `end_to_end_ms`. An end-to-end figure for a real spoken exchange (wake → answer heard) was **not** measured (no human, no Ollama).

## Limitations (honest)

1. **No human-speech test.** Recognition accuracy, wake-word hit/false-alarm rates, VAD tuning in a real room and real barge-in over speakers are unmeasured. Barge-in was tested with scripted microphones and speakers; echo (JARVIS's voice in the microphone) is why the default is wake-word barge-in.
2. Tray menu **clicks** and the **dashboard in a browser** were not exercised (menu objects and the API/page content were).
3. Ollama was not running: free-form LLM answers and the "language model unavailable" message were tested with fakes only. Deterministic answers (schedule, follow-ups, hub) need no LLM.
4. Integrations were exercised with the Phase 18 fakes, not real accounts; PostgreSQL not run (618 skipped).
5. Follow-ups cover schedule and email lists; GitHub/document/message lists are not follow-up aware. Corrections handle a leading correction of the reminder created in the last 5 minutes, not mid-sentence self-repair ("at 5, no, 6").
6. Long answers are **truncated at a sentence**, not LLM-summarised; the pointer to the dashboard is added.
7. DND-held announcements are recorded on the dashboard, not replayed when DND ends.
8. Changing the microphone, STT model, TTS voice or wake-word model needs a restart. The wake phrase is fixed by the model file ("Hey JARVIS"); the `wake_word` setting names it, it does not train a new one.
9. The Whisper vocabulary hint biases toward command words; it did not create text from silence in testing, but it is a bias, and "Top/Sop" style mishearings are accepted as Stop (a safe direction).
10. No speaker verification: anyone in earshot can wake JARVIS (`security.md`).
11. Tool latency is broken out only for hub tools; task/reminder tools are inside `conversation_ms`.

## Manual requirements

Speak to it: `python -m desktop.launcher`, say "Hey JARVIS", then try "What's my schedule today?", "Which meeting is the longest?", "Remind me tomorrow to study" → "Morning." → "Make that 7 PM", "Stop" while it talks. Tune with `CONFIGURATION.md`; check the dashboard's Voice panel. For real accounts: the Phase 18 manual steps. To confirm the microphone works at the Windows level: `python scripts/voice_real_check.py --mic` while talking (a level near 0.0001 or below means the microphone is muted or blocked by Windows privacy settings).
