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
