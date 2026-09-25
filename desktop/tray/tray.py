"""Windows system-tray controller (pystray).

Shows the real status (see `desktop.tray.status`) and forwards commands to the RuntimeManager and, through `TrayActions`,
to the rest of JARVIS (briefing, tasks, reminders, memory, integrations, settings, private mode). It never touches the
VoiceEngine directly. An action that is not available (its service is not enabled) is greyed out rather than faked.

Menu:
    JARVIS / <status> / <microphone state>
    Talk to JARVIS - Today's Briefing - Tasks - Reminders - Memory - Integrations - Settings
    Pause JARVIS (Resume JARVIS) - Private mode - Restart JARVIS - Exit
"""

import threading
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

    # ---- lifecycle -------------------------------------------------------------------------------------------------------

    def start(self) -> None:
        """Show the tray icon. Raises TrayError if it does not come up."""
        ready = threading.Event()

        def setup(icon: pystray.Icon) -> None:
            icon.visible = True
            ready.set()

        try:
            view = self.view()
            self._icon = pystray.Icon("jarvis", render_icon(view.state), self._title(view), menu=self._build_menu())
            self._icon.run_detached(setup)
        except Exception as exc:  # noqa: BLE001 - pystray backends raise assorted platform errors
            self._icon = None
            raise TrayError(f"Could not create the system tray icon: {exc}") from exc

        if not ready.wait(TRAY_READY_TIMEOUT_SECONDS):
            self.stop()
            raise TrayError("System tray icon did not initialize in time")

        self._manager.add_listener(lambda status: self.refresh())
        logger.info("Tray initialized")

    def stop(self) -> None:
        icon, self._icon = self._icon, None
        if icon is not None:
            icon.stop()
            logger.info("Tray stopped")

    def view(self) -> TrayView:
        status: RuntimeStatus = self._manager.status()
        return compute_tray_view(status, self._overall(), self._privacy_mode())

    def refresh(self) -> None:
        """Re-read the real state and update the icon, tooltip and menu. Called on runtime and health changes."""
        icon = self._icon
        if icon is None:
            return
        try:
            view = self.view()
            icon.icon = render_icon(view.state)
            icon.title = self._title(view)
            icon.update_menu()
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
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Talk to JARVIS", self._talk,
                             enabled=lambda item: a.talk is not None and state() is RuntimeState.RUNNING),
            pystray.MenuItem("Today's Briefing", self._shower(a.show_briefing, "Today's briefing"), enabled=lambda item: a.show_briefing is not None),
            pystray.MenuItem("Tasks", self._shower(a.show_tasks, "Tasks"), enabled=lambda item: a.show_tasks is not None),
            pystray.MenuItem("Reminders", self._shower(a.show_reminders, "Reminders"), enabled=lambda item: a.show_reminders is not None),
            pystray.MenuItem("Memory", self._shower(a.show_memory, "Memory"), enabled=lambda item: a.show_memory is not None),
            pystray.MenuItem("Integrations", self._shower(a.show_integrations, "Integrations"), enabled=lambda item: a.show_integrations is not None),
            pystray.MenuItem("Settings", self._settings, enabled=lambda item: a.open_settings is not None),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(
                lambda item: "Resume JARVIS" if state() in (RuntimeState.PAUSED, RuntimeState.STOPPED, RuntimeState.ERROR) else "Pause JARVIS",
                self._pause_or_resume,
                enabled=lambda item: state() in (RuntimeState.RUNNING, RuntimeState.PAUSED, RuntimeState.STOPPED, RuntimeState.ERROR),
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

    def _talk(self, icon=None, item=None) -> None:
        if self._actions.talk is not None:
            self._actions.talk()

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

    @staticmethod
    def _title(view: TrayView) -> str:
        title = f"JARVIS - {view.label} - {view.voice_label}"
        if view.detail:
            title += f" ({view.detail})"
        return title[:127]  # Windows tooltip limit
