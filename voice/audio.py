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


class AudioOutput:
    """Blocking speaker playback."""

    def __init__(self, device: str = ""):
        self._device = _resolve_device(device)

    def play(self, samples: np.ndarray, sample_rate: int) -> None:
        try:
            sd.play(samples, samplerate=sample_rate, device=self._device)
            sd.wait()
        except Exception as exc:  # noqa: BLE001 - PortAudio raises various backend errors
            raise AudioDeviceError(f"Could not play audio through speakers: {exc}") from exc
