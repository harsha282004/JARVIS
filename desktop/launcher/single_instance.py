"""Prevents two JARVIS runtimes (e.g. startup + manual launch) from fighting
over the microphone, using a per-session named Windows mutex."""

import sys

_MUTEX_NAME = "Local\\JARVIS_Runtime"
_ERROR_ALREADY_EXISTS = 183


class SingleInstanceGuard:
    def __init__(self, name: str = _MUTEX_NAME):
        self._name = name
        self._handle = None

    def acquire(self) -> bool:
        """Return True if this is the only instance. Always True off Windows."""
        if sys.platform != "win32":
            return True
        import ctypes

        kernel32 = ctypes.windll.kernel32
        self._handle = kernel32.CreateMutexW(None, False, self._name)
        if kernel32.GetLastError() == _ERROR_ALREADY_EXISTS:
            kernel32.CloseHandle(self._handle)
            self._handle = None
            return False
        return True

    def release(self) -> None:
        if self._handle is not None:
            import ctypes

            ctypes.windll.kernel32.CloseHandle(self._handle)
            self._handle = None


_EXIT_EVENT_NAME = r"Local\JARVIS_Exit"
_EVENT_MODIFY_STATE = 0x0002
_WAIT_OBJECT_0 = 0


class ExitSignal:
    """A named Windows event that lets `python -m desktop.launcher --stop` ask the running JARVIS to shut down gracefully
    (the tray icon's Exit path: microphone released, services stopped, connections closed), without killing the process.

    The running instance calls `wait_in_thread(callback)`; `send()` (from another process) signals it. Off Windows both do nothing."""

    def __init__(self, name: str = _EXIT_EVENT_NAME):
        self._name = name
        self._handle = None
        self._thread = None
        self._closing = False

    def wait_in_thread(self, callback) -> bool:
        if sys.platform != "win32":
            return False
        import ctypes
        import threading
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateEventW.restype = wintypes.HANDLE
        kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        handle = kernel32.CreateEventW(None, True, False, self._name)  # manual reset: a signal sent before we wait is not lost
        if not handle:
            return False
        self._handle = handle

        def wait() -> None:
            while not self._closing:
                if kernel32.WaitForSingleObject(handle, 500) == _WAIT_OBJECT_0:
                    callback()
                    return

        self._thread = threading.Thread(target=wait, name="jarvis-exit-signal", daemon=True)
        self._thread.start()
        return True

    def close(self) -> None:
        self._closing = True
        if self._thread is not None:
            self._thread.join(timeout=2)
            self._thread = None
        if self._handle is not None and sys.platform == "win32":
            import ctypes

            ctypes.WinDLL("kernel32").CloseHandle(self._handle)
        self._handle = None

    @classmethod
    def send(cls, name: str = _EXIT_EVENT_NAME) -> bool:
        """True if a running JARVIS was signalled, False if none is running."""
        if sys.platform != "win32":
            return False
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenEventW.restype = wintypes.HANDLE
        handle = kernel32.OpenEventW(_EVENT_MODIFY_STATE, False, name)
        if not handle:
            return False
        try:
            return bool(kernel32.SetEvent(handle))
        finally:
            kernel32.CloseHandle(handle)
