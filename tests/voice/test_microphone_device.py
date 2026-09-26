"""Microphone device discovery, selection, format handling, signal validation and reconnection (Windows audio behaviour) over a scripted fake PortAudio backend.
No real device is opened and no audio is stored or logged."""

import logging

import numpy as np
import pytest

from voice.audio import FRAME_SAMPLES, AudioInput
from voice.exceptions import AudioDeviceError
from voice.mic import MicrophoneDeviceManager, level_stats, to_pipeline


class FakeStream:
    def __init__(self, owner, device, samplerate, channels, blocksize, **kw):
        self.owner, self.device, self.rate, self.channels, self.block = owner, device, samplerate, channels, blocksize
        self.closed = False
        self.reads = 0

    def start(self):
        if self.device in self.owner.fail_open:
            raise RuntimeError("PaErrorCode -9999")

    def read(self, n):
        self.reads += 1
        if self.owner.dead:
            raise RuntimeError("device unplugged")
        amp = self.owner.amplitude.get(self.device, 100)
        data = (np.ones((n, self.channels)) * amp).astype(np.int16)
        return data, False

    def stop(self):
        pass

    def close(self):
        self.closed = True


class FakeSD:
    """devices: list of (name, hostapi_index, max_in, max_out, default_rate)."""

    def __init__(self, devices, default_in=1, native_only=(), amplitude=None, fail_open=()):
        self.devices = [{"name": n, "hostapi": h, "max_input_channels": i, "max_output_channels": o, "default_samplerate": r} for n, h, i, o, r in devices]
        self.hostapis = [{"name": "MME"}, {"name": "Windows WASAPI"}, {"name": "Windows WDM-KS"}]
        self.default = type("D", (), {"device": (default_in, 0)})()
        self.native_only = set(native_only)          # devices that reject 16 kHz (WASAPI shared mode)
        self.amplitude, self.fail_open = amplitude or {}, set(fail_open)
        self.dead = False
        self.streams: list[FakeStream] = []

    def query_devices(self):
        return list(self.devices)

    def query_hostapis(self):
        return self.hostapis

    def check_input_settings(self, device, samplerate, channels, dtype):
        d = self.devices[device]
        if d["max_input_channels"] < channels or (device in self.native_only and samplerate != int(d["default_samplerate"])):
            raise RuntimeError("Invalid sample rate")

    def InputStream(self, **kw):  # noqa: N802
        s = FakeStream(self, kw["device"], kw["samplerate"], kw["channels"], kw["blocksize"])
        self.streams.append(s)
        return s


REALTEK = [("Microsoft Sound Mapper - Input", 0, 2, 0, 44100), ("Microphone Array (Realtek(R) Au", 0, 2, 0, 44100), ("Speakers (Realtek)", 0, 0, 2, 44100),
           ("Microphone Array (Realtek(R) Audio)", 1, 2, 0, 48000), ("Stereo Mix (Realtek HD Audio Stereo input)", 2, 2, 0, 48000),
           ("Microphone Array (Realtek HD Audio Mic Array input)", 2, 2, 0, 44100)]


# ---- discovery ---------------------------------------------------------------------------------------------------------------------------

def test_lists_only_input_devices_with_metadata_and_default():
    m = MicrophoneDeviceManager(FakeSD(REALTEK))
    names = [d.name for d in m.list_input_devices()]
    assert "Speakers (Realtek)" not in names and len(names) == 5                     # output-only rejected
    default = m.get_default_input_device()
    assert default.index == 1 and default.hostapi == "MME" and default.is_default and default.max_input_channels == 2


def test_no_input_devices():
    m = MicrophoneDeviceManager(FakeSD([("Speakers", 0, 0, 2, 44100)], default_in=-1))
    assert m.list_input_devices() == [] and m.get_default_input_device() is None and m.candidates("")[1] == []
    with pytest.raises(AudioDeviceError, match="No microphone input device was found"):
        AudioInput(16000, "", backend=FakeSD([("Speakers", 0, 0, 2, 44100)], default_in=-1)).open()


def test_auto_prefers_default_then_same_named_endpoints_and_skips_virtual_devices():
    mode, cands = MicrophoneDeviceManager(FakeSD(REALTEK)).candidates("auto")
    assert mode == "auto" and cands[0].index == 1
    assert [c.index for c in cands][:2] == [1, 3]                                    # the same physical mic through WASAPI next
    assert all("Stereo Mix" not in c.name and "Sound Mapper" not in c.name for c in cands)


def test_duplicate_looking_devices_are_not_chosen_by_first_word_match():
    sd = FakeSD([("Microphone (USB Webcam)", 0, 1, 0, 16000), ("Microphone Array (Realtek(R) Audio)", 0, 2, 0, 44100)], default_in=1)
    assert MicrophoneDeviceManager(sd).candidates("")[1][0].index == 1                # the Windows default, not the first "Microphone"


@pytest.mark.parametrize("config,mode,first", [("", "auto", 1), ("auto", "auto", 1), ("3", "index", 3), ("Microphone Array (Realtek(R) Audio)", "name", 3), ("realtek hd audio mic array", "name", 5)])
def test_configuration_modes(config, mode, first):
    got_mode, cands = MicrophoneDeviceManager(FakeSD(REALTEK)).candidates(config)
    assert got_mode == mode and cands[0].index == first


def test_invalid_configured_device_falls_back_to_the_default_and_says_so_when_nothing_matches():
    m = MicrophoneDeviceManager(FakeSD(REALTEK))
    assert m.candidates("99")[1][0].index == 1 and m.candidates("no such microphone")[1][0].index == 1     # falls back to the default rather than failing
    assert m.candidates("Speakers (Realtek)")[1][0].index == 1                       # an output-only name never matches


# ---- streams: formats and fallbacks -------------------------------------------------------------------------------------------------------

def test_opens_default_at_the_pipeline_format_and_reads_int16_mono_frames():
    sd = FakeSD(REALTEK)
    mic = AudioInput(16000, "", backend=sd)
    mic.open()
    frame = mic.read_frame()
    assert frame.shape == (FRAME_SAMPLES,) and frame.dtype == np.int16
    d = mic.selection.describe()
    assert d["device"].startswith("Microphone Array") and d["host_api"] == "MME" and d["sample_rate"] == 16000 and d["status"] == "ready" and not d["resampled"]
    mic.close()
    assert mic.selection.status == "closed" and sd.streams[-1].closed


def test_native_format_is_downmixed_and_resampled_when_16k_is_rejected():
    sd = FakeSD(REALTEK, default_in=3, native_only={3})
    mic = AudioInput(16000, "", backend=sd)
    mic.open()
    d = mic.selection.describe()
    assert d["sample_rate"] == 48000 and d["channels"] == 2 and d["resampled"] and d["host_api"] == "Windows WASAPI"
    frames = [mic.read_frame() for _ in range(5)]
    assert all(f.shape == (FRAME_SAMPLES,) and f.dtype == np.int16 for f in frames) and int(frames[0][0]) == 100     # constant signal survives 48k -> 16k
    assert sd.streams[-1].block == FRAME_SAMPLES * 3                                 # 80 ms of 48 kHz per frame


def test_backend_failure_falls_through_to_the_next_endpoint():
    sd = FakeSD(REALTEK, fail_open={1})
    mic = AudioInput(16000, "", backend=sd)
    mic.open()
    assert mic.selection.device.index == 3 and any("MME" in t for t in mic.selection.tried)


def test_all_endpoints_failing_gives_a_clear_error_with_what_was_tried():
    sd = FakeSD(REALTEK, fail_open={1, 3, 5})
    with pytest.raises(AudioDeviceError, match="Could not open any microphone. Tried: Microphone Array"):
        AudioInput(16000, "", backend=sd).open()


# ---- signal validation --------------------------------------------------------------------------------------------------------------------

def test_a_digitally_silent_endpoint_is_skipped_for_one_with_signal():
    sd = FakeSD(REALTEK, amplitude={1: 0, 3: 7})
    mic = AudioInput(16000, "", backend=sd)
    mic.open()
    assert mic.selection.device.index == 3 and mic.selection.status == "ready"


def test_all_silent_still_opens_but_reports_no_signal():
    sd = FakeSD(REALTEK, amplitude={1: 0, 3: 0, 5: 0})
    mic = AudioInput(16000, "", backend=sd)
    mic.open()
    assert mic.selection.status == "no_signal" and mic.is_open


def test_level_stats_distinguish_zero_near_zero_and_real_signals():
    assert level_stats(np.zeros(1600, dtype=np.int16))["peak_int"] == 0
    quiet = level_stats(np.array([1, -1] * 800, dtype=np.int16))
    assert quiet["peak_int"] == 1 and 0 < quiet["peak"] < 0.0001
    loud = level_stats((np.sin(np.linspace(0, 200, 16000)) * 12000).astype(np.int16))
    assert loud["peak"] > 0.3 and loud["rms"] > 0.2 and loud["min"] < 0 < loud["max"]
    assert level_stats(np.array([], dtype=np.int16))["peak_int"] == 0


def test_resampler_shapes_and_values():
    x = np.tile(np.array([[10, 30]], dtype=np.int16), (4800, 1))
    y = to_pipeline(x, 48000, 16000)
    assert y.shape == (1600,) and y.dtype == np.int16 and int(y[0]) == 20            # stereo mean, 3:1 decimation
    z = to_pipeline(np.full((4410, 1), 50, dtype=np.int16), 44100, 16000)
    assert abs(len(z) - 1600) <= 1 and int(z[10]) == 50


# ---- reconnection -------------------------------------------------------------------------------------------------------------------------

def test_disconnect_raises_and_reopening_re_resolves_the_device():
    sd = FakeSD(REALTEK)
    mic = AudioInput(16000, "", backend=sd)
    mic.open()
    sd.dead = True
    with pytest.raises(RuntimeError):
        mic.read_frame()                                                             # the engine's existing microphone-error path handles this
    mic.close()
    with pytest.raises(AudioDeviceError):
        AudioInput(16000, "", backend=_all_dead(sd)).open()                          # still unplugged: a clear error, no crash
    sd.dead = False
    sd.devices[1]["name"], sd.devices[3]["name"] = "Headset Microphone (USB)", "Headset Microphone (USB)"       # replugged as something else
    mic.open()
    assert mic.is_open and mic.selection.device.index == 1 and mic.selection.status == "ready"


def _all_dead(sd):
    sd.dead = True
    return sd


# ---- privacy ------------------------------------------------------------------------------------------------------------------------------

def test_logs_are_metadata_only_and_nothing_is_written(tmp_path, caplog, monkeypatch):
    monkeypatch.chdir(tmp_path)
    caplog.set_level(logging.INFO)
    mic = AudioInput(16000, "", backend=FakeSD(REALTEK, amplitude={1: 77}))
    mic.open()
    mic.read_frame()
    mic.close()
    text = " ".join(r.getMessage() for r in caplog.records)
    assert "Microphone:" in text and "Realtek" in text and "77" not in text.replace("16000", "")
    assert not list(tmp_path.iterdir())


def test_the_audio_modules_never_touch_the_network_or_disk():
    from pathlib import Path

    for name in ("audio.py", "mic.py"):
        src = (Path(__file__).resolve().parents[2] / "voice" / name).read_text(encoding="utf-8")
        for forbidden in ("import socket", "requests", "httpx", "urllib", ".write(", "wave", "soundfile", "np.save", "tofile"):
            assert forbidden not in src, (name, forbidden)
        assert not __import__("re").search(r"(?<![\w.])open\(", src.replace("def open(", "").replace("before open()", "")), name       # the builtin open(): no file is ever opened
