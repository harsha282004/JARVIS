# Windows tray

JARVIS shows one system-tray icon (`desktop/tray/tray.py`, built on `pystray`). It never touches the voice engine
directly — every action goes through `RuntimeManager`, `VoiceSwitch` or `VoiceControl`, the same objects the dashboard
API uses, so the tray and the dashboard can never show different states.

## Where the icon actually appears

Windows decides whether a tray icon sits directly in the notification area or inside the **"^" hidden-icons overflow**
next to the clock — this is a per-icon Windows setting (Settings → Personalization → Taskbar → "Select which icons
appear on the taskbar"), not something JARVIS controls. Both locations are the same icon; if you don't see JARVIS at a
glance, click **"^"** first. If it isn't there either, see "Missing tray icon" below.

## Left click: instant Voice ON/OFF

**A single left click on the JARVIS tray icon toggles voice ON/OFF directly — no menu needed.** This is the fastest
way to control JARVIS: pystray invokes the menu's one `default=True` item on a plain click, and that item is the
same "Turn Voice ON"/"Turn Voice OFF" action described below, calling the exact same `VoiceSwitch` the dashboard uses
(`desktop/tray/tray.py::_toggle_voice_power`). It is deterministic — no LLM call is involved — and it only ever
toggles the voice subsystem: the application, dashboard, API, Gmail, Calendar and Agent all keep running unaffected.
If the toggle fails for some reason, the tray logs it and shows a balloon notification rather than silently claiming
success; the icon/tooltip always re-read the real switch state afterward, so it can never show a state that didn't
actually happen.

## Right-click menu

```
JARVIS
<status>                              e.g. 🟢 Online / ⚠ Degraded / ⏻ Voice OFF
<microphone indicator>                e.g. 🎙 Listening / 🔇 Microphone disabled
● Voice: ON                           <- plain status line (○ Voice: OFF when off)
Turn Voice OFF                        <- the ONE valid action for the current state (default item: also the left-click action)
---
Talk to JARVIS                        starts a conversation without the wake word
Stop speaking
Today's Briefing / Tasks / Reminders / Memory / Integrations
Open dashboard
<autonomous task controls, if a task is running>
Open browser / Close browser / Stop browser action
Voice Settings                        opens the dashboard's Voice panel
Settings                              opens .env in your editor
---
Mute voice / Voice notifications / Do Not Disturb
---
Pause listening (Resume listening)    pauses the RUNTIME (distinct from the voice switch; see below)
Private mode (microphone off)
Restart JARVIS
---
Exit
```

## JARVIS — ON/OFF vs. Pause listening vs. Private mode

These look similar but answer different questions:

- **JARVIS — ON/OFF** is the voice switch (`voice.switch.VoiceSwitch`): the user's persistent choice of whether voice
  should run at all. OFF releases the microphone and stops wake word/VAD/STT; it survives a restart.
- **Pause listening / Resume listening** stops/starts the `RuntimeManager` worker directly, for a quick "stop
  listening for a minute" without changing the persisted ON/OFF preference. Turning the voice switch OFF also pauses
  the worker, but pausing the worker does *not* turn the switch off — resuming later restores whatever the switch says.
- **Private mode** is a privacy mode, not the voice switch: it also closes the microphone (and additionally stops
  external monitoring), and is meant for "nothing should listen or observe right now" rather than "I don't want voice".
  Privacy and the voice switch both have to allow the microphone for it to actually open.

When any of these keeps the microphone closed, the tray shows it truthfully: OFF is `⏻ Voice OFF`; Paused/Private show
`⏸ Paused` with the reason in the tooltip detail.

## Tooltip

Hovering the icon shows the real, current voice state in one or two short lines (never a claim of "Listening" while
voice is actually off), read from the same `voice.status.VoiceStatus.snapshot()` the dashboard uses:

| State | Tooltip |
|---|---|
| OFF | `JARVIS — Voice OFF` / `Click to turn Voice ON` |
| ON, never conversed yet | `JARVIS — Voice ON` / `Listening for "Hey JARVIS"` |
| ON, sleeping after a conversation | `JARVIS — Voice ON` / `Sleeping — say "Hey JARVIS" to wake` |
| Mid-conversation, listening | `JARVIS — Voice ON` / `Listening...` |
| Transcribing or thinking | `JARVIS — Processing` |
| Speaking | `JARVIS — Speaking` |

The "never conversed yet" vs "sleeping after a conversation" distinction uses `conversation.sleep_reason` from the
snapshot (`None` until the first conversation actually ends) — both are technically the same engine state
(`voice_state == "waiting"`), so this is the one honest signal available to tell them apart, not a fabricated one.

## What's real, never faked

The tray never shows a status it cannot back up: 🟢 Online only when the voice runtime is running and no critical
service is failing; ⚠ Degraded when a non-critical service (Gmail, Calendar, LLM, ...) is unhealthy; 🔴 Offline when the
runtime itself is stopped or crashed. A menu item with no underlying capability (e.g. no browser agent configured) is
greyed out rather than doing nothing silently.

## Notifications

`tray.notify(title, message)` shows a native Windows balloon (truncated to ~250 characters). Reminders and proactive
alerts are delivered through the tray when the "desktop" channel is chosen; a failed notification is logged, never
retried into a loop.

## Missing tray icon

If Windows shows no JARVIS icon anywhere (notification area or "^"), work through these in order:

1. **Is JARVIS actually running?** `python scripts/jarvis_status.py` — if it says `STOPPED`, nothing built a tray icon
   because there is no process; see "Last shutdown" in its output for why it stopped, then start it (log in again, or
   `Start-ScheduledTask -TaskName JARVIS` in PowerShell to start it immediately without logging out).
2. **Did something send it a stop signal?** `python scripts/jarvis_status.py`'s "Last shutdown" line names the reason.
   `user_exit (stop command)` means something ran `python -m desktop.launcher --stop` (the tray's own Exit item, or a
   script). **This includes `scripts/e2e_launcher_check.py`**: before it isolated itself with `JARVIS_INSTANCE_ID`, its
   cleanup step used the same OS-level "stop" signal as a real launch, so running it while your real JARVIS was up
   would silently shut down *your* assistant, not just the throwaway instance the script started — no tray bug at all,
   just your real JARVIS having been told to exit. It now uses its own private mutex/signal names precisely so this
   can't happen; if you still see it, you're on an older build of that script.
3. **Did the process start but fail to create the icon?** Check `logs/jarvis.log` for `TRAY_STATUS=running` (success)
   vs. a tray error — the launcher never lets a tray failure take down the rest of JARVIS (dashboard, voice,
   integrations keep running), so a stuck/missing icon with the API otherwise healthy points here. Tray -> Restart
   JARVIS, or restart the process, to retry icon creation.
4. **Is it a duplicate/second copy?** `desktop/launcher/single_instance.py`'s single-instance guard means a second
   launch (e.g. starting it manually while the Task Scheduler copy is already up) exits immediately without a tray —
   by design, so it never fights the first one for the microphone. `jarvis_status.py`'s PID is the one that's real.

## Testing without a real tray

`desktop/tray/tray.py`'s menu, icon rendering and `TrayActions` wiring are unit-tested with a `pystray` menu built in
memory (no real icon shown) — see `tests/voice/test_e2e_scenarios_api_tray_security.py` and
`tests/desktop/test_runtime_hardening.py`. The tray itself (`TrayController.start()`) is only exercised for real by
running the application (`python -m desktop.launcher`) or `scripts/e2e_launcher_check.py --no-tray false`.
