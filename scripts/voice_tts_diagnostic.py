"""Measure real TTS audio (Piper) through the exact production buffer path -- synthesis, `pad_utterance()`, and the
same gain/soft-clip stage `AudioOutput` applies -- to answer, with numbers, whether a short utterance like the wake
acknowledgement "Yes?" is actually too short/quiet/clipped, *before* changing anything. No microphone is used.

    python scripts/voice_tts_diagnostic.py --text "Yes?"
    python scripts/voice_tts_diagnostic.py --text "Yes?" --play                 # REAL playback through the real
                                                                                  # resolved output device (see
                                                                                  # voice/speaker.py); you must
                                                                                  # actually listen -- this script
                                                                                  # cannot hear it for you.
    python scripts/voice_tts_diagnostic.py --compare-phrases                    # "Yes?" vs longer/alternate acks
    python scripts/voice_tts_diagnostic.py --list-devices                       # output devices + resolved default

Everything measured is printed; nothing is asserted as pass/fail (only a human, or a real wake-word test, can say
whether "Yes?" was actually heard -- see docs/VOICE.md).
"""

import argparse
import sys
import time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

CANDIDATE_ACK_PHRASES = ["Yes?", "Yes, I'm listening.", "Yes. How can I help?", "I'm listening."]


def measure(samples: np.ndarray, rate: int) -> dict:
    """Duration/peak/RMS/silence/clipping of a mono float32 (-1..1) or int16 buffer. "True peak" here is the sample
    peak (no inter-sample/oversampled reconstruction) -- a real limitation: a true ITU-R BS.1770 true-peak meter needs
    4x+ oversampling this script does not do. It is noted as such below rather than silently overstated."""
    if samples.size == 0:
        return {"frame_count": 0, "duration_ms": 0.0, "peak": 0.0, "rms": 0.0, "true_peak_note": "n/a (empty)",
                "leading_silence_ms": 0.0, "trailing_silence_ms": 0.0, "first_nonzero_sample": -1,
                "last_nonzero_sample": -1, "clipping_pct": 0.0}
    x = samples.astype(np.float64)
    if samples.dtype == np.int16:
        x = x / 32768.0
    abs_x = np.abs(x)
    duration_ms = len(x) / rate * 1000
    nonzero = np.nonzero(abs_x > 1e-6)[0]  # -120 dBFS floor: distinguishes true digital silence from any real signal
    first_nz = int(nonzero[0]) if nonzero.size else -1
    last_nz = int(nonzero[-1]) if nonzero.size else -1
    leading_ms = (first_nz / rate * 1000) if first_nz >= 0 else duration_ms
    trailing_ms = ((len(x) - 1 - last_nz) / rate * 1000) if last_nz >= 0 else duration_ms
    clip_frac = float(np.mean(abs_x >= 0.999)) * 100
    return {"frame_count": len(x), "duration_ms": round(duration_ms, 1), "peak": round(float(abs_x.max()), 4),
            "rms": round(float(np.sqrt(np.mean(x * x))), 4), "true_peak_note": "sample peak only, not oversampled",
            "leading_silence_ms": round(leading_ms, 1), "trailing_silence_ms": round(trailing_ms, 1),
            "first_nonzero_sample": first_nz, "last_nonzero_sample": last_nz, "clipping_pct": round(clip_frac, 3)}


def print_report(label: str, provider_name: str, voice: str, channels: int, dtype: str, rate: int, stats: dict) -> None:
    print(f"\n--- {label} ---")
    print(f"  provider={provider_name} voice={voice} sample_rate={rate} channels={channels} dtype={dtype}")
    for key in ("frame_count", "duration_ms", "peak", "rms", "true_peak_note", "leading_silence_ms",
                "trailing_silence_ms", "first_nonzero_sample", "last_nonzero_sample", "clipping_pct"):
        print(f"  {key}={stats[key]}")


def build_tts(settings):
    from voice.tts.piper_provider import PiperProvider

    return PiperProvider(settings.TTS_MODEL_PATH), "piper", Path(settings.TTS_MODEL_PATH).stem


def diagnose_text(settings, text: str, gain: float, play: bool) -> dict:
    from voice.audio import AudioOutput, pad_utterance

    tts, provider_name, voice = build_tts(settings)
    t0 = time.monotonic()
    raw, rate = tts.synthesize(text)
    synth_ms = (time.monotonic() - t0) * 1000
    raw_stats = measure(raw, rate)
    padded = pad_utterance(raw, rate)
    padded_stats = measure(padded, rate)

    print(f"\n===== {text!r} =====")
    print(f"  synth_time_ms={synth_ms:.1f}")
    print_report("raw Piper output (pre-padding)", provider_name, voice, 1, str(raw.dtype), rate, raw_stats)
    print_report("padded buffer (what AudioOutput actually receives)", provider_name, voice, 1, str(padded.dtype), rate, padded_stats)

    if not play:
        print("\n  (no --play: this is the measured buffer only, not a real playback test)")
        return {"text": text, "raw": raw_stats, "padded": padded_stats}

    out = AudioOutput(device=settings.VOICE_OUTPUT_DEVICE, volume=gain)
    print(f"\n  Speaker resolved: {out.selection.describe()}")
    if out.selection.device is None:
        print("  FAIL: no usable output device -- cannot play. Tried: " + "; ".join(out.selection.tried[:6] or ["none"]))
        return {"text": text, "raw": raw_stats, "padded": padded_stats, "played": False}
    print("  Warming up the output stream (silent, one-time cold-start cost)...")
    out.warm_up()
    print("  ACK_STARTED")
    t_play0 = time.monotonic()
    print("  ACK_PLAYBACK_STARTED -- LISTEN NOW")
    out.play(padded, rate)
    playback_ms = (time.monotonic() - t_play0) * 1000
    print(f"  ACK_PLAYBACK_FINISHED duration_ms={playback_ms:.1f}")
    print(f"  ACK_TOTAL_DURATION_MS={synth_ms + playback_ms:.1f}")
    print("  Did you hear the COMPLETE phrase clearly? This script cannot know -- only a human can confirm that.")
    return {"text": text, "raw": raw_stats, "padded": padded_stats, "played": True, "playback_ms": round(playback_ms, 1)}


def list_devices() -> int:
    from voice.speaker import SpeakerDeviceManager

    mgr = SpeakerDeviceManager()
    try:
        devices = mgr.list_output_devices()
    except Exception as exc:  # noqa: BLE001
        print("FAIL: could not list audio devices:", exc)
        return 1
    print("Output devices (index, host API, channels, native rate):")
    for d in devices:
        print(f"  [{d.index:>2}] {d.name}  | {d.hostapi} | {d.max_output_channels} ch | {d.default_samplerate:.0f} Hz{'  <- Windows default' if d.is_default else ''}")
    from backend.core.config import get_settings

    settings = get_settings()
    mode, candidates = mgr.candidates(settings.VOICE_OUTPUT_DEVICE)
    print(f"\nVOICE_OUTPUT_DEVICE={settings.VOICE_OUTPUT_DEVICE!r} -> mode {mode}; order JARVIS will try:")
    for d in candidates[:6]:
        print(f"  [{d.index:>2}] {d.name} ({d.hostapi})")
    resolved = mgr.resolve(settings.VOICE_OUTPUT_DEVICE)
    print(f"\nResolved: {resolved.describe()}")
    return 0


def volume_and_mute_diagnostic() -> None:
    """Diagnose (never change) Windows volume/mute state for the resolved output device, best-effort via pycaw if
    installed. This never writes to the system volume."""
    print("\nWindows volume/mute diagnostic (read-only; nothing is changed):")
    try:
        from ctypes import POINTER, cast

        from comtypes import CLSCTX_ALL
        from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume
    except ImportError:
        print("  pycaw not installed -- skipping (pip install pycaw comtypes to enable this check; not required for JARVIS to run)")
        return
    try:
        speakers = AudioUtilities.GetSpeakers()
        interface = speakers.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
        volume = cast(interface, POINTER(IAudioEndpointVolume))
        print(f"  system_volume={volume.GetMasterVolumeLevelScalar() * 100:.0f}%  muted={bool(volume.GetMute())}")
    except Exception as exc:  # noqa: BLE001 - diagnostic only, never fatal
        print(f"  could not read system volume/mute state: {exc}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--text", default="Yes?", help='text to synthesize and measure (default: "Yes?")')
    ap.add_argument("--play", action="store_true", help="actually play the audio through the real resolved output device (you must listen)")
    ap.add_argument("--compare-phrases", action="store_true", help="measure/compare Yes? against alternate acknowledgement phrasings")
    ap.add_argument("--list-devices", action="store_true", help="list output devices and the one JARVIS would resolve to; synthesizes nothing")
    ap.add_argument("--gain", type=float, default=None, help="override VOICE_TTS_VOLUME for this run only (for --play)")
    args = ap.parse_args()

    if args.list_devices:
        return list_devices()

    from backend.core.config import get_settings

    settings = get_settings()
    gain = args.gain if args.gain is not None else settings.VOICE_TTS_VOLUME
    volume_and_mute_diagnostic()

    texts = CANDIDATE_ACK_PHRASES if args.compare_phrases else [args.text]
    for text in texts:
        diagnose_text(settings, text, gain, args.play)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
