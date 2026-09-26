# Implementation log

Newest first. Each entry says what was decided, what was found by testing, and what was fixed. (Earlier phases are documented in their own reports: `COMBINED_PHASE_16_17_IMPLEMENTATION.md`, `PHASE_18_IMPLEMENTATION.md`.)

## Phase 21 — Autonomous computer agent, multi-step planning, controlled task execution

### Decisions

1. **Deterministic, not model-driven.** Planning, action choice, observation and verification are code (grammar + rules). It works offline, has zero model latency, cannot be talked into anything by a page, and is fully testable. A model-proposed plan is supported through the same validator (`from_proposal`), not wired to a live model.
2. **One door.** Planner and runner import nothing that acts; `ToolRouter.call` is the single caller of `BrowserTools.call`; the GitHub API is reached through the existing hub tools (plus one new read-only `read_repository_readme`).
3. **Reuse the confirmation engine** (Phase 17) for task-level confirmations instead of a new pending-action store: strict yes/no, single use, bound to the step, and the Phase 19 low-confidence guard applies for free.
4. **Risk is computed, propagated as a maximum, and can only be raised by the page.** Priced from the tool, arguments and the plan's wording; re-priced from real values before running.
5. **Typed references** instead of free text between steps; text from pages never becomes a tool name or an untyped argument.
6. **Verify against the world.** Every step has explicit checks; state checks use an independent observation.
7. **Single commands stay with Phase 20.** Autonomy takes multi-step and research goals only.
8. **API before browser**, browser as an honest fallback ("GitHub isn't connected, so I searched publicly… can't tell whether it's yours").

### Found by testing and fixed

| Found by | Problem | Fix |
|---|---|---|
| unit tests | "the acoustic one" chose option 1 ("one" was an ordinal) | ordinals are first/second/third… and digits only |
| unit tests | a repeated network timeout was reported as "the page isn't responding" (loop) instead of its cause | transient network failures are bounded by retries and reported by cause; loops are for unresponsive pages |
| unit tests | "Find the report and download it" was split into two clauses and searched for "it" | whole-goal pattern before splitting |
| unit tests | a live-thread test returned after the whole task (inline wait) so "Stop" hit a finished task | tests use a short inline wait; production default 25 s |
| unit tests | ambiguity numbering mixed the tool's numbering and the spoken numbering | `n` is what the user hears, `orig_n` the tool's |
| unit tests | download risk was LOW because the target name was a run-time reference | risk also reads the plan's wording ("Download …") and is re-priced with real values |
| unit tests | a proposed plan without a report step failed "without an answer" | verified steps without a report complete with "Done. Every step was carried out and verified." |
| unit tests | a clarification answer that was really a new goal was consumed as an answer | new-goal verbs end the clarification |
| review | `upload_file.filename` accepted `../../etc/passwd` at schema level | bare-name pattern in the browser schema |
| real Edge | a task treated another local port as "already open" (`same_site` compared only registrable domains) | IPs and ports compare exactly; observation carries `host:port`; a goal that names a page is not skipped because the site is open |
| real YouTube | the tool said "playing" while the ad was buffering; verification (independent observation) said not playing | tool requires `not paused`; runner re-observes media until it settles (`AUTONOMY_OBSERVATION_TIMEOUT_SECONDS`) |
| real Bing | two pages of the official site plus a look-alike (`postgres.guide`) made "most relevant official result" ambiguous | the technology's own domain scores highest; other pages of the same site are not rivals |
| real GitHub (no API) | a public search result was announced as "your repository" | honest wording when the repository came from a public search |
| real app | (none) | task API, panel and shutdown verified in the real process |

### Not done / deferred

See `PHASE_21_IMPLEMENTATION.md` §Limitations.

## Phase 20 — Controlled browser and computer agent

### Decisions

1. **Playwright + the installed Edge/Chrome**, on one dedicated thread (its sync API is thread-affine); a small `PageDriver` interface lets every behavior be tested against a deterministic fake web *and* against a real browser.
2. **Tools, not scripts.** 31 registered tools with strict schemas (extra keys forbidden). No shell, JavaScript, cookie, path or selector arguments exist; the only page scripts are fixed constants in `browser/scripts.py`.
3. **Reuse the existing gates.** The five browser categories are registered with the Phase 5 `PermissionManager`; consequential actions use the Phase 17 `ConfirmationEngine` (single use, bound to the exact arguments) rather than a new pending-action mechanism. The category is decided from the user's words *and* the matching elements' real names.
4. **Verify or say so.** Every action has an explicit verification; an unverifiable success is spoken as such; clicks, typing and uploads are never retried, not even after a crash.
5. **API first, browser second.** `BrowserRouter` runs after `HubRouter`; repositories are resolved through the GitHub integration and only *opened* in the browser.
6. **Deterministic routing, LLM not connected.** Like Phase 17/18, so it works offline and page text cannot steer it. Giving the AgentBrain the descriptors is a follow-up.
7. **Different site = new tab**; same site = same tab; site root already open = reuse (idempotence).

### Found by testing and fixed

| Found by | Problem | Fix |
|---|---|---|
| fake-web tests | a click on two "Save" buttons that differ was treated as one | ambiguity returns candidates; an explicit index resolves |
| fake-web tests | opening YouTube replaced the page the user was on (and "close YouTube" then closed everything) | a different site opens in a new tab |
| tests | waits used real time under a no-op sleep (94 s suite) | injectable clock; virtual time in tests |
| tests | shutdown then a new request waited 14 s for a stopped worker | fail fast when shut down |
| real Edge | tab titles empty on the dashboard: Playwright objects were read from another thread | per-tab cache refreshed on the browser thread |
| real Edge | a click on a `target=_blank` link hung: the navigation route swallowed an exception and never continued the request (popups have no frame yet) | always continue/abort the request; treat a frameless request as main-frame |
| real Edge | the downloads panel (`edge://downloads-hub`) opened after a download and was adopted as the active tab | browser-internal pages are never adopted |
| real Edge | a 404 without a page body surfaced as an unhelpful error; unreachable hosts, unsafe ports and bad certificates likewise | mapped to spoken reasons; certificates are never bypassed |
| real local site | video seek failed: my test server lacked HTTP Range support | server fixed (a test bug, not an engine bug) |
| real YouTube | two official results (video, audio) tied and were always ambiguous | official video preferred unless audio was asked (margin 0.1) |
| real YouTube | skip-ad clicked a hidden button; seeking an ad silently failed | the skip button must be visible (waited for up to 7 s); seek during an ad is refused with the reason |
| real DuckDuckGo | headless browsers get a human-check page ("bots use DuckDuckGo too") | reported, never solved; default search is Bing; results are read after they render; Bing tracking links are unwrapped |
| real app run | the process-left check counted its own PowerShell | filter by browser process name |
| voice | bare "Pause." was the Phase 19 *Wait* control word | "pause" removed from the wait words ("wait", "hold on", "one moment" remain) |

### Tests changed on purpose

None of the earlier tests needed editing for Phase 20. `voice/normalize.py` no longer treats "pause" as a wait word (the Phase 19 tests still pass).

## Phase 19 — Advanced voice, natural conversation

### Decisions

1. **Extend, don't duplicate.** One `VoiceEngine`. New behavior arrived as injected strategies (settings store, VAD, policy, status, log). Without them the engine behaves like the original fixed-window engine, so the Phase 1–3 tests kept passing untouched (only tests that pinned behavior Phase 19 deliberately changed were edited, listed below).
2. **The agent entry point did not change.** The voice layer still calls `ConversationEngine.respond(text)` exactly once per request. Control words, confidence guard, clarification, correction and follow-ups are either handled before it (control words: they are not requests) or inside the conversation layer through the existing executor/permission path.
3. **Deterministic over LLM for conversation glue.** Follow-ups, clarification answers and corrections are code, not prompts: they work with Ollama down, cannot hallucinate, and are unit-testable.
4. **Fail toward silence and honesty.** Interrupt and Stop always err on the side of stopping; a misheard confirmation err on the side of asking again; unknown "it" is asked, not guessed; unknown settings are rejected.
5. **Reminder creation stays LOW risk.** The spec's "remind me at 6 PM to study with confirmation" is satisfied by the spoken read-back; destructive actions (cancel) keep the strict yes. I did not raise the risk level, because that would change Phase 9's tested policy without a security reason.
6. **Barge-in default is the wake word**, not loud-speech detection: there is no echo cancellation, so `vad` mode would hear JARVIS's own voice on speakers. `vad` exists for headsets.

### What was built (files)

New: `voice/{settings,vad,normalize,policy,status,control}.py`, `agent/intelligence/followups.py`, `scripts/voice_real_check.py`, `tests/voice_helpers.py`, `tests/voice/test_{vad_normalize_settings_policy,engine_phase19,conversation_natural,e2e_scenarios_api_tray_security,providers_health_queue}.py`, docs `VOICE_ARCHITECTURE.md`, `CONFIGURATION.md`, `IMPLEMENTATION_LOG.md`, `PHASE_19_IMPLEMENTATION.md`; `security.md` gained a voice section (on Windows `SECURITY.md` and `security.md` are the same file).

Changed: `voice/engine.py` (rewritten around the same public API), `voice/audio.py` (cancellable `AudioOutput`, `list_input_devices`), `voice/stt/*` (`Transcription`, confidence, vocabulary hint), `voice/tts/piper_provider.py` (speed), `voice/wakeword/openwakeword_provider.py` (sensitivity, reset, last score), `voice/bootstrap.py`, `agent/tasks/{notifications,executor,tools,scheduler}.py` (priorities, clarification, correction), `agent/proactive/engine.py` (priority metadata), `agent/intelligence/hub_router.py` (remembers the last list), `backend/core/conversation/engine.py` (`awaiting_answer`, `cancel_pending`, `last_action_name`, clarification/correction step), `backend/core/{config,context}.py`, `backend/api/routes/system.py` + `dashboard.html` (Voice panel, `/voice*`), `desktop/{tray/tray,runtime/manager,runtime/composition,runtime/health_checks,launcher/cli}.py`, `.env.example`, `scripts/e2e_launcher_check.py`.

### Found by testing and fixed

| Found by | Problem | Fix |
|---|---|---|
| unit tests | shutdown requested before the microphone opened skipped opening (a test pinned "released on stop") | open first, check stop only while retrying |
| unit tests | extra `should_stop` checks in the conversation loop changed the pinned call sequence and dropped a turn | no extra checks in the legacy path; in VAD capture a shutdown returns "stopped" |
| architecture test | `voice/engine.py` listed tool names such as `briefing_action` (a guard forbids briefing knowledge in the voice package) | the conversation exposes `last_action_name`; the engine just reads it |
| unit tests | barge-in test never barged in: the 0.3 s playback grace used wall-clock time and the fake played instantly | grace is an injectable setting |
| unit tests | permission-denied status was overwritten with `CLOSED` when the activation ended | unavailable statuses are kept until the next successful open |
| unit tests | (design) a stray "Stop speaking" click while idle would have swallowed the next reminder | the interrupt flag is only remembered while speaking/thinking |
| unit tests | (design) a shutdown mid-utterance would have transcribed and acted on the partial audio | dropped |
| conversation tests | correction rolled a past time to tomorrow silently | refuse: "That time has already passed" |
| API/real run | with the runtime paused (private mode) `/voice` reported `MICROPHONE_UNKNOWN` | reports `MICROPHONE_CLOSED` when paused/stopped |
| real run (Piper → VAD → Whisper) | the default minimum utterance (0.3 s) dropped a one-word "Stop." (0.24 s of speech) as noise | default 0.15 s |
| real run | Whisper base heard a lone "Stop." as "Top" 1/4, "Cancel." as "Council/Pencil" 3/4 | command-vocabulary `initial_prompt` (8/8 correct; silence still gives ""); "Top/Sop…" also accepted as Stop (safe direction) |
| real run | a tray/API activation followed by silence was counted as a wake-word false alarm | manual activations are not counted |
| unit tests | a fresh `VoiceStatus` claimed STT "not ready" (health DEGRADED before the engine existed) | readiness is tri-state: unknown / ready / not ready |
| tests | regex escapes in patch scripts produced a backspace character in two patterns (`\b`) | replaced; noted so scripts avoid `\\b` |

### Tests changed on purpose

`test_voice_failures.py` (STT/TTS failures used to crash the worker; they are now recovered and reported), `test_launcher_tray.py` ("Pause JARVIS" → "Pause listening"), `test_reminders_scheduler.py` and `test_proactive_security_actions.py` (reminder/proactive metadata now carries `priority`).

### Not done / deferred

See `PHASE_19_IMPLEMENTATION.md` §Limitations.


---

# Phase 22 — Personal Operator (autonomous workflows)

New: `workflows/{models,facts,priority,store,tools,templates,planner,runner,operator,build}.py`, `scripts/workflow_real_check.py`, `tests/workflow_helpers.py`, `tests/workflows/test_{units_planner_facts,cross_integration,scenarios_failures,security_limits,wiring_api_config,proactive_memory_recovery,real_browser}.py`, docs `PERSONAL_OPERATOR_ARCHITECTURE.md`, `WORKFLOW_ENGINE.md`, `WORKFLOW_SECURITY.md`, `WORKFLOW_TEMPLATES.md`, `PHASE_22_IMPLEMENTATION.md`.

Changed: `integrations/github/adapter.py` (`identity()`), `agent/intelligence/router.py` (operator router first, `intercept_cancel` before the confirmation engine, `cancel_task`/`task_active` include workflows), `desktop/runtime/composition.py` (build the operator; tray Pause/Resume/Stop/label cover tasks *and* workflows), `desktop/launcher/cli.py` (context, cleanup), `backend/core/{config,context}.py` (11 `WORKFLOW*` settings, `AppContext.operator`), `backend/api/routes/system.py` (`/workflows*`), `backend/api/dashboard.html` (Personal Operator panel), `browser/engine.py` (see below), `scripts/e2e_launcher_check.py` (operator checks), `.env.example`, docs (`AUTONOMOUS_AGENT_ARCHITECTURE`, `AUTONOMOUS_TASKS`, `BROWSER_ARCHITECTURE`, `VOICE_ARCHITECTURE`, `CONFIGURATION`, `architecture`, `README`).

### Found by testing and fixed

| Found by | Problem | Fix |
|---|---|---|
| scenario test | choosing the calendar's date in a date conflict still created the task on the email's date (the task step read the verified fact, not the comparison's resolved one) | typed `From(..., fallback=...)`: the final fact is the comparison step's |
| scenario test | the question was announced before the confirmation request existed (a very fast "yes" would find nothing pending) | status changes only after the request is registered |
| security test | a rebuilt (hand-edited) checkpoint was not validated; literal argument *types* were not checked at plan time | recovery runs `validate()`; scope recomputed; literal arguments schema-checked (references when resolved) |
| review | unbounded number of unfinished workflows | cap of 3 × `WORKFLOW_MAX_CONCURRENT` |
| review | "Cancel" spoken while a confirmation was open was eaten by the confirmation engine as a "no" | `intercept_cancel` before `confirmations.respond` |
| scenario test | an empty calendar failed the "has:events" check | `has:` means present |
| grammar tests | "Open the application link from the internship email" planned as *apply*; "Turn that into a reminder" mis-fired on "this week"; "task for it" gave topic "it"; briefing phrase swallowed "check tomorrow's calendar and related emails" | verb required for apply; pronoun rules; topic stop-words; meeting preparation before briefing |
| tests | patch scripts wrote a backspace character for `` in one regex (again) | replaced; patch scripts use `chr(92)` or the Edit tool |
| **real-browser tests** | **latent Phase 20/21 bug:** `BrowserEngine._page_result` verified the final page against the *host without its port*, so any address with a port (every local test server) failed verification ("I asked for 127.0.0.1 but the browser shows http://127.0.0.1:52952/") and **all 16 real-browser tests of Phases 20/21 had been skipping silently**. Public sites were not affected | compare against the requested address when there is one (ports compared exactly, as `same_site` intends). All 16 real-browser tests now run and pass, plus 3 new operator real-browser tests |

### Tests changed on purpose

`tests/autonomy/test_wiring_api_tray_config.py` and `tests/browser/test_voice_scenarios_api_tray.py` (they pinned the exact source text of the composition root: the router now takes the operator router and the tray controls come from `_task_controls`).

### Not done / deferred

See `PHASE_22_IMPLEMENTATION.md` §Known limitations.


---

# Microphone capture fix (Windows)

**Root cause (measured on this machine).** The microphone was not dead: `AudioInput` opened the Windows default (MME "Microphone Array") at 16 kHz mono and delivered real samples, but (1) `voice_real_check.py --mic` printed levels to four decimals, and a quiet room is ~0.00002 of full scale, so it showed `min 0.0000 max 0.0000` as if the device were silent; (2) device selection was a bare default/index with no validation, no fallback between the four Windows host APIs (DirectSound endpoint [5] delivers exact zeros on this laptop while MME/WASAPI deliver samples; WASAPI rejects 16 kHz; WDM-KS rejects the blocking API), no zero-signal detection and no diagnostics. A subsequent capture during real ambient sound gave a raw peak of 511 with PASS.

**Changed.** New `voice/mic.py` (`MicrophoneDeviceManager`, `MicSelection`, `to_pipeline`, `level_stats`); `voice/audio.py` (`AudioInput` uses it: candidates, native-format fallback with one resample, zero-signal skip, metadata-only log line, `selection`; `list_input_devices()` returns host API/channels/rate); `scripts/voice_real_check.py` (`--devices`, `--mic-only`, `--seconds`, PASS only on real samples, FAIL on zeros/≤4 LSB noise floor, raw integer peak shown); `backend/core/config.py` and `.env.example` (`MICROPHONE_DEVICE` accepts `auto`, names, indexes); docs. Existing public API of `AudioInput`, the engine and Phase 19 behaviour are unchanged.

**Tests added.** `tests/voice/test_microphone_device.py` (21): discovery (default, none, output-only, duplicate names, virtual devices), configuration modes and invalid values, format/channel fallback with resampling, backend failure fall-through, zero/near-zero/real signal, disconnect/reconnect re-resolution, privacy (metadata-only log, nothing written, no network/file APIs).

**Limitations.** A human voice through the real microphone into wake word/STT/TTS could not be exercised by the automated run (no speaker was present); the physical-microphone evidence is device enumeration, real sample capture and the real launcher's manual activation path. WDM-KS endpoints are not used (PortAudio callback-only). No software gain is applied: a very low input level must be fixed in Windows (the diagnostic says so).


---

# LLM provider: Groq replaces Ollama as the default

New: `backend/core/llm/{groq_provider,factory}.py`, `tests/llm/test_groq_provider.py`, `scripts/llm_real_check.py`, `docs/LLM_PROVIDER.md`. Changed: `LLMProviderError.kind`; `backend/core/config.py` (`LLM_PROVIDER=groq`, `LLM_MODEL=openai/gpt-oss-20b`, `GROQ_*`, `LLM_*` limits); `.env.example`; local `.env` (non-secret LLM lines only, `GROQ_API_KEY=` left blank); `voice/bootstrap.py` and `scripts/{kg_cli,rag_cli,run_voice}.py` (use `build_llm`); `desktop/runtime/health_checks.py` (staged LLM health); `desktop/runtime/manager.py` (labelled provider errors); `backend/api/routes/system.py` (`GET /llm`); `backend/core/redaction.py` (`gsk_` keys); `agent/intelligence/router.py` (time/date answered from the clock); README, TROUBLESHOOTING. No dependency added (httpx was already required); Ollama is not a dependency.

Found while testing: a model-side "response_format ... not supported" 400 was misclassified as "model unavailable" (it now falls back to non-JSON mode, with schema validation as the safety net). Before this change "What time is it?" had no capability behind it and went to the language model, which cannot know the time.

Not verified: real Groq inference and the real voice -> Groq -> Piper loop (no `GROQ_API_KEY` is configured in this environment); see `docs/LLM_PROVIDER.md` and `scripts/llm_real_check.py`.


---

# Strict wake policy, voice session/sleep, male voice

**Root cause of the spontaneous "Yes?".** It is only spoken after `_wait_for_wake` returns, and that returned on (1) any single 80 ms frame with an openWakeWord score >= 0.5 (ambient speech, TV, echo and words like "Okay Jarvis"/"Hey Travis" all reach that; the log shows `WAKE_WORD_DETECTED` followed by `No speech captured` repeatedly) and (2) a stale tray/API activation flag that stayed set until the engine next listened. Barge-in used the same one-frame trigger.

**New/changed.** `voice/wake.py` (WakeGate, exact phrase validation, sleep-command grammar, debounce/refractory windows), `voice/engine.py` (validated `WakeEvent` is the only path to the acknowledgement; second-stage local phrase check; manual request TTL; post-TTS block; strong-score barge-in; 120 s audio-time session; sleep command; silent timeout), `voice/status.py` (session_state, sleep_reason, last_wake, wake_rejections), `voice/settings.py`, `voice/bootstrap.py`, `backend/core/config.py`, `.env.example`, local `.env` (TTS lines only), `desktop/runtime/health_checks.py` (`tts_voice_check` names the real voice), `scripts/voice_real_check.py` (strict wake policy with the real models), docs, `tests/voice/test_wake_session_sleep.py` (83 tests). Existing behaviour tests were kept; only the session window default (20 s -> 120 s) changed.

**Found while testing.** A candidate was decided on the first raised frame, pre-empting the direct path (fixed: a rising score is watched until it ends or becomes sustained); the real model fires >= 0.99 on "Okay Jarvis"/"Yes Jarvis", so strong scores are also phrase-checked by default (`WAKE_DIRECT_ACCEPT=false`); a fixed-window capture reported no duration and could loop forever (time now always advances).

**Male voice.** `en_US-ryan-medium` (Piper's official male `ryan` voice) was NOT installed (only the female `lessac`); it was downloaded with the project's own `piper.download_voices` from the official piper-voices repository and verified (`dataset: ryan`, different model hash, lower pitch estimate 204 Hz vs 266 Hz for lessac on the same sentence). `.env` now points at it.

**Limits.** A standalone "Jarvis" is detected by the hey_jarvis model only inconsistently (synthetic "Jarvis." scored 0.98, bare "Jarvis" 0.03), so it relies on the candidate path and may need a second try; real human voices were not exercised by the automated run.
