# Troubleshooting

First look at the tray tooltip / dashboard (`http://127.0.0.1:8000/dashboard`, or `GET /health/services` with the dashboard's token) and `logs/jarvis.log`.

| Symptom | Likely cause | What to do |
|---|---|---|
| No JARVIS icon anywhere (notification area or "^") | JARVIS isn't running, or something sent it a stop signal | `python scripts/jarvis_status.py` first — `STOPPED` with `Last shutdown: user_exit (stop command)` means something ran `--stop` (possibly an old build of `scripts/e2e_launcher_check.py`; see `docs/TRAY.md`); restart with `Start-ScheduledTask -TaskName JARVIS` or by logging in again |
| Tray is 🔴 Offline right after start | voice runtime failed (model path, microphone) | read `last_error` in the tooltip/dashboard; fix `.env` paths; the supervisor retries with backoff (`JARVIS_RECOVERY_*`); or tray -> Restart JARVIS |
| Tray is ⚠ Degraded | a service is bad but voice runs: see the Services table | e.g. `llm failed` / `llm disconnected` -> see `docs/LLM_PROVIDER.md` (Groq key, model, network), or start Ollama if `LLM_PROVIDER=ollama`; `database degraded (no migrations applied)` -> `alembic -c database/alembic.ini upgrade head`; `gmail failed (needs you to sign in again)` -> `python scripts/gmail_cli.py auth` |
| "I'm being rate-limited by my language model" (or an older build's generic "I can't reach my language model") | a real, small tokens-per-minute limit on the Groq account, exhausted by ordinary conversation | see `docs/GROQ_RATE_LIMITS.md` for what was measured and mitigated; wait a few seconds and ask again, or ask fewer questions per minute |
| A Gmail question ("what's my last email?") gets "I can't carry out actions like that yet." | the model classified it as an action but the routing didn't resolve to a concrete tool (rare, now retried automatically) | if it persists, check `.jarvis/voice_log.jsonl` for the turn's `intent`/`tool`; if Gmail itself is off, the reply now says "Gmail isn't connected right now" instead of this generic line |
| JARVIS's "Yes?" is too quiet | Piper already peaks near 0 dBFS (measured) -- this is a device cold-start on the first sound played, not a low-volume synthesis | should now be fixed automatically (a silent warm-up clip primes the speaker before "Yes?" is ever spoken, `voice/audio.py`); if still quiet, raise Voice Settings -> TTS volume above 1.0 (now a real amplifier, soft-clipped, up to 2.0) |
| Tray ⏸ Paused and "Microphone disabled" | PRIVATE/PAUSED mode (saved; survives restart) or you paused it | tray -> Private mode (uncheck) / Resume, or dashboard -> Privacy -> active |
| Nothing happens when I say "Hey JARVIS" | wake word model missing, microphone busy/permission, paused, or **the JARVIS voice switch is OFF** | check `GET /api/voice` (`enabled`/`mode`) or the tray's **JARVIS — ON/OFF** item first; then the Services table (`wake_word`, `microphone`); Windows microphone privacy setting; tray -> Talk to JARVIS tests the rest of the pipeline |
| JARVIS said "Yes?" once but a follow-up "Hey JARVIS" / "JARVIS" gets no reply, even though the log shows `LISTENING_STARTED`/`STT_COMPLETED` | (fixed) the wake phrase alone normalizes to an empty string once the wake word is stripped, and used to fall through to a silent "miss" instead of a fresh acknowledgement | update to a build with the fix in `voice/engine.py` (`_converse`, the `confirm_phrase(text)` check before treating an empty normalized transcript as a miss); a bare re-said wake phrase mid-conversation now gets its own "Yes?" |
| "I couldn't check your calendar / email" | source unreachable, OAuth expired, or `JARVIS_OFFLINE_MODE=true` | this is deliberate honesty; fix the integration, it recovers on the next call |
| "Another JARVIS runtime is already running" | a second launch | `stop_jarvis.ps1` (or tray Exit) first |
| `stop_jarvis.ps1` says it is still running | the voice worker is stuck loading a model | wait, then tray -> Exit; check the log; the script never force-kills |
| Launcher exits immediately (code 2) | invalid `.env` value | `logs/jarvis.log` names the bad fields |
| Dashboard 401/403/503 | missing token / non-loopback host / JARVIS not running in this process | open `/dashboard` (it embeds the token) from the same machine while JARVIS is running |
| `Port 8000 in use` in the log | another server on `API_PORT` | change `API_PORT`; JARVIS continues without the dashboard |
| Duplicate reminders/notifications after a crash | should not happen | notification history is in `.jarvis/notifications.json`; report with the log |
| A state file was reset | it was corrupted (power loss) | it was moved to `*.corrupt` and defaults were used; look in `.jarvis/` |
| High RAM (~500 MB) | Whisper + ONNX models | use a smaller `STT_MODEL` (tiny/base) |
| Everything is slow after sleep | stale connections | JARVIS disposes the database pool on resume; if not, tray -> Restart JARVIS |

Useful commands: `python -m desktop.launcher --stop`, `--startup-status`, `python scripts/check_db.py`, `python scripts/secret_scan.py`,
`python scripts/e2e_launcher_check.py`.

If a source is "unavailable" JARVIS will not guess what it contains. If an action was not confirmed by a read-back it is reported as unverified: check the calendar/task list yourself before asking again.

## JARVIS says "Yes?" when nobody called it
Read `.jarvis/voice_log.jsonl`: the `wake` line says where it came from (`source`, `score`, `stt_confirmation`). `wake_rejected` lines are candidates that were correctly refused. If a `wake` line has `source: manual`, it came from the tray/dashboard/API "Talk to JARVIS". The wake policy only accepts "Hey JARVIS" / "JARVIS" after a local speech check (see `docs/VOICE_ARCHITECTURE.md`); raise `WAKE_WORD_THRESHOLD` if candidates are frequent. JARVIS stays awake for `VOICE_SESSION_TIMEOUT_SECONDS` (120 s) after the last thing you said and then sleeps silently; say "JARVIS sleep" to end a conversation immediately.
