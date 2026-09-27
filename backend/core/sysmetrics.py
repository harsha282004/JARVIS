"""Process resource readings without extra dependencies (Windows working set via ctypes, CPU via process_time)."""

import ctypes
import os
import shutil
import sys
import threading
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


_cpu_meter = CpuMeter()
_cpu_lock = threading.Lock()
_cpu_meter.start()
_last_cpu_percent: float | None = None


def cpu_percent() -> float | None:
    """This process's CPU use (percent of one core) since the last call (or since startup, for the first call).
    Dashboard.html's System Resources card polls this every few seconds, which is exactly the window this measures --
    never a fabricated number, and never a blocking sleep in the request thread."""
    global _last_cpu_percent
    with _cpu_lock:
        try:
            _last_cpu_percent = _cpu_meter.stop()
        except Exception:  # noqa: BLE001 - a measurement failure must not break the dashboard; report the last known value
            pass
        finally:
            _cpu_meter.start()
    return _last_cpu_percent


def disk_usage_percent(path: str | None = None) -> dict | None:
    """Real disk usage (percent used, free GB) for the drive holding `path` (default: this project's own root, i.e.
    the drive JARVIS itself runs from). None if it cannot be read."""
    try:
        target = path or os.path.dirname(os.path.abspath(__file__))
        usage = shutil.disk_usage(target)
    except OSError:
        return None
    if usage.total <= 0:
        return None
    return {"percent_used": round(100.0 * usage.used / usage.total, 1), "free_gb": round(usage.free / (1024 ** 3), 1),
            "total_gb": round(usage.total / (1024 ** 3), 1)}
