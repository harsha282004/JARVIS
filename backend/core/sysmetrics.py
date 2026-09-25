"""Process resource readings without extra dependencies (Windows working set via ctypes, CPU via process_time)."""

import ctypes
import os
import sys
import time
from ctypes import wintypes


def working_set_mb() -> float | None:
    """Resident memory of this process in MB, or None if it cannot be read on this platform."""
    if sys.platform != "win32":
        try:
            import resource

            return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024
        except Exception:  # noqa: BLE001
            return None

    class Counters(ctypes.Structure):
        _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD), ("PeakWorkingSetSize", ctypes.c_size_t),
                    ("WorkingSetSize", ctypes.c_size_t), ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t), ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                    ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t)]

    counters = Counters()
    counters.cb = ctypes.sizeof(Counters)
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        kernel32.K32GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(Counters), wintypes.DWORD]
        kernel32.K32GetProcessMemoryInfo.restype = wintypes.BOOL
        ok = kernel32.K32GetProcessMemoryInfo(kernel32.GetCurrentProcess(), ctypes.byref(counters), counters.cb)
    except Exception:  # noqa: BLE001
        return None
    return counters.WorkingSetSize / (1024 * 1024) if ok else None


class CpuMeter:
    """Average CPU use of this process (percent of one core) between `start()` and `stop()`."""

    def start(self) -> None:
        self._cpu, self._wall = time.process_time(), time.perf_counter()

    def stop(self) -> float:
        wall = time.perf_counter() - self._wall
        return round(100.0 * (time.process_time() - self._cpu) / wall, 2) if wall > 0 else 0.0


def pid() -> int:
    return os.getpid()
