"""Windows logoff / shutdown / restart handling.

A windowless background process (pythonw.exe) is not a console application, so Windows does not deliver Ctrl+C or console-shutdown events to it; it receives
`WM_QUERYENDSESSION` / `WM_ENDSESSION` on a top-level window, and is terminated shortly after if it has none. `SessionEndWatcher` owns one invisible top-level window
on its own thread so that a Windows shutdown, restart or logoff is recognised as what it is - a REQUEST TO STOP, reason `windows_shutdown`, never a crash - and gives
the application a few seconds to release the microphone, stop the services and close the API port before Windows ends the process. `install_console_handler` does the same
for the (console) case of a manual run: closing the console window or a logoff/shutdown while running from a terminal.

Best effort and Windows only: on any other platform, or if the window cannot be created, nothing happens and JARVIS keeps running (a warning is logged).
"""

import sys
import threading
from collections.abc import Callable

from backend.core.logging import get_logger

logger = get_logger(__name__)

WM_QUERYENDSESSION = 0x0011
WM_ENDSESSION = 0x0016
WM_CLOSE = 0x0010
WM_QUIT = 0x0012
ENDSESSION_LOGOFF = 0x80000000

_CONSOLE_EVENTS = {0: ("ctrl_c", "CTRL_C_EVENT"), 1: ("ctrl_c", "CTRL_BREAK_EVENT"), 2: ("user_exit", "console window closed"),
                   5: ("windows_shutdown", "user logoff"), 6: ("windows_shutdown", "system shutdown")}


def reason_for_console_event(code: int) -> tuple[str, str]:
    """(shutdown reason, detail) for a console control event code; unknown codes are treated as a plain user exit."""
    return _CONSOLE_EVENTS.get(code, ("user_exit", f"console event {code}"))


def reason_for_end_session(flags: int) -> tuple[str, str]:
    return ("windows_shutdown", "user logoff" if flags & ENDSESSION_LOGOFF else "system shutdown or restart")


class SessionEndWatcher:
    def __init__(self, on_end: Callable[[str, str], None], end_wait_seconds: float = 5.0):
        self._on_end = on_end
        self._end_wait = end_wait_seconds
        self._done = threading.Event()
        self._ready = threading.Event()
        self._thread: threading.Thread | None = None
        self._thread_id = 0
        self.hwnd = 0
        self.ended = False

    def notify_shutdown_complete(self) -> None:
        """Called by the application when it has released everything; lets a pending WM_ENDSESSION return so Windows can proceed."""
        self._done.set()

    def start(self) -> bool:
        if sys.platform != "win32":
            return False
        self._thread = threading.Thread(target=self._run, name="jarvis-session-watcher", daemon=True)
        self._thread.start()
        if not self._ready.wait(3.0) or not self.hwnd:
            logger.warning("Windows session-end watcher could not be created; a Windows shutdown will not be handled gracefully")
            return False
        return True

    def stop(self) -> None:
        if sys.platform != "win32" or self._thread is None:
            return
        import ctypes

        if self._thread_id:
            ctypes.windll.user32.PostThreadMessageW(self._thread_id, WM_QUIT, 0, 0)
        self._thread.join(timeout=2.0)
        self._thread = None

    def _run(self) -> None:
        import ctypes
        from ctypes import wintypes

        user32, kernel32 = ctypes.windll.user32, ctypes.windll.kernel32
        LRESULT = ctypes.c_ssize_t
        WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
        user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        user32.DefWindowProcW.restype = LRESULT

        class WNDCLASSW(ctypes.Structure):
            _fields_ = [("style", wintypes.UINT), ("lpfnWndProc", WNDPROC), ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int), ("hInstance", wintypes.HINSTANCE),
                        ("hIcon", wintypes.HICON), ("hCursor", wintypes.HANDLE), ("hbrBackground", wintypes.HBRUSH), ("lpszMenuName", wintypes.LPCWSTR), ("lpszClassName", wintypes.LPCWSTR)]

        def proc(hwnd, msg, wparam, lparam):
            if msg == WM_QUERYENDSESSION:
                return 1  # never veto: JARVIS has nothing that must block a shutdown
            if msg == WM_ENDSESSION and wparam:
                reason, detail = reason_for_end_session(lparam & 0xFFFFFFFF)
                self.ended = True
                try:
                    self._on_end(reason, detail)
                except Exception:  # noqa: BLE001
                    logger.exception("Session-end handler failed")
                self._done.wait(self._end_wait)  # hold Windows' termination until the runtime has released the microphone and port (bounded)
                return 0
            return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

        self._proc = WNDPROC(proc)  # keep a reference: the callback must outlive the window
        hinstance = kernel32.GetModuleHandleW(None)
        name = f"JarvisSessionWatcher{id(self)}"
        cls = WNDCLASSW()
        cls.lpfnWndProc, cls.hInstance, cls.lpszClassName = self._proc, hinstance, name
        user32.RegisterClassW.argtypes = [ctypes.POINTER(WNDCLASSW)]
        user32.CreateWindowExW.restype = wintypes.HWND
        user32.CreateWindowExW.argtypes = [wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                           wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID]
        if not user32.RegisterClassW(ctypes.byref(cls)):
            self._ready.set()
            return
        # A real (never shown) top-level window: message-only windows do not receive the broadcast session-end messages.
        hwnd = user32.CreateWindowExW(0, name, "JARVIS session watcher", 0, 0, 0, 0, 0, None, None, hinstance, None)
        if not hwnd:
            self._ready.set()
            return
        self.hwnd = int(hwnd)
        self._thread_id = kernel32.GetCurrentThreadId()
        self._ready.set()
        msg = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))
        user32.DestroyWindow(hwnd)
        user32.UnregisterClassW(name, hinstance)


def install_console_handler(on_event: Callable[[str, str], None]):
    """Console close / logoff / shutdown (manual runs from a terminal). Returns the handler object (keep it referenced) or None."""
    if sys.platform != "win32":
        return None
    import ctypes
    from ctypes import wintypes

    handler_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.DWORD)

    def handler(code: int) -> bool:
        if code in _CONSOLE_EVENTS:
            reason, detail = reason_for_console_event(code)
            on_event(reason, detail)
            return True
        return False

    fn = handler_type(handler)
    try:
        if not ctypes.windll.kernel32.SetConsoleCtrlHandler(fn, True):
            return None
    except Exception:  # noqa: BLE001
        return None
    return fn
