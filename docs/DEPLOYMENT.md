# Deployment (Windows, without VS Code)

JARVIS runs as `pythonw -m desktop.launcher`: no console window, a tray icon, and background services (voice runtime, reminder scheduler, health monitor,
supervisor, intelligence runner, local API). No executable/installer is built: the PowerShell scripts below were judged sufficient (no packaging technology added).

## Prerequisites

Windows 10/11, Python 3.11+, PostgreSQL (for tasks, reminders, memory, events, RAG, graph), a microphone, the wake-word/TTS models (`docs/voice-system.md`),
Ollama with the model named in `LLM_MODEL` (only for questions the intelligence layer does not answer itself). Gmail/Calendar/Telegram are optional.

## Install

```powershell
.\scripts\windows\install_jarvis.ps1                    # Startup-folder shortcut (default)
.\scripts\windows\install_jarvis.ps1 -UseScheduledTask   # per-user logon task that also restarts JARVIS if the whole process dies
.\scripts\windows\install_jarvis.ps1 -NoStartup -SkipMigrations
```
It creates `.venv`, installs `requirements.txt`, copies `.env.example` to `.env` **only if `.env` does not exist**, creates `logs/` and `.jarvis/`, runs
`alembic upgrade head`, checks the connection, and registers start-with-Windows. No administrator rights. Then edit `.env` (`DATABASE_URL`, `WAKE_WORD_MODEL_PATH`, `TTS_MODEL_PATH`, `JARVIS_TIMEZONE`, integration flags).

## Production configuration

`APP_ENV=production`, `LOG_LEVEL=INFO`, `JARVIS_LOG_JSON=true` (optional), `JARVIS_AUTO_RECOVERY=true`. Secrets only in `.env` (git-ignored) or `.jarvis/` (git-ignored); never commit them
(`python scripts/secret_scan.py` checks). Every setting is documented in `.env.example`. Notable: `JARVIS_PRIVACY_DEFAULT`, `JARVIS_OFFLINE_MODE`,
`JARVIS_API_ENABLED` (dashboard on `http://127.0.0.1:8000/dashboard`, loopback only), `JARVIS_INTELLIGENCE_*`, quiet hours and working hours (also changeable by voice).

## Run, stop, restart

```powershell
.\scripts\windows\start_jarvis.ps1      # hidden; tray icon turns green when online
.\scripts\windows\stop_jarvis.ps1       # graceful (`python -m desktop.launcher --stop`): microphone released, services stopped, database pool closed; never force-kills
# restart = stop, then start (or tray -> Restart JARVIS to restart only the voice runtime)
```
Tray states: 🟢 Online, 🟡 Starting, 🔴 Offline, ⏸ Paused (or private), ⚠ Degraded. Startup log: `logs/jarvis.log` (`startup_begin`, `database_check`, `startup_complete`).

## Update

```powershell
.\scripts\windows\update_jarvis.ps1     # stop, git pull --ff-only, pip install, alembic upgrade head, start
```
It refuses to update while JARVIS will not stop, and does not restart if the migration fails.

## Uninstall

```powershell
.\scripts\windows\uninstall_jarvis.ps1                        # stop, remove Startup shortcut and Scheduled Task
.\scripts\windows\uninstall_jarvis.ps1 -RemoveLocalState      # also delete .jarvis (asks first; -Force skips)
```
Your database, `.env`, models and documents are never touched.

## Verify an installation

```powershell
.venv\Scripts\python.exe scripts\e2e_launcher_check.py --privacy private   # starts a throw-away instance, checks API/health/mic state, stops it, checks exit code
.venv\Scripts\python.exe scripts\secret_scan.py
.venv\Scripts\python.exe -m pytest -q
```
`--privacy active` also starts the real wake-word listener (the microphone opens for the run).

## Not verified on this machine

Startup-folder shortcut creation, the Scheduled Task, `update_jarvis.ps1` and `uninstall_jarvis.ps1` were syntax-checked but not executed (they change the machine's startup
configuration or reinstall dependencies). Run them once and check the tray after signing out and in.
