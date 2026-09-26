# Troubleshooting

First look at the tray tooltip / dashboard (`http://127.0.0.1:8000/dashboard`, or `GET /health/services` with the dashboard's token) and `logs/jarvis.log`.

| Symptom | Likely cause | What to do |
|---|---|---|
| Tray is 🔴 Offline right after start | voice runtime failed (model path, microphone) | read `last_error` in the tooltip/dashboard; fix `.env` paths; the supervisor retries with backoff (`JARVIS_RECOVERY_*`); or tray -> Restart JARVIS |
| Tray is ⚠ Degraded | a service is bad but voice runs: see the Services table | e.g. `llm failed` / `llm disconnected` -> see `docs/LLM_PROVIDER.md` (Groq key, model, network), or start Ollama if `LLM_PROVIDER=ollama`; `database degraded (no migrations applied)` -> `alembic -c database/alembic.ini upgrade head`; `gmail failed (needs you to sign in again)` -> `python scripts/gmail_cli.py auth` |
| Tray ⏸ Paused and "Microphone disabled" | PRIVATE/PAUSED mode (saved; survives restart) or you paused it | tray -> Private mode (uncheck) / Resume, or dashboard -> Privacy -> active |
| Nothing happens when I say "Hey JARVIS" | wake word model missing, microphone busy/permission, paused | Services table (`wake_word`, `microphone`); Windows microphone privacy setting; tray -> Talk to JARVIS tests the rest of the pipeline |
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
