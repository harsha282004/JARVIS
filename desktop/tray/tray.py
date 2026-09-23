"""Windows system-tray controller (pystray).

Runtime control only: it shows the current RuntimeState and forwards
Start/Resume, Pause, Restart and Exit to the RuntimeManager. It never
touches the VoiceEngine directly, and it is not a dashboard or chat UI.
"""

import threading
from collections.abc import Callable

import pystray
from PIL import Image, ImageDraw

from backend.core.logging import get_logger
from desktop.runtime.manager import RuntimeManager
from desktop.runtime.state import RuntimeState, RuntimeStatus

logger = get_logger(__name__)

TRAY_READY_TIMEOUT_SECONDS = 5.0

_STATE_COLORS: dict[RuntimeState, str] = {
    RuntimeState.STARTING: "#3b82f6",
    RuntimeState.RUNNING: "#22c55e",
    RuntimeState.PAUSED: "#eab308",
    RuntimeState.STOPPING: "#9ca3af",
    RuntimeState.STOPPED: "#6b7280",
    RuntimeState.ERROR: "#ef4444",
}


class TrayError(Exception):
    """Raised when the tray icon cannot be created."""


def render_icon(state: RuntimeState) -> Image.Image:
    """Draw a state-colored disc with a 'J' (no external image assets needed)."""
    size = 64
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.ellipse((4, 4, size - 4, size - 4), fill=_STATE_COLORS[state])
    draw.text((size // 2 - 4, size // 2 - 6), "J", fill="white")
    return image


class TrayController:
    def __init__(self, manager: RuntimeManager, on_exit: Callable[[], None]):
        self._manager = manager
        self._on_exit = on_exit
        self._icon: pystray.Icon | None = None

    def start(self) -> None:
        """Show the tray icon. Raises TrayError if it does not come up."""
        ready = threading.Event()

        def setup(icon: pystray.Icon) -> None:
            icon.visible = True
            ready.set()

        try:
            self._icon = pystray.Icon(
                "jarvis",
                render_icon(self._manager.state),
                self._title(self._manager.status()),
                menu=self._build_menu(),
            )
            self._icon.run_detached(setup)
        except Exception as exc:  # noqa: BLE001 - pystray backends raise assorted platform errors
            self._icon = None
            raise TrayError(f"Could not create the system tray icon: {exc}") from exc

        if not ready.wait(TRAY_READY_TIMEOUT_SECONDS):
            self.stop()
            raise TrayError("System tray icon did not initialize in time")

        self._manager.add_listener(self._on_status)
        logger.info("Tray initialized")

    def stop(self) -> None:
        icon, self._icon = self._icon, None
        if icon is not None:
            icon.stop()
            logger.info("Tray stopped")

    def _build_menu(self) -> pystray.Menu:
        return pystray.Menu(
            pystray.MenuItem(lambda item: f"JARVIS: {self._manager.state.value}", None, enabled=False),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(
                "Start / Resume",
                self._start_or_resume,
                enabled=lambda item: self._manager.state
                in (RuntimeState.PAUSED, RuntimeState.STOPPED, RuntimeState.ERROR),
            ),
            pystray.MenuItem(
                "Pause", self._pause, enabled=lambda item: self._manager.state is RuntimeState.RUNNING
            ),
            pystray.MenuItem(
                "Restart",
                self._restart,
                enabled=lambda item: self._manager.state
                in (RuntimeState.RUNNING, RuntimeState.PAUSED, RuntimeState.ERROR),
            ),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Exit", self._exit),
        )

    def _start_or_resume(self, icon=None, item=None) -> None:
        if self._manager.state is RuntimeState.PAUSED:
            self._manager.resume()
        else:
            self._manager.start()

    def _pause(self, icon=None, item=None) -> None:
        self._manager.pause()

    def _restart(self, icon=None, item=None) -> None:
        self._manager.restart()

    def _exit(self, icon=None, item=None) -> None:
        logger.info("Exit requested from tray")
        self._on_exit()

    def _on_status(self, status: RuntimeStatus) -> None:
        icon = self._icon
        if icon is None:
            return
        icon.icon = render_icon(status.state)
        icon.title = self._title(status)
        icon.update_menu()

    @staticmethod
    def _title(status: RuntimeStatus) -> str:
        title = f"JARVIS - {status.state.value}"
        if status.last_error:
            title += f" ({status.last_error})"
        return title[:127]  # Windows tooltip limit
