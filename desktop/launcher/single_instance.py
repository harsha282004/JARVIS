"""Prevents two JARVIS runtimes (e.g. startup + manual launch) from fighting
over the microphone, using a per-session named Windows mutex.

Both names are process-wide (any process on the login session that opens them can see/signal them), which is exactly
what makes "one real JARVIS at a time" work across a manual launch and a Task Scheduler launch. It also means a second,
independent JARVIS checkout or a diagnostic harness (`scripts/e2e_launcher_check.py`) MUST NOT share these names with a
person's real running instance: `--stop` would then shut down their live assistant instead of the throwaway one it
started. `JARVIS_INSTANCE_ID` gives such a caller its own namespace; a real launch (manual or Task Scheduler) never
sets it, so it always uses the shared default names."""

import os
import sys

_MUTEX_NAME = "Local\\JARVIS_Runtime"
_EXIT_EVENT_NAME = r"Local\JARVIS_Exit"
_ERROR_ALREADY_EXISTS = 183


def _scoped(base_name: str, instance_id: str | None) -> str:
    """`base_name`, or a private namespace for it: an explicit `instance_id`, else `JARVIS_INSTANCE_ID` from the
    environment (set only by a test/diagnostic harness), else `base_name` unchanged."""
    suffix = instance_id if instance_id is not None else os.environ.get("JARVIS_INSTANCE_ID", "")
    return f"{base_name}_{suffix}" if suffix else base_name


class SingleInstanceGuard:
    def __init__(self, name: str | None = None, instance_id: str | None = None):
        self._name = name if name is not None else _scoped(_MUTEX_NAME, instance_id)
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

    @classmethod
    def is_running(cls, name: str | None = None, instance_id: str | None = None) -> bool:
        """True if a JARVIS runtime of this login session currently holds the instance mutex (does not take it)."""
        if sys.platform != "win32":
            return False
        import ctypes
        from ctypes import wintypes

        name = name if name is not None else _scoped(_MUTEX_NAME, instance_id)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenMutexW.restype = wintypes.HANDLE
        handle = kernel32.OpenMutexW(0x00100000, False, name)  # SYNCHRONIZE
        if not handle:
            return False
        kernel32.CloseHandle(handle)
        return True

    def release(self) -> None:
        if self._handle is not None:
            import ctypes

            ctypes.windll.kernel32.CloseHandle(self._handle)
            self._handle = None


_EVENT_MODIFY_STATE = 0x0002
_WAIT_OBJECT_0 = 0


class ExitSignal:
    """A named Windows event that lets `python -m desktop.launcher --stop` ask the running JARVIS to shut down gracefully
    (the tray icon's Exit path: microphone released, services stopped, connections closed), without killing the process.

    The running instance calls `wait_in_thread(callback)`; `send()` (from another process) signals it. Off Windows both do nothing."""

    def __init__(self, name: str | None = None, instance_id: str | None = None):
        self._name = name if name is not None else _scoped(_EXIT_EVENT_NAME, instance_id)
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
    def send(cls, name: str | None = None, instance_id: str | None = None) -> bool:
        """True if a running JARVIS was signalled, False if none is running."""
        if sys.platform != "win32":
            return False
        import ctypes
        from ctypes import wintypes

        name = name if name is not None else _scoped(_EXIT_EVENT_NAME, instance_id)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenEventW.restype = wintypes.HANDLE
        handle = kernel32.OpenEventW(_EVENT_MODIFY_STATE, False, name)
        if not handle:
            return False
        try:
            return bool(kernel32.SetEvent(handle))
        finally:
            kernel32.CloseHandle(handle)
