"""Speaker (output) device discovery, selection and validation (voice/speaker.py) over a scripted fake PortAudio
backend -- the output-side counterpart of tests/voice/test_microphone_device.py. No real device is opened."""

import numpy as np
import pytest

from voice.audio import AudioOutput
from voice.speaker import SpeakerDeviceManager


class FakeSD:
    """devices: list of (name, hostapi_index, max_out, default_rate)."""

    def __init__(self, devices, default_out=1, fail_open=(), wasapi_default=None):
        self.devices = [{"name": n, "hostapi": h, "max_output_channels": o, "default_samplerate": r} for n, h, o, r in devices]
        self.hostapis = [{"name": "MME", "default_output_device": default_out},
                          {"name": "Windows WASAPI", "default_output_device": wasapi_default if wasapi_default is not None else default_out},
                          {"name": "Windows WDM-KS", "default_output_device": -1}]
        self.default = type("D", (), {"device": (0, default_out)})()
        self.fail_open = set(fail_open)
        self.played = []

    def query_devices(self):
        return list(self.devices)

    def query_hostapis(self):
        return self.hostapis

    def check_output_settings(self, device, samplerate, channels):
        if device in self.fail_open:
            raise RuntimeError("device not connected")

    def play(self, samples, samplerate, device=None):
        self.played.append((samples, samplerate, device))

    def wait(self):
        pass


BLUETOOTH_INCIDENT = [
    ("Speakers (Realtek(R) Audio)", 0, 2, 44100),        # MME, index 0
    ("Headphones (Noise Buds R1)", 0, 2, 44100),         # MME, index 1 -- the paired Bluetooth headset
    ("Speakers (Realtek(R) Audio)", 1, 2, 48000),        # WASAPI, index 2
    ("Headphones (Noise Buds R1)", 1, 2, 48000),         # WASAPI, index 3 -- live default agrees with MME
]


def test_live_wasapi_default_is_preferred_over_the_cached_cross_api_default():
    """Root cause this exists for: a real machine where BOTH MME's cached default and WASAPI's live default agreed
    the configured output was a paired Bluetooth headset, not the laptop speakers -- this must be visible, not hidden."""
    sd = FakeSD(BLUETOOTH_INCIDENT, default_out=1, wasapi_default=3)
    mgr = SpeakerDeviceManager(sd)
    assert mgr._default_index() == 3
    mode, cands = mgr.candidates("auto")
    assert mode == "auto" and cands[0].name == "Headphones (Noise Buds R1)"


def test_a_device_that_fails_to_open_falls_back_to_the_next_real_device():
    """A Bluetooth sink that is paired but not connected typically fails check_output_settings outright."""
    sd = FakeSD(BLUETOOTH_INCIDENT, default_out=1, wasapi_default=3, fail_open={3, 1})
    sel = SpeakerDeviceManager(sd).resolve("auto")
    assert sel.status == "ready" and sel.device.name == "Speakers (Realtek(R) Audio)"
    assert any("Noise Buds" in t for t in sel.tried)   # the failed attempt is recorded, not silently skipped


def test_no_usable_output_device_reports_no_device():
    sd = FakeSD(BLUETOOTH_INCIDENT, default_out=1, wasapi_default=3, fail_open={0, 1, 2, 3})
    sel = SpeakerDeviceManager(sd).resolve("auto")
    assert sel.status == "error" and sel.device is None


def test_virtual_and_loopback_devices_are_never_auto_selected():
    sd = FakeSD([("Speakers (Realtek(R) Audio)", 0, 2, 44100), ("Stereo Mix (Realtek HD Audio)", 0, 2, 44100),
                 ("CABLE Input (VB-Audio Virtual Cable)", 0, 2, 44100)], default_out=0)
    mode, cands = SpeakerDeviceManager(sd).candidates("auto")
    assert all("Stereo Mix" not in c.name and "CABLE" not in c.name for c in cands)


@pytest.mark.parametrize("config,mode,first", [("", "auto", "Headphones (Noise Buds R1)"), ("2", "index", "Speakers (Realtek(R) Audio)"),
                                                ("Realtek", "name", "Speakers (Realtek(R) Audio)")])
def test_explicit_voice_output_device_configuration_wins(config, mode, first):
    sd = FakeSD(BLUETOOTH_INCIDENT, default_out=1, wasapi_default=3)
    got_mode, cands = SpeakerDeviceManager(sd).candidates(config)
    assert got_mode == mode and cands[0].name == first


def test_audio_output_resolves_and_plays_through_the_validated_device():
    sd = FakeSD(BLUETOOTH_INCIDENT, default_out=1, wasapi_default=3)
    out = AudioOutput(backend=sd)
    assert out.selection.status == "ready" and out.selection.device.name == "Headphones (Noise Buds R1)"
    out.play(np.zeros(10, dtype=np.float32), 22050)
    assert sd.played[0][2] == out.selection.device.index


def test_audio_output_falls_back_gracefully_when_the_backend_cannot_enumerate_devices():
    """A minimal test double (only .play()/.wait()) must not prevent AudioOutput from constructing or playing."""
    class MinimalBackend:
        def __init__(self):
            self.played = []

        def play(self, samples, samplerate, device=None):
            self.played.append((samples, samplerate, device))

        def wait(self):
            pass

    out = AudioOutput(backend=MinimalBackend())
    assert out.selection.status == "unknown" and out.selection.device is None
    out.play(np.zeros(5, dtype=np.float32), 22050)  # must not raise
    assert out._device is None


def test_reselect_re_runs_resolution():
    sd = FakeSD(BLUETOOTH_INCIDENT, default_out=1, wasapi_default=1)  # both agree: Realtek speakers this time
    out = AudioOutput(backend=sd)
    assert out.selection.device.name == "Headphones (Noise Buds R1)"
    sd.hostapis[1]["default_output_device"] = 0  # Windows default changed to Realtek speakers
    out.reselect()
    assert out.selection.device.name == "Speakers (Realtek(R) Audio)"
