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
