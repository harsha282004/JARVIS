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


_WARMUP_SECONDS = 0.12       # long enough for Windows/WASAPI to spin up a shared-mode stream once, short enough to be inaudible
_WARMUP_AMPLITUDE = 0.0005   # near-silent: this primes the device, it is not meant to be heard
_MAX_GAIN = 2.0              # VOICE_TTS_VOLUME above 1.0 amplifies (soft-clipped below); this is the ceiling
_SOFT_CLIP_KNEE = 0.9        # samples above this fraction of full scale are compressed rather than hard-clipped


def _soft_clip(samples: np.ndarray) -> np.ndarray:
    """tanh-based soft clipper above `_SOFT_CLIP_KNEE`: a gain that would otherwise clip harshly is rounded off
    instead, so raising VOICE_TTS_VOLUME cannot produce the sharp digital distortion a hard clip would."""
    knee = _SOFT_CLIP_KNEE
    over = np.abs(samples) > knee
    if not np.any(over):
        return samples
    out = samples.copy()
    sign = np.sign(out[over])
    excess = (np.abs(out[over]) - knee) / (1.0 - knee)
    out[over] = sign * (knee + (1.0 - knee) * np.tanh(excess))
    return out


_LEAD_SILENCE_SECONDS = 0.15   # protects the first phoneme of a short utterance ("Yes?") from a device/driver cold-start
# Measured on a real machine: MME's own reported default_high_output_latency was 0.18 s for the resolved output
# device. The original 0.08 s tail was comfortably shorter than that -- not long enough to survive a real driver of
# that latency tearing down/finishing the stream. 0.22 s clears the measured figure with margin and is still
# inaudible (true silence, not noise).
_TAIL_SILENCE_SECONDS = 0.22


def pad_utterance(samples: np.ndarray, sample_rate: int, lead_seconds: float = _LEAD_SILENCE_SECONDS,
                  tail_seconds: float = _TAIL_SILENCE_SECONDS) -> np.ndarray:
    """A short run of true digital silence before and after `samples`.

    Root cause this exists for: Piper's own output already peaks at 0 dBFS for every utterance measured, including
    "Yes?" (confirmed: it does not need more gain) -- a short acknowledgement sounding clipped/incomplete is a
    playback-path artifact (the OS audio stack's first and last few dozen milliseconds of any freshly opened output
    stream are the least reliable part of it, and a 150 ms clip like "Yes?" has almost no margin to lose there, while
    a multi-second reply barely notices). `AudioOutput.warm_up()` already primes the stream once before the first
    real utterance of an activation; this pad protects every individual utterance directly, in the exact buffer
    handed to the audio driver, which cannot be defeated by timing between separate calls the way a one-off warm-up
    could be. The padding is true zeros, not the tiny warm-up noise floor, so it adds no audible hiss."""
    if lead_seconds <= 0 and tail_seconds <= 0:
        return samples
    lead = np.zeros(int(lead_seconds * sample_rate), dtype=samples.dtype)
    tail = np.zeros(int(tail_seconds * sample_rate), dtype=samples.dtype)
    return np.concatenate([lead, samples, tail]) if samples.size else samples


class AudioOutput:
    """Speaker playback that can be stopped at any moment (barge-in).

    `play()` blocks until finished (or `stop()`); `start()` returns immediately so the caller can keep listening while
    JARVIS talks. `volume` applies to every sample so it works for any TTS engine: 0..1 attenuates; above 1.0 (up to
    `_MAX_GAIN`) amplifies a quiet voice model's output, soft-clipped so it never produces harsh digital distortion.

    Piper already peaks each utterance near 0 dBFS (measured: "Yes?" and a five-second sentence both peak at 1.0), so a
    quiet-sounding short acknowledgement is not a gain problem. It is much more often a COLD START: `sd.play()` opens a
    fresh output stream on first use, and on Windows/WASAPI that stream's first ~100-200 ms can be lost while the audio
    engine spins up -- a cost a five-second sentence barely notices but that can swallow most of a 150 ms "Yes?". Call
    `warm_up()` once, before any real speech, to pay that cost on a silent trial clip instead of on the first real word.
    """

    def __init__(self, device: str = "", volume: float = 1.0, backend=None):
        self._config = device
        self.volume = volume
        self._sd = backend if backend is not None else sd
        self._warmed = False
        self.selection = self._resolve()

    def _resolve(self):
        """Real device resolution (voice/speaker.py), not blind trust in whatever index `sounddevice` hands back --
        see that module's docstring for the real Bluetooth-default-output incident this exists because of. Logged
        exactly like the microphone's own resolution (`voice.audio` "Microphone: ..."), so the actual output device
        is always visible, never silently assumed. Re-run on demand via `reselect()` (e.g. after a device change).
        A backend that cannot enumerate devices (a minimal test double, or a real but unusual PortAudio build) falls
        back to `device=None` -- exactly the old "let sounddevice pick" behavior -- rather than failing to construct."""
        from voice.speaker import SpeakerDeviceManager, SpeakerSelection

        try:
            selection = SpeakerDeviceManager(self._sd).resolve(self._config)
        except Exception:  # noqa: BLE001 - device resolution must never prevent AudioOutput from existing
            return SpeakerSelection("auto", None, status="unknown")
        if selection.device is not None:
            logger.info("Speaker: %s", selection.describe())
        else:
            logger.warning("No usable audio output device found (tried: %s)", "; ".join(selection.tried[:6]) or "none")
        return selection

    def reselect(self) -> None:
        """Re-run output device resolution (e.g. after Windows reports a device change). Safe to call anytime."""
        self.selection = self._resolve()

    @property
    def _device(self):
        return self.selection.device.index if self.selection.device is not None else None

    def warm_up(self) -> None:
        """Play a near-silent, brief clip so the OS output stream is already open by the time real speech is spoken.
        Best-effort and idempotent: a failure here only means the first real utterance pays the cold-start cost, same
        as before this existed; it never raises and never blocks longer than the clip itself."""
        if self._warmed:
            return
        try:
            samples = (np.random.default_rng(0).uniform(-1.0, 1.0, int(_WARMUP_SECONDS * 22050)).astype(np.float32) * _WARMUP_AMPLITUDE)
            self._sd.play(samples, samplerate=22050, device=self._device)
            self._sd.wait()
            self._warmed = True  # only on success: a failed attempt (device not ready yet) may still succeed next cycle
        except Exception:  # noqa: BLE001 - warm-up is an optimization, never a requirement
            logger.warning("Audio output warm-up failed; the first spoken reply may be quieter than usual")

    def _scaled(self, samples: np.ndarray) -> np.ndarray:
        gain = min(max(self.volume, 0.0), _MAX_GAIN)
        if abs(gain - 1.0) < 1e-6:
            return samples
        if samples.dtype == np.int16:
            scaled = samples.astype(np.float32) / 32767.0 * gain
            return (_soft_clip(scaled) * 32767.0).astype(np.int16)
        scaled = samples.astype(np.float32) * gain
        return _soft_clip(scaled) if gain > 1.0 else scaled

    def start(self, samples: np.ndarray, sample_rate: int) -> None:
        try:
            self._sd.play(self._scaled(samples), samplerate=sample_rate, device=self._device)
        except Exception as exc:  # noqa: BLE001 - PortAudio raises various backend errors
            raise AudioDeviceError(f"Could not play audio through speakers: {exc}") from exc

    @property
    def is_playing(self) -> bool:
        try:
            stream = self._sd.get_stream()
            return bool(stream.active)
        except Exception:  # noqa: BLE001 - no stream yet / already closed
            return False

    def stop(self) -> None:
        """Silence the speakers immediately. Safe to call when nothing is playing."""
        try:
            self._sd.stop()
        except Exception:  # noqa: BLE001 - stopping must never raise
            pass

    def play(self, samples: np.ndarray, sample_rate: int) -> None:
        self.start(samples, sample_rate)
        try:
            self._sd.wait()
        except Exception as exc:  # noqa: BLE001
            raise AudioDeviceError(f"Could not play audio through speakers: {exc}") from exc
