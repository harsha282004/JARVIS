"""Microphone input and speaker output, built on `sounddevice` (PortAudio).

Local only: audio is captured to an in-memory numpy buffer for the
duration of one utterance and is never persisted to disk or sent anywhere
except the local STT provider. Nothing here uploads audio or keeps a
rolling recording.
"""

from collections.abc import Iterator

import numpy as np
import sounddevice as sd

from backend.core.logging import get_logger
from voice.exceptions import AudioDeviceError
from voice.mic import InputDevice, MicSelection, MicrophoneDeviceManager, to_pipeline

logger = get_logger(__name__)

FRAME_SAMPLES = 1280  # 80ms at 16kHz — the chunk size openWakeWord expects


def _resolve_device(device: str) -> int | str | None:
    """Map an empty config value to "use the system default device"."""
    if not device:
        return None
    if device.isdigit():
        return int(device)
    return device


class AudioInput:
    """Blocking microphone reader, yielding fixed-size int16 mono frames at `sample_rate`.

    Device choice is delegated to `MicrophoneDeviceManager` (default Windows input, same-named endpoints of other host APIs, then any real microphone; an explicit
    index or name in MICROPHONE_DEVICE wins). A stream that opens but delivers only digital zeros is skipped in favour of the next endpoint. If a device cannot open
    at the pipeline format it is opened at its native rate/channels and downmixed/resampled here, once.
    """

    PROBE_BLOCKS = 4

    def __init__(self, sample_rate: int = 16000, device: str = "", backend=None):
        self.sample_rate = sample_rate
        self._config = device
        self._backend = backend
        self._manager = None
        self._stream = None
        self._native_rate = sample_rate
        self._native = False
        self._buffer = np.zeros(0, dtype=np.int16)
        self.selection = MicSelection("auto", None, pipeline_rate=sample_rate)

    @property
    def is_open(self) -> bool:
        return self._stream is not None

    def _sd(self):
        return self._backend if self._backend is not None else sd

    def manager(self) -> MicrophoneDeviceManager:
        if self._manager is None:
            self._manager = MicrophoneDeviceManager(self._sd())
        return self._manager

    def _plans(self, dev: InputDevice) -> list[tuple[int, int, bool]]:
        mgr = self.manager()
        plans: list[tuple[int, int, bool]] = []
        if mgr.can_open(dev, self.sample_rate, 1):
            plans.append((self.sample_rate, 1, False))
        native = int(dev.default_samplerate) or 48000
        for ch in sorted({min(2, dev.max_input_channels), 1}, reverse=True):
            if mgr.can_open(dev, native, ch):
                plans.append((native, ch, True))
                break
        return plans

    def _block(self, native: bool, rate: int) -> int:
        return FRAME_SAMPLES if not native else max(1, FRAME_SAMPLES * rate // self.sample_rate)

    def _open_stream(self, dev: InputDevice, rate: int, channels: int, native: bool):
        stream = self._sd().InputStream(samplerate=rate, channels=channels, dtype="int16", device=dev.index, blocksize=self._block(native, rate))
        stream.start()
        return stream

    def _read_raw(self, stream, native: bool, rate: int):
        data, _ = stream.read(self._block(native, rate))
        return data

    def open(self) -> None:
        self.close()
        try:
            mode, candidates = self.manager().candidates(self._config)
        except Exception as exc:  # noqa: BLE001 - PortAudio raises various backend errors
            raise AudioDeviceError(f"Could not list audio devices: {exc}") from exc
        sel = MicSelection(mode, None, pipeline_rate=self.sample_rate)
        if not candidates:
            self.selection = sel
            hint = f" (MICROPHONE_DEVICE={self._config!r} matched nothing)" if mode != "auto" else ""
            raise AudioDeviceError(f"No microphone input device was found{hint}. Check Windows Settings > Sound > Input and the microphone privacy switch.")
        silent = None
        for dev in candidates:
            for rate, channels, native in self._plans(dev):
                sel.tried.append(f"{dev.name} [{dev.hostapi}] {rate} Hz/{channels} ch")
                try:
                    stream = self._open_stream(dev, rate, channels, native)
                except Exception:  # noqa: BLE001 - try the next format / endpoint
                    continue
                try:
                    peak = 0
                    for _ in range(self.PROBE_BLOCKS):
                        peak = max(peak, int(np.abs(self._read_raw(stream, native, rate).astype(np.int32)).max()))
                except Exception:  # noqa: BLE001
                    self._close_quietly(stream)
                    continue
                if peak == 0:                                   # digital silence: a live microphone is never exactly zero
                    self._close_quietly(stream)
                    silent = silent or (dev, rate, channels, native)
                    continue
                self._adopt(stream, dev, rate, channels, native, sel, "ready")
                return
        if silent is not None:
            dev, rate, channels, native = silent
            try:
                self._adopt(self._open_stream(dev, rate, channels, native), dev, rate, channels, native, sel, "no_signal")
                return
            except Exception:  # noqa: BLE001
                pass
        sel.status = "error"
        self.selection = sel
        raise AudioDeviceError("Could not open any microphone. Tried: " + "; ".join(sel.tried[:6]))

    def _adopt(self, stream, dev, rate, channels, native, sel: MicSelection, status: str) -> None:
        self._stream, self._native, self._native_rate = stream, native, rate
        self._buffer = np.zeros(0, dtype=np.int16)
        sel.device, sel.stream_rate, sel.stream_channels, sel.resampled, sel.status = dev, rate, channels, native, status
        self.selection = sel
        log = logger.info if status == "ready" else logger.warning
        log("Microphone: %s", sel.describe())                 # metadata only: never audio

    @staticmethod
    def _close_quietly(stream) -> None:
        try:
            stream.stop()
            stream.close()
        except Exception:  # noqa: BLE001
            pass

    def close(self) -> None:
        if self._stream is not None:
            self._close_quietly(self._stream)
            self._stream = None
            if self.selection.status in ("ready", "no_signal"):
                self.selection.status = "closed"

    def __enter__(self) -> "AudioInput":
        self.open()
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    def read_frame(self) -> np.ndarray:
        """Read one FRAME_SAMPLES-length int16 mono frame at the pipeline rate."""
        if self._stream is None:
            raise AudioDeviceError("AudioInput.read_frame() called before open()")
        if not self._native:
            data, _overflowed = self._stream.read(FRAME_SAMPLES)
            return data[:, 0]
        while len(self._buffer) < FRAME_SAMPLES:
            self._buffer = np.concatenate([self._buffer, to_pipeline(self._read_raw(self._stream, True, self._native_rate), self._native_rate, self.sample_rate)])
        frame, self._buffer = self._buffer[:FRAME_SAMPLES], self._buffer[FRAME_SAMPLES:]
        return frame

    def frames(self, duration_seconds: float) -> Iterator[np.ndarray]:
        """Yield frames covering approximately `duration_seconds` of audio."""
        total_frames = max(1, int(duration_seconds * self.sample_rate / FRAME_SAMPLES))
        for _ in range(total_frames):
            yield self.read_frame()


def list_input_devices() -> list[dict]:
    """Input devices as {index, name, default, hostapi, channels, default_samplerate}. Nothing is opened and no audio is read."""
    try:
        return [{"index": d.index, "name": d.name, "default": d.is_default, "hostapi": d.hostapi, "channels": d.max_input_channels, "default_samplerate": d.default_samplerate}
                for d in MicrophoneDeviceManager(sd).list_input_devices()]
    except Exception as exc:  # noqa: BLE001 - PortAudio raises various backend errors
        raise AudioDeviceError(f"Could not list audio devices: {exc}") from exc


class AudioOutput:
    """Speaker playback that can be stopped at any moment (barge-in).

    `play()` blocks until finished (or `stop()`); `start()` returns immediately so the caller can keep listening while
    JARVIS talks. `volume` (0..1) is applied to the samples, so it works for any TTS engine.
    """

    def __init__(self, device: str = "", volume: float = 1.0):
        self._device = _resolve_device(device)
        self.volume = volume

    def _scaled(self, samples: np.ndarray) -> np.ndarray:
        if self.volume >= 0.999:
            return samples
        if samples.dtype == np.int16:
            return (samples.astype(np.float32) * self.volume).astype(np.int16)
        return samples.astype(np.float32) * self.volume

    def start(self, samples: np.ndarray, sample_rate: int) -> None:
        try:
            sd.play(self._scaled(samples), samplerate=sample_rate, device=self._device)
        except Exception as exc:  # noqa: BLE001 - PortAudio raises various backend errors
            raise AudioDeviceError(f"Could not play audio through speakers: {exc}") from exc

    @property
    def is_playing(self) -> bool:
        try:
            stream = sd.get_stream()
            return bool(stream.active)
        except Exception:  # noqa: BLE001 - no stream yet / already closed
            return False

    def stop(self) -> None:
        """Silence the speakers immediately. Safe to call when nothing is playing."""
        try:
            sd.stop()
        except Exception:  # noqa: BLE001 - stopping must never raise
            pass

    def play(self, samples: np.ndarray, sample_rate: int) -> None:
        self.start(samples, sample_rate)
        try:
            sd.wait()
        except Exception as exc:  # noqa: BLE001
            raise AudioDeviceError(f"Could not play audio through speakers: {exc}") from exc
