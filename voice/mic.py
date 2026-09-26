"""Microphone device discovery, selection and validation (Windows audio aware). Metadata only: no audio is ever stored, logged or sent.

    settings.MICROPHONE_DEVICE  ("" / "auto" | an index | a device name)
      -> MicrophoneDeviceManager.candidates()   ordered, output-only and virtual devices removed
      -> AudioInput.open() tries each candidate: the target format first (16 kHz mono), then the device's native format with our own
         downmix/resample; a stream that opens but is digitally silent (all exact zeros) is skipped in favour of the next endpoint
      -> frames of FRAME_SAMPLES int16 mono at the pipeline rate -> wake word / VAD / STT (unchanged contract)

Windows exposes one physical microphone as several endpoints (MME, DirectSound, WASAPI, WDM-KS) whose names look alike; indexes change between boots. Nothing here
hard-codes an index: the default input is resolved at open time, and equivalent endpoints of the same name are tried as fallbacks.
"""

import re
from dataclasses import dataclass, field
from typing import Any

import numpy as np

_VIRTUAL = re.compile(r"stereo mix|what u hear|loopback|virtual|voicemeeter|cable (?:input|output)|sound mapper|primary sound", re.I)
_MIC_WORDS = re.compile(r"micro?phone|mic array|mic input|headset|webcam|array", re.I)
_HOSTAPI_ORDER = {"Windows WASAPI": 0, "MME": 1, "Windows DirectSound": 2, "Windows WDM-KS": 9}   # WDM-KS rejects the blocking API: last resort


@dataclass(frozen=True)
class InputDevice:
    index: int
    name: str
    hostapi: str
    max_input_channels: int
    default_samplerate: float
    is_default: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {"index": self.index, "name": self.name, "hostapi": self.hostapi, "channels": self.max_input_channels, "default_samplerate": self.default_samplerate,
                "default": self.is_default}


@dataclass
class MicSelection:
    """What was actually opened. Safe to log."""

    mode: str                     # auto | index | name
    device: InputDevice | None
    stream_rate: int = 0
    stream_channels: int = 0
    pipeline_rate: int = 16000
    resampled: bool = False
    status: str = "closed"        # ready | no_signal | closed | error
    tried: list[str] = field(default_factory=list)

    def describe(self) -> dict[str, Any]:
        d = self.device
        return {"mode": self.mode, "device": d.name if d else None, "host_api": d.hostapi if d else None, "sample_rate": self.stream_rate, "channels": self.stream_channels,
                "input_channels": d.max_input_channels if d else 0, "pipeline_rate": self.pipeline_rate, "resampled": self.resampled, "status": self.status}


def _norm(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", name.lower())


class MicrophoneDeviceManager:
    def __init__(self, backend: Any = None):
        if backend is None:
            import sounddevice as backend  # noqa: PLC0415 - only needed when a real microphone is used
        self._sd = backend

    # ---- enumeration ---------------------------------------------------------------------------------------------------------------

    def _default_index(self) -> int | None:
        try:
            idx = self._sd.default.device[0]
            return int(idx) if idx is not None and int(idx) >= 0 else None
        except Exception:  # noqa: BLE001
            return None

    def list_input_devices(self) -> list[InputDevice]:
        """Every device with input channels (output-only devices never appear). Names only; nothing is opened."""
        devices = self._sd.query_devices()
        apis = self._sd.query_hostapis()
        default = self._default_index()
        out = []
        for i, d in enumerate(devices):
            if int(d.get("max_input_channels", 0)) <= 0:
                continue
            api = apis[d["hostapi"]]["name"] if 0 <= d.get("hostapi", -1) < len(apis) else "?"
            out.append(InputDevice(i, str(d["name"]), api, int(d["max_input_channels"]), float(d.get("default_samplerate", 0) or 0), i == default))
        return out

    def get_default_input_device(self) -> InputDevice | None:
        default = self._default_index()
        return next((d for d in self.list_input_devices() if d.index == default), None)

    # ---- selection ------------------------------------------------------------------------------------------------------------------

    @staticmethod
    def parse_mode(config: str) -> tuple[str, str]:
        text = (config or "").strip()
        if text.lower() in ("", "auto", "default"):
            return "auto", ""
        return ("index", text) if text.isdigit() else ("name", text)

    def candidates(self, config: str = "") -> tuple[str, list[InputDevice]]:
        """(mode, ordered devices to try). Explicit configuration first; then the Windows default and the same-named endpoints of the other host APIs; then any
        other real microphone. Virtual mixes, the sound mapper and output-only devices are never chosen automatically."""
        mode, value = self.parse_mode(config)
        devices = self.list_input_devices()
        if not devices:
            return mode, []
        picked: list[InputDevice] = []

        def add(d: InputDevice) -> None:
            if d not in picked:
                picked.append(d)

        if mode == "index":
            hit = next((d for d in devices if d.index == int(value)), None)
            if hit is not None:
                add(hit)
        elif mode == "name":
            want = _norm(value)
            exact = [d for d in devices if _norm(d.name) == want]
            partial = [d for d in devices if want and want in _norm(d.name)]
            for d in sorted(exact or partial, key=lambda x: _HOSTAPI_ORDER.get(x.hostapi, 5)):
                add(d)
        if mode in ("auto",) or not picked:
            default = next((d for d in devices if d.is_default), None)
            if default is not None:
                add(default)
                # the same physical endpoint through the other host APIs (names truncate differently: compare a normalised prefix)
                stem = _norm(default.name)[:18]
                for d in sorted(devices, key=lambda x: _HOSTAPI_ORDER.get(x.hostapi, 5)):
                    if stem and _norm(d.name).startswith(stem):
                        add(d)
        for d in sorted(devices, key=lambda x: (_HOSTAPI_ORDER.get(x.hostapi, 5), x.index)):
            if not _VIRTUAL.search(d.name) and _MIC_WORDS.search(d.name):
                add(d)
        return mode, picked

    # ---- validation ------------------------------------------------------------------------------------------------------------------

    def can_open(self, device: InputDevice, rate: int, channels: int) -> bool:
        try:
            self._sd.check_input_settings(device=device.index, samplerate=rate, channels=channels, dtype="int16")
            return True
        except Exception:  # noqa: BLE001
            return False


def to_pipeline(block: np.ndarray, native_rate: int, target_rate: int) -> np.ndarray:
    """int16 (n, ch) or (n,) at the device rate -> int16 mono at the pipeline rate (channel mean, then polyphase-ish decimation / linear interpolation)."""
    x = block.astype(np.float32)
    if x.ndim == 2:
        x = x.mean(axis=1)
    if native_rate != target_rate:
        if native_rate % target_rate == 0:                       # 48000 -> 16000: average groups (a box low-pass), exact
            k = native_rate // target_rate
            n = (len(x) // k) * k
            x = x[:n].reshape(-1, k).mean(axis=1)
        else:
            n = max(1, round(len(x) * target_rate / native_rate))
            x = np.interp(np.linspace(0, len(x) - 1, n), np.arange(len(x)), x)
    return np.clip(x, -32768, 32767).astype(np.int16)


def level_stats(samples: np.ndarray) -> dict[str, float]:
    """min/max/peak/rms of int16 samples as fractions of full scale, plus the raw integer peak. Nothing else about the audio is kept."""
    if samples.size == 0:
        return {"min": 0.0, "max": 0.0, "peak": 0.0, "rms": 0.0, "peak_int": 0}
    x = samples.astype(np.float64)
    return {"min": float(x.min() / 32768.0), "max": float(x.max() / 32768.0), "peak": float(np.abs(x).max() / 32768.0), "rms": float(np.sqrt((x ** 2).mean()) / 32768.0),
            "peak_int": int(np.abs(samples.astype(np.int32)).max())}
