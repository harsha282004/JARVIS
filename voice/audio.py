"""Microphone input and speaker output, built on `sounddevice` (PortAudio).

Local only: audio is captured to an in-memory numpy buffer for the
duration of one utterance and is never persisted to disk or sent anywhere
except the local STT provider. Nothing here uploads audio or keeps a
rolling recording.
"""

from collections.abc import Iterator

import numpy as np
import sounddevice as sd

from voice.exceptions import AudioDeviceError

FRAME_SAMPLES = 1280  # 80ms at 16kHz — the chunk size openWakeWord expects


def _resolve_device(device: str) -> int | str | None:
    """Map an empty config value to "use the system default device"."""
    if not device:
        return None
    if device.isdigit():
        return int(device)
    return device


class AudioInput:
    """Blocking microphone reader, yielding fixed-size int16 mono frames."""

    def __init__(self, sample_rate: int = 16000, device: str = ""):
        self.sample_rate = sample_rate
        self._device = _resolve_device(device)
        self._stream: sd.InputStream | None = None

    @property
    def is_open(self) -> bool:
        return self._stream is not None

    def open(self) -> None:
        try:
            self._stream = sd.InputStream(
                samplerate=self.sample_rate,
                channels=1,
                dtype="int16",
                device=self._device,
                blocksize=FRAME_SAMPLES,
            )
            self._stream.start()
        except Exception as exc:  # noqa: BLE001 - PortAudio raises various backend errors
            self._stream = None
            raise AudioDeviceError(f"Could not open microphone input: {exc}") from exc

    def close(self) -> None:
        if self._stream is not None:
            self._stream.stop()
            self._stream.close()
            self._stream = None

    def __enter__(self) -> "AudioInput":
        self.open()
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    def read_frame(self) -> np.ndarray:
        """Read one FRAME_SAMPLES-length int16 mono frame from the microphone."""
        if self._stream is None:
            raise AudioDeviceError("AudioInput.read_frame() called before open()")
        data, overflowed = self._stream.read(FRAME_SAMPLES)
        return data[:, 0]

    def frames(self, duration_seconds: float) -> Iterator[np.ndarray]:
        """Yield frames covering approximately `duration_seconds` of audio."""
        total_frames = max(1, int(duration_seconds * self.sample_rate / FRAME_SAMPLES))
        for _ in range(total_frames):
            yield self.read_frame()


def list_input_devices() -> list[dict]:
    """Input devices as {index, name, default}. Names only; nothing is opened and no audio is read."""
    try:
        devices = sd.query_devices()
        default = sd.default.device[0]
    except Exception as exc:  # noqa: BLE001 - PortAudio raises various backend errors
        raise AudioDeviceError(f"Could not list audio devices: {exc}") from exc
    return [
        {"index": i, "name": d["name"], "default": i == default}
        for i, d in enumerate(devices)
        if d.get("max_input_channels", 0) > 0
    ]


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
