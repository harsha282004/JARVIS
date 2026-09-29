# Windows Runtime (Phase 2)

Phase 2 turns the Phase 1 voice engine into a persistent Windows
background application with a system-tray icon, pause/resume, optional
start-with-Windows, and graceful shutdown. It is **runtime and lifecycle
only** — no new voice, reasoning, memory, or integration behavior.

## Architecture

```
Windows runtime (desktop/launcher: CLI, JarvisApplication)
        |            \
        |             +-- desktop/tray:  TrayController (pystray)  -- runtime control only
        |             +-- desktop/runtime/power.py: SleepResumeWatcher
        v
RuntimeManager (desktop/runtime/manager.py)   <- the only thing that touches the engine
        v
VoiceEngine (voice/engine.py, unchanged state machine)
        v
Wake word / STT / LLM / TTS   (Phase 1 providers)
```

| Module | Responsibility |
|--------|----------------|
| `desktop/runtime/state.py` | `RuntimeState` enum and `RuntimeStatus` snapshot |
| `desktop/runtime/manager.py` | `RuntimeManager`: start / pause / resume / restart / shutdown, worker thread, error capture, status |
| `desktop/runtime/power.py` | `SleepResumeWatcher`: detects a wake from sleep |
| `desktop/tray/tray.py` | Tray icon + menu; forwards clicks to `RuntimeManager` |
| `desktop/launcher/startup.py` | Optional Startup-folder shortcut (`StartupManager`) |
| `desktop/launcher/single_instance.py` | Named-mutex guard: only one runtime per login session |
| `desktop/launcher/app.py` | `JarvisApplication`: startup/shutdown sequencing, keeps the process alive |
| `desktop/launcher/cli.py` | `python -m desktop.launcher` entry point and startup commands |

The runtime contains no LLM, memory, or integration logic. The only change
to Phase 1 code is two small lifecycle hooks: `VoiceEngine.run_once(should_stop=...)`
(polled while waiting for the wake word, so the microphone can be released
promptly) and `VoiceEngine.microphone_active`; plus an optional `log_file`
for `configure_logging` (a windowless process has no console).

## Reminder scheduler (Phase 9)

`JarvisApplication` also owns an optional `ReminderScheduler`: it is started after the tray and before the voice
engine, and stopped first at shutdown. It runs on its own thread, independent of the RuntimeManager and the voice
engine (pausing the microphone does not pause reminders), and a failure to start it is logged without stopping
JARVIS. Reminders are shown through `TrayController.notify` and spoken by the VoiceEngine between conversations.
See `docs/tasks-and-reminders.md`.

## Lifecycle

`RuntimeState` (application lifecycle) is separate from `VoiceState`
(WAITING/LISTENING/... inside one wake-word cycle); both appear in the status.

```
STOPPED --start--> STARTING --engine built--> RUNNING <--resume-- PAUSED
                       |                        |  \--pause------->  |
                       v (build fails)          v (worker crashes)
                     ERROR <--------------------+
ERROR --start/restart--> STARTING          any --shutdown--> STOPPING --> STOPPED
```

Startup: load config -> logging -> single-instance check -> tray -> sleep
watcher -> `RuntimeManager.start()` (builds the engine on a worker thread, so
the tray is responsive while models load; first run downloads the Whisper
model) -> `RUNNING`.

Shutdown (tray Exit, Ctrl+C, console close): `STOPPING` -> stop worker
(microphone released when the engine exits its wait loop) -> drop engine ->
stop sleep watcher -> stop tray -> `STOPPED`. `shutdown()` is idempotent and
safe to call concurrently.

Status (`RuntimeManager.status()`): runtime state, voice state, startup time
(when the runtime object was created), last error, microphone-active flag. It
is an in-process Python API; there is no network endpoint.

## System tray

Icon colour = state (blue starting, green running, yellow paused, red error,
grey stopped). Tooltip shows the state and any last error. Menu:

- **JARVIS: \<state\>** (informational)
- **Start / Resume** - resumes if paused; starts if stopped or in error
- **Pause** - stop listening, release the microphone, keep the app alive
- **Restart** - rebuild the engine (also the recovery path after an error)
- **Exit** - graceful shutdown

If the tray cannot be created, the runtime logs the error and exits with code 1
rather than running invisibly with no way to stop it. Set
`JARVIS_TRAY_ENABLED=false` to run headless deliberately (Ctrl+C to stop).

### The icon can be genuinely absent even when everything looks "running"

A confirmed bug in pystray's Windows backend: `Shell_NotifyIcon(NIM_ADD, ...)` (the call that actually asks Explorer
to show the icon) never checks its own success/failure return value. If that call fails -- which really happens for
a few seconds right after logon, or right after a Task-Scheduler-triggered interactive launch (this project's real
startup path) -- pystray proceeds exactly as if the icon were showing. Before this was fixed, JARVIS's own logs and
`jarvis_status.py` could say `TRAY_STATUS=running` while the Windows notification area (including the hidden-icons
overflow) had no JARVIS icon at all.

`TrayController` now independently confirms this with `Shell_NotifyIconGetRect` (the documented, official way a
process can ask Windows whether its own notify icon is currently registered) right after `NIM_ADD`, retries briefly
in-process for a transient "Explorer's tray isn't ready yet" failure, and re-verifies on every periodic health tick
(self-healing -- toggling the icon off/on -- if it ever finds the icon gone later, e.g. after an Explorer restart). A
genuine failure now raises `TrayError`, so `TrayKeeper`'s existing capped-backoff retry loop (2s/4s/8s/15s/30s)
actually runs instead of never firing. `jarvis_status.py` and `/status`'s `process.tray_health` report the real,
independently-verified state (`icon_registered: true/false/null`), separately from the older `tray` field (which
only ever meant "TrayController.start() did not raise").

## Pause / resume

Pause stops the worker at the next wake-word poll (~80 ms while waiting;
if it is mid-answer, the current answer finishes first), which closes the
microphone stream. Resume reuses the already-loaded models and reopens the
microphone, returning to WAITING. `PAUSED` does not unload the models, so
resume is fast.

## Errors

- Engine cannot be built (missing model file, no microphone, ...): `ERROR`,
  `last_error` set, tray and app stay alive. Fix the cause, then tray
  **Restart** / **Start**.
- Engine crashes while running: `ERROR`, traceback in the log, same recovery.
- Ollama unreachable/model missing during a cycle: not fatal. Recorded as
  `last_error`; JARVIS keeps listening and the next question retries.
- Worker does not stop within 15 s: `ERROR` (pause/restart) or logged during
  shutdown; the worker is a daemon thread so the process still exits.

## Sleep / resume

`SleepResumeWatcher` wakes every 2 s and compares wall-clock time; a gap
over ~12 s means the machine slept. On resume, a `RUNNING` runtime restarts its
worker so the microphone is reopened (audio devices can be invalid after
sleep). `PAUSED`/`STOPPED` runtimes are left alone. Limitations: it cannot act
*before* sleep (no "about to sleep" signal), and a large manual clock change
causes a harmless false positive (the microphone is just reopened). This
avoids a pywin32 dependency; native `WM_POWERBROADCAST` handling is a possible
later improvement.

## Configuration

Added to `.env` / `.env.example` (existing `Settings` class):

| Setting | Default | Meaning |
|---------|---------|---------|
| `JARVIS_RUNTIME_ENABLED` | `true` | `false`: `python -m desktop.launcher` exits immediately (disables a startup-launched JARVIS without removing the shortcut) |
| `JARVIS_TRAY_ENABLED` | `true` | `false`: run headless, no tray |

There is deliberately **no** `JARVIS_START_WITH_WINDOWS` setting: startup is
changed only by an explicit command, never as a side effect of a setting or of
launching JARVIS. Logs go to `logs/jarvis.log` (rotating, git-ignored) and to
the console when there is one.

## Running it (Windows)

```powershell
.venv\Scripts\Activate.ps1
python -m desktop.launcher            # with a console (logs visible)
.venv\Scripts\pythonw.exe -m desktop.launcher   # no console window; watch logs\jarvis.log
```

Prerequisites are the Phase 1 ones (models downloaded, `.env` configured,
Ollama running for real answers) - see `docs/voice-system.md`. A second
launch exits with code 3 ("already running").

## Start with Windows

User-level only; no admin rights, no registry edits, no installer.

```powershell
python -m desktop.launcher --enable-startup    # create shortcut in the Startup folder
python -m desktop.launcher --startup-status
python -m desktop.launcher --disable-startup   # remove it
```

`--enable-startup` creates `JARVIS.lnk` in
`%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup`. Target =
`pythonw.exe` of the interpreter you ran the command with (so use the project
`.venv`), arguments `-m desktop.launcher`, working directory = this project.
All paths are resolved at run time. Manual equivalent: press Win+R, run
`shell:startup`, and add a shortcut with those three properties. Because the
shortcut points at that interpreter and project folder, re-run
`--enable-startup` if you move the project or recreate the venv.

## Troubleshooting

- **Nothing happens on startup / no tray icon**: read `logs\jarvis.log`. The
  tray can be in the hidden-icons overflow (^) of the taskbar.
- **State ERROR with a model/path message**: fix `.env` (paths are relative to
  the project root) and choose Restart.
- **Stays in STARTING for a long time**: first run downloads the Whisper model.
- **"Another JARVIS runtime is already running"**: exit the tray instance
  first (or end `pythonw.exe` in Task Manager if it is stuck).
- **JARVIS answers with an error / silence**: Ollama not running or model not
  pulled (see `last_error` in the tray tooltip).

## Testing

Automated (`pytest`, deterministic, fakes/mocks): lifecycle states,
pause/resume, shutdown idempotency, failure handling, status, engine hook,
sleep-gap detection (fake clock), startup shortcut logic (temp folder + fake
PowerShell runner), launcher sequencing and tray menu logic (fake manager).
These do **not** prove real Windows behavior.

Windows-specific, actually executed on the development machine (see the Phase 2
report): real tray icon creation, real `RuntimeManager` + real VoiceEngine
and microphone acquire/release across start/pause/resume/simulated-system-resume/
shutdown, windowless `pythonw -m desktop.launcher` run with file logging,
single-instance rejection, and real `.lnk` creation via PowerShell (into a
temporary folder).

Still to verify by hand: clicking the tray icon/menu with the mouse; a real
sleep/resume cycle; enabling startup in the real Startup folder and logging
out/in; a spoken "Hey JARVIS" round trip through the runtime.

## Limitations

- No IPC: an external process cannot ask a running JARVIS to pause or exit
  (use the tray or Ctrl+C); a force-kill skips graceful shutdown.
- Pausing during an answer waits for that answer to finish.
- Sleep detection is after-the-fact only (see above).
- Startup shortcut is tied to the interpreter/project location.
- No installer, no Windows service (user-session app by design: it needs the
  user's microphone and audio devices).
- Phase 1 limits still apply (fixed listening window). Restarting the runtime
  discards the in-memory conversation (pause/resume keeps it).


---

# Windows auto-start and persistent runtime (hardened)

## What was wrong (root causes, each reproduced)
1. **`python -m desktop.launcher` only works when the working directory is the project.** Windows starts a Run entry (which cannot even set a working directory) and a Task Scheduler action in `System32` or the user profile. From there the interpreter answers `No module named 'desktop'` and exits with code 1 - and `pythonw.exe` has no console, so **nothing was logged and JARVIS simply disappeared**. Reproduced by starting `pythonw -m desktop.launcher` with `C:\Windows\System32` as the working directory.
2. `.env` was discovered relative to the working directory at import time (`backend/core/database.py` reads the settings before `main()` changes directory).
3. A tray icon that could not be created right after logon (Explorer's taskbar not ready) **ended the whole process** before the voice engine started.
4. Nothing recorded *why* the process stopped (`Shutdown requested` was logged without a reason), uncaught exceptions under `pythonw` were lost, and a Windows logoff/shutdown was never recognised.
5. The microphone was retried every 2 s forever, and a duplicate start exited with a failure code (which a "restart on failure" policy would treat as a crash).

A manual start from a terminal, from VS Code or with `start_jarvis.ps1` **did** keep running (verified: minutes alive, clean `--stop`), which is why it looked fine during development.

## Startup architecture
```
Windows logon
  -> Task Scheduler task "JARVIS" (per user, no admin; +20 s delay; MultipleInstances=IgnoreNew; restart 5x/1 min on FAILURE; no time limit)
       (fallbacks: HKCU Run entry, Startup-folder shortcut - exactly ONE mechanism is ever active)
  -> <project>\.venv\Scripts\pythonw.exe <project>\scripts\windows\jarvis_launcher.pyw --startup-source windows
  -> desktop.launcher.entry.run()   chdir + sys.path from the file's own location, stdio guard, excepthooks, crash log
  -> cli.main()                     config (.env resolved from the project, not the cwd) -> logging -> SingleInstanceGuard -> ExitCoordinator + signal/console/session handlers
  -> JarvisApplication.run()        tray (TrayKeeper: never fatal, retries) -> power watcher -> scheduler -> RuntimeManager/VoiceEngine -> API + health + supervisor + intelligence
       the main thread blocks on the exit event until an explicit, reasoned stop request
```
**Task Scheduler rather than Run:** a Run entry has no working directory, no delay (audio, taskbar and network may not be ready), no restart policy and no duplicate protection. The task has all four, still needs no administrator rights (`RunLevel Limited`, interactive token), and a failure exits non-zero so Windows restarts it, while a deliberate exit (tray Exit, `--stop`, Windows shutdown) and a duplicate start exit with **0** and are never restarted. If Task Scheduler is unavailable (policy), `--enable-startup` falls back to the Run entry automatically.

## Lifetime rules (regression tests: `tests/desktop/test_startup_hardening.py`)
- The application never falls through to the end of `main()`: the main thread waits on the exit event; no background thread keeps it alive.
- **Only an explicit stop request ends it, and every request has a reason**, logged as `JARVIS_SHUTDOWN_REQUESTED SHUTDOWN_REASON=<reason> DETAIL=...`: `user_exit` (tray Exit, `--stop`), `ctrl_c`, `windows_shutdown` (logoff, shutdown, restart: a real top-level window receives `WM_QUERYENDSESSION`/`WM_ENDSESSION`; console runs use the console control handler), `startup_failure`, `unexpected_exception` (stack trace, redacted, also in `logs\jarvis-crash.log`), `fatal_error`, `restart`. The first reason wins.
- **Optional parts never end the process:** the tray (retried with 2, 4, 8, 15, 30 s backoff until the taskbar exists), power watcher, scheduler, background services, the voice runtime (auto-restarted by the supervisor), the database, Groq and integrations are shown degraded instead.
- **Audio not ready at boot:** bounded exponential retry 2, 4, 8, 16, 30 s (capped, never a tight loop); the moment the microphone appears it is used.
- **Single instance:** a per-session named mutex. Windows auto-start plus a manual `start_jarvis.ps1` never create two runtimes: the second start logs `JARVIS_ALREADY_RUNNING`, changes nothing and exits 0.
- **Voice at boot:** `STARTING -> RUNNING -> WAITING`; the microphone listens for the wake word only. Nothing at boot, reconnect, health check or a stale tray/API request can produce a "Yes?": it is only reachable through a validated wake ("Hey JARVIS" / "JARVIS", see `docs/VOICE_ARCHITECTURE.md`).

## Startup log (no secrets)
`JARVIS_STARTING STARTUP_SOURCE=windows PROJECT_ROOT=... PYTHON_EXECUTABLE=... PID=... PARENT_PID=... ENVIRONMENT=... CONFIG_STATUS=ok LLM_STATUS=groq:openai/gpt-oss-20b api_key=configured`, `DATABASE_STATUS=ready`, `TRAY_STATUS=running`, `JARVIS_RUNTIME_RUNNING`, and once the voice runtime is up `STARTUP_REPORT TRAY_STATUS=... VOICE_STATUS=... MICROPHONE_STATUS=... WAKE_LISTENER=... DEGRADED=...`.

## Commands
```powershell
# start (from anywhere; does nothing if JARVIS is already running)
powershell -ExecutionPolicy Bypass -File .\scripts\windows\start_jarvis.ps1
# stop gracefully (releases the microphone and the port; never force-kills)
powershell -ExecutionPolicy Bypass -File .\scripts\windows\stop_jarvis.ps1     # or tray -> Exit, or Ctrl+C when run in a terminal
# status of everything (safe to repeat)
.\.venv\Scripts\python.exe scripts\jarvis_status.py [--json]
# verify configuration and paths without starting anything, from any directory
.\.venv\Scripts\python.exe scripts\windows\jarvis_launcher.pyw --self-check
# start with Windows
.\.venv\Scripts\python.exe scripts\windows\jarvis_launcher.pyw --enable-startup [--startup-method task|run|shortcut]
.\.venv\Scripts\python.exe scripts\windows\jarvis_launcher.pyw --startup-status
.\.venv\Scripts\python.exe scripts\windows\jarvis_launcher.pyw --disable-startup
Get-ScheduledTask -TaskName JARVIS | Get-ScheduledTaskInfo      # LastRunTime, LastTaskResult (0 = ok)
Get-Process pythonw,python | Select Id,ProcessName,StartTime,Path
Get-Content .\logs\jarvis.log -Tail 60                          # the main log
Get-Content .\logs\jarvis-crash.log -Tail 60                    # only written when something unexpected happened
```

## Troubleshooting after a reboot
| Symptom | Check |
|---|---|
| JARVIS is not running after logon | `jarvis_status.py`; `--startup-status` (exactly one mechanism?); `Get-ScheduledTaskInfo` (LastTaskResult); `logs\jarvis-crash.log`; the first `JARVIS_STARTING` line of `logs\jarvis.log` |
| No tray icon | `TRAY_STATUS=` in the log: `retrying` = the taskbar was not ready yet, it keeps retrying; a tray failure never stops JARVIS |
| No voice | `STARTUP_REPORT MICROPHONE_STATUS=`; Windows Settings > Privacy > Microphone; `scripts/voice_real_check.py --mic-only --mic` |
| It stopped by itself | the last `SHUTDOWN_REASON=` in `logs\jarvis.log` (`jarvis_status.py` shows it) |
| Two copies | not possible: the second exits 0 with `JARVIS_ALREADY_RUNNING`; `--startup-status` warns if two auto-start mechanisms are registered |
| You moved the project or recreated `.venv` | run `--enable-startup` again (the registration stores the interpreter and script paths) |

## Limitations
A Windows *reboot* is not performed by the automated verification (it would end the session): the manual procedure is in `docs/IMPLEMENTATION_LOG.md`. `python -m desktop.launcher` still needs the project directory as working directory (an interpreter limitation) and is now for manual use only; everything Windows runs uses the launcher script.
