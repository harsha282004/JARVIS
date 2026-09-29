"""Speaker (output) device discovery, selection and validation (Windows audio aware) -- the output-side counterpart
of `voice/mic.py`'s microphone resolution, built for the same real reason: `sounddevice`'s/PortAudio's "default
device" is not always what a person would call their real speakers.

Root cause this exists for: measured on a real machine, `sd.default.device` (and even the Windows-WASAPI host API's
own live default, `sd.query_hostapis()[wasapi]['default_output_device']`) resolved to a **paired Bluetooth headset**
(a real, current Windows-configured default output), not the laptop's built-in speakers. JARVIS's microphone was
correctly on the built-in array the whole time (`voice/mic.py` already does this validation for input) -- only the
speaker side blindly trusted whatever index `sounddevice` handed it, with no check that it actually opens, and no
visibility into what it even was. A Bluetooth sink that is paired but not actively connected/awake has real wake-up
latency (its low-power link mode has to renegotiate before audio flows) that can eat an entire short acknowledgement
like "Yes?" while barely denting a multi-second reply -- exactly the reported symptom.

This module does NOT hard-code a preference for "the laptop's speakers": it validates whatever the resolved device is
(via `sounddevice.check_output_settings`, the same real stream-negotiation Windows/PortAudio itself would do) and
only falls back to another real device when the first choice actually fails to open. What it adds over blind trust is
visibility (the resolved device is logged and reported on `/voice`, exactly like the microphone already is) and an
explicit override (`VOICE_OUTPUT_DEVICE`, mirroring `MICROPHONE_DEVICE`) so a person whose Windows default really is
a sometimes-disconnected wireless device can pin JARVIS to a wired one without any code change.
"""

import re
from dataclasses import dataclass, field
from typing import Any

_VIRTUAL = re.compile(r"stereo mix|what u hear|loopback|virtual|voicemeeter|cable (?:input|output)|sound mapper|primary sound", re.I)
_HOSTAPI_ORDER = {"Windows WASAPI": 0, "MME": 1, "Windows DirectSound": 2, "Windows WDM-KS": 9}


@dataclass(frozen=True)
class OutputDevice:
    index: int
    name: str
    hostapi: str
    max_output_channels: int
    default_samplerate: float
    is_default: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {"index": self.index, "name": self.name, "hostapi": self.hostapi, "channels": self.max_output_channels,
                "default_samplerate": self.default_samplerate, "default": self.is_default}


@dataclass
class SpeakerSelection:
    """What output device is actually in use. Safe to log (name/host-api/rate only, never audio)."""

    mode: str                      # auto | index | name
    device: OutputDevice | None
    stream_rate: int = 0
    channels: int = 0
    status: str = "unknown"        # ready | no_device | error
    tried: list[str] = field(default_factory=list)

    def describe(self) -> dict[str, Any]:
        d = self.device
        return {"mode": self.mode, "device": d.name if d else None, "host_api": d.hostapi if d else None,
                "sample_rate": self.stream_rate, "channels": self.channels, "status": self.status}


def _norm(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", name.lower())


class SpeakerDeviceManager:
    def __init__(self, backend: Any = None):
        if backend is None:
            import sounddevice as backend  # noqa: PLC0415 - only needed when real playback is used
        self._sd = backend

    # ---- enumeration ---------------------------------------------------------------------------------------------------------------

    def _default_index(self) -> int | None:
        """The live WASAPI default if available (the host API Windows itself manages the current default playback
        device through) else sounddevice's own cross-API default -- never a cached value from process start."""
        try:
            apis = self._sd.query_hostapis()
            wasapi = next((a for a in apis if a["name"] == "Windows WASAPI"), None)
            if wasapi is not None and wasapi.get("default_output_device", -1) >= 0:
                return int(wasapi["default_output_device"])
        except Exception:  # noqa: BLE001
            pass
        try:
            idx = self._sd.default.device[1]
            return int(idx) if idx is not None and int(idx) >= 0 else None
        except Exception:  # noqa: BLE001
            return None

    def list_output_devices(self) -> list[OutputDevice]:
        devices = self._sd.query_devices()
        apis = self._sd.query_hostapis()
        default = self._default_index()
        out = []
        for i, d in enumerate(devices):
            if int(d.get("max_output_channels", 0)) <= 0:
                continue
            api = apis[d["hostapi"]]["name"] if 0 <= d.get("hostapi", -1) < len(apis) else "?"
            out.append(OutputDevice(i, str(d["name"]), api, int(d["max_output_channels"]), float(d.get("default_samplerate", 0) or 0), i == default))
        return out

    # ---- selection ------------------------------------------------------------------------------------------------------------------

    @staticmethod
    def parse_mode(config: str) -> tuple[str, str]:
        text = (config or "").strip()
        if text.lower() in ("", "auto", "default"):
            return "auto", ""
        return ("index", text) if text.isdigit() else ("name", text)

    def candidates(self, config: str = "") -> tuple[str, list[OutputDevice]]:
        """(mode, ordered devices to try). Explicit configuration first; then the live Windows default and the
        same-named endpoints of the other host APIs; then any other real output device. The sound mapper and
        loopback/virtual mixes are never chosen automatically."""
        mode, value = self.parse_mode(config)
        devices = self.list_output_devices()
        if not devices:
            return mode, []
        picked: list[OutputDevice] = []

        def add(d: OutputDevice) -> None:
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
        if mode == "auto" or not picked:
            default = next((d for d in devices if d.is_default), None)
            if default is not None:
                add(default)
                stem = _norm(default.name)[:18]
                for d in sorted(devices, key=lambda x: _HOSTAPI_ORDER.get(x.hostapi, 5)):
                    if stem and _norm(d.name).startswith(stem):
                        add(d)
        for d in sorted(devices, key=lambda x: (_HOSTAPI_ORDER.get(x.hostapi, 5), x.index)):
            if not _VIRTUAL.search(d.name):
                add(d)
        return mode, picked

    # ---- validation ------------------------------------------------------------------------------------------------------------------

    def can_open(self, device: OutputDevice, rate: int, channels: int = 1) -> bool:
        try:
            self._sd.check_output_settings(device=device.index, samplerate=rate, channels=min(channels, device.max_output_channels) or 1)
            return True
        except Exception:  # noqa: BLE001
            return False

    def resolve(self, config: str, rate: int = 22050) -> SpeakerSelection:
        """The device to actually play through: the first candidate (explicit config, else the live Windows default
        and its equivalents, else any other real output) that genuinely accepts a stream at `rate`. A device that is
        paired but not connected typically fails this check outright; one that opens but has real link latency
        (a Bluetooth sink coming out of a low-power sniff state) cannot be detected this way -- that is a real,
        documented limitation (see docs/VOICE.md), not something a settings check can see."""
        mode, candidates = self.candidates(config)
        sel = SpeakerSelection(mode, None)
        for d in candidates:
            sel.tried.append(f"{d.name} [{d.hostapi}]")
            if self.can_open(d, rate, 1):
                sel.device, sel.stream_rate, sel.channels, sel.status = d, rate, min(1, d.max_output_channels) or 1, "ready"
                return sel
        sel.status = "no_device" if not candidates else "error"
        return sel
