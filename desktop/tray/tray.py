"""Windows system-tray controller (pystray).

Shows the real status (see `desktop.tray.status`) and forwards commands to the RuntimeManager and, through `TrayActions`,
to the rest of JARVIS (briefing, tasks, reminders, memory, integrations, settings, private mode). It never touches the
VoiceEngine directly. An action that is not available (its service is not enabled) is greyed out rather than faked.

Menu:
    JARVIS / <status> / <microphone state>
    JARVIS — ON / OFF  (the voice switch: OFF releases the microphone and stops wake word, VAD and speech recognition)
    Talk to JARVIS - Today's Briefing - Tasks - Reminders - Memory - Integrations - Settings
    Stop speaking - Open dashboard - Mute voice - Voice notifications - Do Not Disturb
    Pause listening (Resume listening) - Private mode - Restart JARVIS - Exit
"""

import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

import pystray
from PIL import Image, ImageDraw

from backend.core.health import OverallStatus
from backend.core.logging import get_logger
from backend.core.privacy import PrivacyMode
from desktop.runtime.manager import RuntimeManager
from desktop.runtime.state import RuntimeState, RuntimeStatus
from desktop.tray.status import TrayState, TrayView, compute_tray_view

logger = get_logger(__name__)

# In-process retries for a transient "Explorer's tray isn't ready to accept the icon yet" failure (real right after
# logon / a Task-Scheduler-triggered launch) before treating it as a genuine failure that TrayKeeper should back off
# and retry for (see `_verify_registration`'s docstring for why this check exists at all).
_REGISTER_RETRY_DELAYS = (0.2, 0.5, 1.0)


def _icon_registered_with_windows(hwnd: int, uid: int = 0) -> bool | None:
    """True/False if Windows Explorer genuinely has this notify icon registered (`Shell_NotifyIconGetRect`), or
    None if the check itself could not run (not Windows, or the API call failed for a reason unrelated to the icon).
    None must never be treated as "registered" by a caller.

    This exists because of a real, confirmed bug in pystray's Windows backend: `_win32.Icon._message()` calls
    `Shell_NotifyIcon(NIM_ADD, ...)` and never checks its BOOL return value, so if that call fails -- e.g. because
    Explorer's notification-area window is not yet ready to accept it, which really happens for a few seconds right
    after logon or a Task-Scheduler-triggered interactive launch, exactly this project's real startup path -- pystray
    proceeds exactly as if the icon were showing. `TrayController.start()`'s own `ready` event (see below) only ever
    reflects "pystray's call did not raise", which is a different and weaker claim than "Windows is showing this
    icon somewhere (including the hidden-icons overflow)". `Shell_NotifyIconGetRect` is the documented, official way
    a process can ask Windows whether ITS OWN notify icon is currently registered, and works for an icon in either
    the visible tray or the overflow -- it is not affected by which one Windows chose.

    (Separately, and harmlessly: pystray also passes `hID=` -- a field name that does not exist on
    `NOTIFYICONDATAW`, whose real field is `uID` -- so every pystray icon's uID is silently left at its default of 0,
    not `id(icon)` as pystray's own docstring implies. That does not stop the icon from displaying (0 is a legal
    uID), but it does mean `uid=0` here is not a guess -- it is the value pystray actually uses for every icon.)
    """
    if sys.platform != "win32":
        return None
    import ctypes
    from ctypes import wintypes

    class _RECT(ctypes.Structure):
        _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long), ("right", ctypes.c_long), ("bottom", ctypes.c_long)]

    class _GUID(ctypes.Structure):
        _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD), ("Data3", wintypes.WORD), ("Data4", ctypes.c_ubyte * 8)]

    class _NOTIFYICONIDENTIFIER(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.DWORD), ("hWnd", wintypes.HWND), ("uID", wintypes.UINT), ("guidItem", _GUID)]

    try:
        identifier = _NOTIFYICONIDENTIFIER(cbSize=ctypes.sizeof(_NOTIFYICONIDENTIFIER), hWnd=wintypes.HWND(hwnd), uID=uid)
        rect = _RECT()
        hres = ctypes.windll.shell32.Shell_NotifyIconGetRect(ctypes.byref(identifier), ctypes.byref(rect))
        return hres == 0  # S_OK
    except Exception:  # noqa: BLE001 - a diagnostic must never crash tray startup or refresh
        return None

TRAY_READY_TIMEOUT_SECONDS = 5.0
MAX_BALLOON_CHARS = 250  # Windows notification balloons truncate longer text

_STATE_COLORS: dict[RuntimeState | TrayState, str] = {
    RuntimeState.STARTING: "#3b82f6",
    RuntimeState.RUNNING: "#22c55e",
    RuntimeState.PAUSED: "#eab308",
    RuntimeState.STOPPING: "#9ca3af",
    RuntimeState.STOPPED: "#6b7280",
    RuntimeState.ERROR: "#ef4444",
    TrayState.ONLINE: "#22c55e",
    TrayState.STARTING: "#eab308",
    TrayState.OFFLINE: "#ef4444",
    TrayState.PAUSED: "#3b82f6",
    TrayState.DEGRADED: "#f97316",
    TrayState.VOICE_OFF: "#4b5563",
}


class TrayError(Exception):
    """Raised when the tray icon cannot be created."""


def render_icon(state: RuntimeState | TrayState) -> Image.Image:
    """Draw a state-colored disc with a 'J' (no external image assets needed)."""
    size = 64
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.ellipse((4, 4, size - 4, size - 4), fill=_STATE_COLORS[state])
    draw.text((size // 2 - 4, size // 2 - 6), "J", fill="white")
    return image


@dataclass
class TrayActions:
    """What the tray can ask the rest of JARVIS to do. `None` means "not available": the menu item is disabled.

    `talk` starts a conversation. The `show_*` callables return a short text that the tray displays as a notification.
    """

    show_briefing: Callable[[], str] | None = None
    show_tasks: Callable[[], str] | None = None
    show_reminders: Callable[[], str] | None = None
    show_memory: Callable[[], str] | None = None
    show_integrations: Callable[[], str] | None = None
    open_settings: Callable[[], None] | None = None
    toggle_private: Callable[[], None] | None = None
    talk: Callable[[], bool] | None = None
    # The JARVIS voice ON/OFF switch (voice.switch.VoiceSwitch): `toggle_voice_power` flips it, `voice_power_on` reads the real state.
    toggle_voice_power: Callable[[], object] | None = None
    voice_power_on: Callable[[], bool] | None = None
    voice_snapshot: Callable[[], dict] | None = None  # voice.status.VoiceStatus.snapshot: the same dict the dashboard reads, for the tooltip
    open_voice_settings: Callable[[], None] | None = None
    # Phase 19 voice controls. Each toggle has a matching state reader so the menu shows what is really on.
    stop_speaking: Callable[[], bool] | None = None
    toggle_mute: Callable[[], None] | None = None
    is_muted: Callable[[], bool] | None = None
    toggle_voice_notifications: Callable[[], None] | None = None
    voice_notifications_on: Callable[[], bool] | None = None
    toggle_dnd: Callable[[], None] | None = None
    dnd_on: Callable[[], bool] | None = None
    open_dashboard: Callable[[], None] | None = None
    # Phase 20 browser controls (not every browser operation: those are voice/API tools)
    open_browser: Callable[[], None] | None = None
    close_browser: Callable[[], None] | None = None
    stop_browser_action: Callable[[], None] | None = None
    browser_open: Callable[[], bool] | None = None
    # Phase 21 autonomous task controls
    task_label: Callable[[], str] | None = None
    pause_task: Callable[[], None] | None = None
    resume_task: Callable[[], None] | None = None
    stop_task: Callable[[], None] | None = None
    task_running: Callable[[], bool] | None = None
    task_paused: Callable[[], bool] | None = None


class TrayController:
    def __init__(
        self,
        manager: RuntimeManager,
        on_exit: Callable[[], None],
        actions: TrayActions | None = None,
        overall: Callable[[], OverallStatus | None] | None = None,
        privacy_mode: Callable[[], PrivacyMode] | None = None,
    ):
        self._manager = manager
        self._on_exit = on_exit
        self._actions = actions or TrayActions()
        self._overall = overall or (lambda: None)
        self._privacy_mode = privacy_mode or (lambda: PrivacyMode.ACTIVE)
        self._icon: pystray.Icon | None = None
        # Real Windows registration state (see `_icon_registered_with_windows`), not just "TrayController exists" or
        # "pystray's call didn't raise" -- None until the first verification has run.
        self._icon_registered: bool | None = None
        self._last_verify_error = ""
        self._registered_at: float | None = None

    # ---- lifecycle -------------------------------------------------------------------------------------------------------

    def start(self) -> None:
        """Show the tray icon. Raises TrayError if it does not come up -- including if pystray's own `NIM_ADD` call
        silently failed to register with Windows (see `_icon_registered_with_windows`'s docstring): that failure
        used to be invisible, reported as `TRAY_STATUS=running` with no Windows icon actually showing anywhere. Now
        it raises here, so `TrayKeeper`'s existing backoff retry loop actually runs instead of never firing."""
        ready = threading.Event()

        def setup(icon: pystray.Icon) -> None:
            icon.visible = True
            logger.info("TRAY_ICON_RUN_ENTERED")
            self._verify_registration(icon)
            ready.set()

        try:
            view = self.view()
            self._icon = pystray.Icon("jarvis", render_icon(view.state), self._title(view), menu=self._build_menu())
            logger.info("TRAY_ICON_CREATED")
            self._icon.run_detached(setup)
        except Exception as exc:  # noqa: BLE001 - pystray backends raise assorted platform errors
            self._icon = None
            logger.error("TRAY_EXCEPTION phase=create (%s)", type(exc).__name__)
            raise TrayError(f"Could not create the system tray icon: {exc}") from exc

        if not ready.wait(TRAY_READY_TIMEOUT_SECONDS):
            self.stop()
            raise TrayError("System tray icon did not initialize in time")

        if self._icon_registered is False:
            detail = f": {self._last_verify_error}" if self._last_verify_error else ""
            self.stop()
            raise TrayError(f"Windows did not register the tray icon (Shell_NotifyIcon add was not confirmed){detail}")

        self._manager.add_listener(lambda status: self.refresh())
        logger.info("TRAY_ICON_VISIBLE registered=%s", self._icon_registered)
        logger.info("Tray initialized")

    def stop(self) -> None:
        icon, self._icon = self._icon, None
        self._icon_registered = None
        if icon is not None:
            icon.stop()
            logger.info("Tray stopped")

    def _verify_registration(self, icon: pystray.Icon) -> None:
        """Confirms Windows itself will show the icon (possibly inside Hidden Icons), not just that pystray's call
        did not raise. A transient "Explorer's tray isn't ready yet" failure is retried briefly in-process first."""
        hwnd = getattr(icon, "_hwnd", None)  # pystray exposes no public accessor for this; see module docstring
        if hwnd is None:
            self._icon_registered = None  # not Windows, or a pystray internal we don't recognise: nothing to verify
            return
        for attempt, delay in enumerate((*_REGISTER_RETRY_DELAYS, None)):
            result = _icon_registered_with_windows(int(hwnd))
            if result is not False:
                self._icon_registered = result
                self._registered_at = time.monotonic() if result else None
                return
            if delay is not None:
                time.sleep(delay)
        self._icon_registered = False
        self._last_verify_error = "Shell_NotifyIconGetRect could not find the icon after NIM_ADD"
        logger.warning("TRAY_ICON_NOT_REGISTERED %s", self._last_verify_error)

    def _reverify_registration(self, icon: pystray.Icon) -> None:
        """Called from `refresh()` (on every runtime/health tick, not just at startup) to catch the icon later
        disappearing from Windows -- e.g. Explorer restarting. pystray does listen for `WM_TASKBARCREATED` and
        re-adds the icon automatically, but that re-add has the exact same unchecked-return-value blind spot as the
        original one, so it is re-verified here too, with one self-heal attempt (toggle the icon off and back on)
        rather than assuming pystray's own recovery worked."""
        hwnd = getattr(icon, "_hwnd", None)
        if hwnd is None:
            return
        was_registered = self._icon_registered
        now = _icon_registered_with_windows(int(hwnd))
        if now is None:
            return
        self._icon_registered = now
        if now:
            self._registered_at = self._registered_at or time.monotonic()
            return
        if was_registered:
            logger.warning("TRAY_ICON_LOST attempting to re-register (Explorer may have restarted)")
        try:
            icon.visible = False
            icon.visible = True
        except Exception as exc:  # noqa: BLE001 - a refresh must never crash the runtime
            logger.warning("TRAY_ICON_READD_FAILED (%s)", type(exc).__name__)
            return
        self._icon_registered = _icon_registered_with_windows(int(hwnd))
        self._registered_at = time.monotonic() if self._icon_registered else None
        logger.info("TRAY_ICON_READD result=%s", self._icon_registered)

    def health(self) -> dict:
        """Real tray health for `/status` and `jarvis_status.py` -- distinguishes "TrayController exists" from
        "Windows has actually registered the icon", per the whole reason this module was audited."""
        icon = self._icon
        thread = getattr(icon, "_thread", None) if icon is not None else None
        return {
            "controller": "running" if icon is not None else "stopped",
            "icon_created": icon is not None,
            "thread_alive": bool(thread and thread.is_alive()) if icon is not None else False,
            "icon_registered": self._icon_registered,
            "registered_at": self._registered_at,
            "last_error": self._last_verify_error,
        }

    def view(self) -> TrayView:
        status: RuntimeStatus = self._manager.status()
        return compute_tray_view(status, self._overall(), self._privacy_mode(), self._voice_on())

    def _voice_on(self) -> bool:
        reader = self._actions.voice_power_on
        return True if reader is None else bool(reader())

    def refresh(self) -> None:
        """Re-read the real state and update the icon, tooltip and menu. Called on runtime and health changes
        (and, via the periodic health tick, roughly every `JARVIS_HEALTH_INTERVAL_SECONDS`), which also makes this
        the periodic re-verification of Windows registration (`_reverify_registration`) -- no separate thread needed."""
        icon = self._icon
        if icon is None:
            return
        try:
            view = self.view()
            icon.icon = render_icon(view.state)
            icon.title = self._title(view)
            icon.update_menu()
            self._reverify_registration(icon)
        except Exception as exc:  # noqa: BLE001 - a cosmetic refresh must never crash the runtime
            logger.warning("Tray refresh failed (%s)", type(exc).__name__)

    def notify(self, title: str, message: str) -> None:
        """Show a Windows notification balloon from the tray icon. Raises TrayError if there is no tray."""
        icon = self._icon
        if icon is None:
            raise TrayError("The system tray icon is not running")
        try:
            icon.notify(message[:MAX_BALLOON_CHARS], title)
        except Exception as exc:  # noqa: BLE001 - pystray backends raise assorted platform errors
            raise TrayError(f"Could not show the notification ({type(exc).__name__})") from None

    # ---- menu ------------------------------------------------------------------------------------------------------------

    def _build_menu(self) -> pystray.Menu:
        a = self._actions
        state = lambda: self._manager.state  # noqa: E731
        return pystray.Menu(
            pystray.MenuItem("JARVIS", None, enabled=False),
            pystray.MenuItem(lambda item: self.view().label, None, enabled=False),
            pystray.MenuItem(lambda item: self.view().voice_label, None, enabled=False),
            # Two lines, not one: a plain status line ("Voice: ON/OFF") and, right under it, the ONE valid action for
            # that state -- never both "Turn Voice ON" and "Turn Voice OFF" shown together. The action item carries
            # `default=True`: pystray invokes exactly that item on a left click (Menu.__call__ finds the first
            # default item), so a single left click on the tray icon toggles voice with no menu needed. This is the
            # authoritative voice.switch.VoiceSwitch the dashboard also uses (`toggle_voice_power` = `switch.toggle`),
            # never a tray-local flag -- see the "not voice_power_on()" fallback below for how it stays honest even
            # when nothing is wired.
            pystray.MenuItem(lambda item: ("●" if self._voice_on() else "○") + " Voice: " + ("ON" if self._voice_on() else "OFF"), None, enabled=False),
            pystray.MenuItem(lambda item: "Turn Voice OFF" if self._voice_on() else "Turn Voice ON", self._toggle_voice_power,
                             default=True, enabled=lambda item: a.toggle_voice_power is not None),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Talk to JARVIS", self._talk,
                             enabled=lambda item: a.talk is not None and state() is RuntimeState.RUNNING and self._voice_on()),
            pystray.MenuItem("Stop speaking", self._stop_speaking, enabled=lambda item: a.stop_speaking is not None and state() is RuntimeState.RUNNING),
            pystray.MenuItem("Today's Briefing", self._shower(a.show_briefing, "Today's briefing"), enabled=lambda item: a.show_briefing is not None),
            pystray.MenuItem("Tasks", self._shower(a.show_tasks, "Tasks"), enabled=lambda item: a.show_tasks is not None),
            pystray.MenuItem("Reminders", self._shower(a.show_reminders, "Reminders"), enabled=lambda item: a.show_reminders is not None),
            pystray.MenuItem("Memory", self._shower(a.show_memory, "Memory"), enabled=lambda item: a.show_memory is not None),
            pystray.MenuItem("Integrations", self._shower(a.show_integrations, "Integrations"), enabled=lambda item: a.show_integrations is not None),
            pystray.MenuItem("Open dashboard", self._dashboard, enabled=lambda item: a.open_dashboard is not None),
            pystray.MenuItem(lambda item: a.task_label() if a.task_label else "No task", None, enabled=False, visible=lambda item: a.task_label is not None),
            pystray.MenuItem("Pause task", self._toggle(a.pause_task), enabled=lambda item: a.pause_task is not None and bool(a.task_running and a.task_running()) and not (a.task_paused and a.task_paused())),
            pystray.MenuItem("Resume task", self._toggle(a.resume_task), enabled=lambda item: a.resume_task is not None and bool(a.task_paused and a.task_paused())),
            pystray.MenuItem("Stop task", self._toggle(a.stop_task), enabled=lambda item: a.stop_task is not None and bool(a.task_running and a.task_running())),
            pystray.MenuItem("Open browser", self._toggle(a.open_browser), enabled=lambda item: a.open_browser is not None),
            pystray.MenuItem("Close browser", self._toggle(a.close_browser), enabled=lambda item: a.close_browser is not None and bool(a.browser_open and a.browser_open())),
            pystray.MenuItem("Stop browser action", self._toggle(a.stop_browser_action), enabled=lambda item: a.stop_browser_action is not None and bool(a.browser_open and a.browser_open())),
            pystray.MenuItem("Voice Settings", self._toggle(a.open_voice_settings), enabled=lambda item: a.open_voice_settings is not None),
            pystray.MenuItem("Settings", self._settings, enabled=lambda item: a.open_settings is not None),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Mute voice", self._toggle(a.toggle_mute), checked=lambda item: bool(a.is_muted and a.is_muted()), enabled=lambda item: a.toggle_mute is not None),
            pystray.MenuItem("Voice notifications", self._toggle(a.toggle_voice_notifications), checked=lambda item: bool(a.voice_notifications_on and a.voice_notifications_on()),
                             enabled=lambda item: a.toggle_voice_notifications is not None),
            pystray.MenuItem("Do Not Disturb", self._toggle(a.toggle_dnd), checked=lambda item: bool(a.dnd_on and a.dnd_on()), enabled=lambda item: a.toggle_dnd is not None),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(
                lambda item: "Resume listening" if state() in (RuntimeState.PAUSED, RuntimeState.STOPPED, RuntimeState.ERROR) else "Pause listening",
                self._pause_or_resume,
                enabled=lambda item: self._voice_on() and state() in (RuntimeState.RUNNING, RuntimeState.PAUSED, RuntimeState.STOPPED, RuntimeState.ERROR),
            ),
            pystray.MenuItem(
                "Private mode (microphone off)", self._toggle_private,
                checked=lambda item: self._privacy_mode() is PrivacyMode.PRIVATE,
                enabled=lambda item: a.toggle_private is not None,
            ),
            pystray.MenuItem(
                "Restart JARVIS", self._restart,
                enabled=lambda item: state() in (RuntimeState.RUNNING, RuntimeState.PAUSED, RuntimeState.ERROR),
            ),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Exit", self._exit),
        )

    def _shower(self, provider: Callable[[], str] | None, title: str) -> Callable:
        def show(icon=None, item=None) -> None:
            if provider is None:
                return
            try:
                text = provider() or "Nothing to show."
            except Exception as exc:  # noqa: BLE001
                logger.warning("Tray action failed (%s)", type(exc).__name__)
                text = "I couldn't get that right now."
            try:
                self.notify(title, text)
            except TrayError as exc:
                logger.warning("Tray notification failed: %s", exc)

        return show

    def _toggle_voice_power(self, icon=None, item=None) -> None:
        """Runs the same VoiceSwitch the dashboard uses (left click on the icon, or the "Turn Voice ON/OFF" menu
        item); the menu/tooltip then re-read the real state from that switch, never a local flag, so a failure here
        can never leave the tray claiming a state that didn't actually happen."""
        if self._actions.toggle_voice_power is None:
            return
        requested_off = self._voice_on()  # about to flip: True means we are turning it OFF
        logger.info("TRAY_VOICE_TOGGLE requested=%s", "off" if requested_off else "on")
        try:
            self._actions.toggle_voice_power()
        except Exception as exc:  # noqa: BLE001 - a menu click must never crash the tray
            logger.error("Unable to %s voice from the tray (%s)", "disable" if requested_off else "enable", type(exc).__name__)
            try:
                self.notify("JARVIS", f"Unable to turn voice {'off' if requested_off else 'on'}. It stays {'on' if self._voice_on() else 'off'}.")
            except TrayError:
                pass
        else:
            now_on = self._voice_on()
            logger.info("VOICE_STATE_CHANGED source=tray state=%s", "on" if now_on else "off")
        self.refresh()

    def _talk(self, icon=None, item=None) -> None:
        if self._actions.talk is not None:
            self._actions.talk()

    def _stop_speaking(self, icon=None, item=None) -> None:
        if self._actions.stop_speaking is not None:
            self._actions.stop_speaking()

    def _dashboard(self, icon=None, item=None) -> None:
        if self._actions.open_dashboard is not None:
            self._actions.open_dashboard()

    def _toggle(self, action: Callable[[], None] | None) -> Callable:
        def run(icon=None, item=None) -> None:
            if action is not None:
                try:
                    action()
                except Exception as exc:  # noqa: BLE001 - a menu click must never crash the tray
                    logger.warning("Tray toggle failed (%s)", type(exc).__name__)
            self.refresh()

        return run

    def _settings(self, icon=None, item=None) -> None:
        if self._actions.open_settings is not None:
            self._actions.open_settings()

    def _toggle_private(self, icon=None, item=None) -> None:
        if self._actions.toggle_private is not None:
            self._actions.toggle_private()
        self.refresh()

    def _start_or_resume(self, icon=None, item=None) -> None:
        if self._manager.state is RuntimeState.PAUSED:
            self._manager.resume()
        else:
            self._manager.start()

    def _pause_or_resume(self, icon=None, item=None) -> None:
        if self._manager.state is RuntimeState.RUNNING:
            self._manager.pause()
        else:
            self._start_or_resume()

    def _pause(self, icon=None, item=None) -> None:
        self._manager.pause()

    def _restart(self, icon=None, item=None) -> None:
        self._manager.restart()

    def _exit(self, icon=None, item=None) -> None:
        logger.info("Exit requested from tray")
        self._on_exit()

    def _title(self, view: TrayView) -> str:
        """The Windows tray tooltip: two short, honest lines built from the real voice snapshot the dashboard also
        reads (`voice.status.VoiceStatus.snapshot`) -- never a claim of "Listening" while voice is actually OFF, and
        never a generic label when a more specific one is available. Falls back to the plain runtime/voice labels
        (the previous tooltip) if no snapshot reader is wired."""
        if not self._voice_on():
            return "JARVIS — Voice OFF\nClick to turn Voice ON"[:127]
        snapshot = None
        reader = self._actions.voice_snapshot
        if reader is not None:
            try:
                snapshot = reader()
            except Exception:  # noqa: BLE001 - a tooltip must never crash the tray
                snapshot = None
        if snapshot is not None:
            voice_state = snapshot.get("voice_state")
            conversation = snapshot.get("conversation") or {}
            if voice_state == "speaking":
                return "JARVIS — Speaking"
            if voice_state in ("transcribing", "thinking"):
                return "JARVIS — Processing"
            if voice_state == "listening":
                return "JARVIS — Voice ON\nListening..."
            # "waiting": armed for the wake phrase. sleep_reason is only ever set once a conversation has actually
            # ended (timeout/command); a fresh ON that has never conversed yet has none, and gets the plainer wording.
            if conversation.get("sleep_reason"):
                return 'JARVIS — Voice ON\nSleeping — say "Hey JARVIS" to wake'
            return 'JARVIS — Voice ON\nListening for "Hey JARVIS"'
        title = f"JARVIS - {view.label} - {view.voice_label}"
        if view.detail:
            title += f" ({view.detail})"
        return title[:127]  # Windows tooltip limit
