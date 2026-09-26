"""Real-model voice check (no speaker output, no human needed, nothing recorded to disk).

    python scripts/voice_real_check.py [--mic]

1. Piper synthesizes test sentences (real TTS), the audio is placed inside silence, cut by the real VAD (UtteranceDetector) and recognized
   by real Faster-Whisper. Prints the transcript, Whisper confidence, timings and whether the wake word model fires on synthetic "Hey Jarvis".
2. Speaker/microphone device enumeration (names only). With --mic, one second of microphone level is sampled and immediately discarded.

Everything stays in memory. Exit code 0 if the pipeline ran; the printed table is the evidence to quote (it is not asserted as a pass/fail
because synthetic speech is not a human speaker).
"""

import argparse
import re
import sys
import time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from voice.normalize import Control, control_of  # noqa: E402
from voice.vad import EnergyVAD, UtteranceDetector, frame_level  # noqa: E402

RATE = 16000
FRAME = 1280
SENTENCES = ["What's my schedule today?", "Remind me at six PM to study.", "Which meeting is the longest?", "Stop."]


def resample(audio: np.ndarray, src: int, dst: int = RATE) -> np.ndarray:
    if src == dst:
        return audio
    n = int(len(audio) * dst / src)
    return np.interp(np.linspace(0, len(audio) - 1, n), np.arange(len(audio)), audio).astype(np.float32)


def to_int16(x: np.ndarray) -> np.ndarray:
    return (np.clip(x, -1, 1) * 32767).astype(np.int16)


def frames_of(audio: np.ndarray):
    for i in range(0, len(audio) - FRAME + 1, FRAME):
        yield audio[i:i + FRAME]


def norm(s: str) -> str:
    return re.sub(r"[^a-z0-9 ]", "", s.lower()).strip()


def wake_policy_check(settings, tts, wake, stt) -> None:
    """The strict wake policy end to end with the REAL models: Piper (the configured voice) speaks each phrase, openWakeWord scores it, the gate decides, and the real
    Faster-Whisper phrase check runs on candidates. Synthetic speech is not a human speaker; this shows which phrases can and cannot wake JARVIS. Nothing is stored."""
    import time as _time

    from voice.wake import WakeConfig, WakeGate, confirm_phrase

    print("\nStrict wake policy (voice: " + Path(settings.TTS_MODEL_PATH).stem + "; exact phrases \"hey jarvis\" / \"jarvis\" only):")
    cases = [("Hey Jarvis", True), ("Jarvis.", None), ("Okay Jarvis", False), ("Yes, Jarvis", False), ("Hey Travis", False), ("Hello there", False),
             ("What is the weather like today", False), ("Turn on the service", False), ("Jarvis sleep", False)]
    cfg = WakeConfig(direct_accept=False)
    for phrase, should_wake in cases:
        audio, rate = tts.synthesize(phrase)
        stream = np.concatenate([np.zeros(RATE, dtype=np.float32), resample(audio, rate), np.zeros(RATE * 2, dtype=np.float32)])
        wake.reset()
        gate = WakeGate(cfg, _time.monotonic)
        ring, peak, outcome = [], 0.0, "no candidate (not woken)"
        for frame in frames_of(stream):
            ring.append(to_int16(frame) if frame.dtype != np.int16 else frame)
            wake.process(to_int16(frame) if frame.dtype != np.int16 else frame)
            peak = max(peak, wake.last_score)
            decision = gate.observe(wake.last_score, wake.threshold)
            if decision.action != "none":
                heard = stt.transcribe_detailed(np.concatenate(ring[-30:]), RATE)
                ok, normalized, why = confirm_phrase(heard.text)
                outcome = f"{'WAKES' if ok else 'rejected'} after STT check ({heard.text!r} -> {normalized or why})"
                break
        verdict = "" if should_wake is None else ("  OK" if (should_wake == outcome.startswith("WAKES")) else "  <-- unexpected")
        print(f"  {phrase!r:34} peak score {peak:.2f}: {outcome}{verdict}")


def microphone_diagnostic(settings, seconds: float, capture: bool) -> int:
    """Device listing and (optionally) a real capture. PASS only if actual non-zero samples were received; the audio is discarded, never saved or sent."""
    import numpy as np

    from voice.audio import AudioInput
    from voice.exceptions import AudioDeviceError
    from voice.mic import MicrophoneDeviceManager, level_stats

    print("Microphone diagnostic")
    try:
        manager = MicrophoneDeviceManager()
        devices = manager.list_input_devices()
        default = manager.get_default_input_device()
    except Exception as exc:  # noqa: BLE001
        print("FAIL: could not list audio devices:", exc)
        return 1
    print("\nInput devices (index, host API, channels, native rate):")
    for d in devices:
        print(f"  [{d.index:>2}] {d.name}  | {d.hostapi} | {d.max_input_channels} ch | {d.default_samplerate:.0f} Hz{'  <- Windows default' if d.is_default else ''}")
    print("\nWindows default input:", default.name if default else "none")
    mode, candidates = manager.candidates(settings.MICROPHONE_DEVICE)
    print(f"MICROPHONE_DEVICE={settings.MICROPHONE_DEVICE!r} -> mode {mode}; order JARVIS will try:")
    for d in candidates[:6]:
        print(f"  [{d.index:>2}] {d.name} ({d.hostapi})")
    if not capture:
        return 0
    mic = AudioInput(settings.AUDIO_SAMPLE_RATE, settings.MICROPHONE_DEVICE)
    try:
        mic.open()
    except AudioDeviceError as exc:
        print("\nFAIL: microphone could not be opened:", exc)
        return 1
    try:
        print(f"\nSelected: {mic.selection.describe()}")
        print(f"Capturing {seconds:.0f} s: please SPEAK now (audio is analysed in memory and discarded)...")
        audio = np.concatenate(list(mic.frames(seconds)))
    finally:
        mic.close()
    stats = level_stats(audio)
    print(f"\nInput level (fraction of full scale): min={stats['min']:.6f} max={stats['max']:.6f} peak={stats['peak']:.6f} rms={stats['rms']:.6f}  (raw integer peak {stats['peak_int']})")
    if stats["peak_int"] <= 4:
        print("FAIL: the microphone opened but no usable signal was detected (all zeros or a noise floor of at most 4/32768). Nobody speaking, a muted/blocked microphone "
              "(Windows privacy switch, hardware mute key) or a wrong device would all look like this: speak during the capture, check the device above, then retry.")
        return 1
    if stats["peak"] < 0.005:
        print("WARN: samples were received but the level is extremely low (peak below 0.5 % of full scale). The device works, but either nobody spoke during the capture "
              "or the input gain is very low (Windows Settings > Sound > Input > Properties > Levels).")
        return 0
    print("PASS: audio samples received from the microphone.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mic", action="store_true", help="capture a few seconds from the selected microphone and report its level (samples discarded, nothing stored)")
    ap.add_argument("--devices", action="store_true", help="list input devices, the Windows default and the device JARVIS would select (opens nothing)")
    ap.add_argument("--mic-only", action="store_true", help="run only the microphone diagnostic (skips the synthetic Piper/VAD/Whisper pipeline)")
    ap.add_argument("--seconds", type=float, default=4.0, help="microphone capture length; speak during it for a meaningful level")
    args = ap.parse_args()
    if args.mic_only or (args.devices and not args.mic):
        from backend.core.config import get_settings

        return microphone_diagnostic(get_settings(), args.seconds, args.mic or args.mic_only)

    from backend.core.config import get_settings
    from voice.stt.faster_whisper_provider import FasterWhisperProvider
    from voice.tts.piper_provider import PiperProvider
    from voice.wakeword.openwakeword_provider import OpenWakeWordProvider

    s = get_settings()
    print("Loading real models (Piper, Faster-Whisper", s.STT_MODEL + ", openWakeWord)...")
    tts = PiperProvider(s.TTS_MODEL_PATH)
    stt = FasterWhisperProvider(s.STT_MODEL, s.STT_LANGUAGE, s.STT_DEVICE)
    wake = OpenWakeWordProvider(s.WAKE_WORD_MODEL_PATH, s.WAKE_WORD_THRESHOLD)

    print(f"\n{'sentence':34} {'heard':34} {'match':6} {'conf':>5} {'VAD':>9} {'utt s':>6} {'STT ms':>7}")
    ok = 0
    for sentence in SENTENCES:
        samples, rate = tts.synthesize(sentence)
        speech = to_int16(resample(samples, rate))
        stream = np.concatenate([np.zeros(FRAME * 8, dtype=np.int16), speech, np.zeros(FRAME * 30, dtype=np.int16)])
        det = UtteranceDetector(EnergyVAD(0.015), RATE, 1.0, 15.0, 0.15, 6.0)
        for f in frames_of(stream):
            det.push(f)
            if det.finished:
                break
        result = det.result()
        started = time.perf_counter()
        heard = stt.transcribe_detailed(result.audio, RATE)
        stt_ms = (time.perf_counter() - started) * 1000
        match = norm(heard.text).replace(" ", "") == norm(sentence).replace(" ", "") or norm(sentence).replace("six", "6").replace(" ", "") in norm(heard.text).replace("six", "6").replace(" ", "")
        if sentence == "Stop.":  # judged as the voice layer judges it: is it understood as the Stop control word?
            match = control_of(heard.text) is Control.STOP
        ok += match
        conf = f"{heard.confidence:.2f}" if heard.confidence is not None else "-"
        print(f"{sentence:34} {heard.text[:34]:34} {'yes' if match else 'NO':6} {conf:>5} {result.status.value:>9} {len(result.audio) / RATE:6.2f} {stt_ms:7.0f}")
    print(f"\nRecognized {ok}/{len(SENTENCES)} synthetic sentences through Piper -> VAD -> Faster-Whisper.")

    silence = np.zeros(FRAME * 30, dtype=np.int16)
    det = UtteranceDetector(EnergyVAD(0.015), RATE, 1.0, 15.0, 0.15, 2.0)
    for f in frames_of(silence):
        det.push(f)
        if det.finished:
            break
    print(f"Silence-only input -> VAD status: {det.status.value}; Whisper on silence -> {stt.transcribe_detailed(silence, RATE).text!r} (must be empty)")

    samples, rate = tts.synthesize("Hey Jarvis")
    stream = np.concatenate([np.zeros(FRAME * 10, dtype=np.int16), to_int16(resample(samples, rate)), np.zeros(FRAME * 20, dtype=np.int16)])
    peak = 0.0
    fired = False
    for f in frames_of(stream):
        fired = wake.process(f) or fired
        peak = max(peak, wake.last_score)
    print(f"Wake word on synthetic 'Hey Jarvis': fired={fired}, peak score {peak:.2f} (threshold {wake.threshold}); synthetic speech is not a human speaker")
    quiet = np.zeros(FRAME * 60, dtype=np.int16)
    wake.reset()
    print(f"Wake word on 4.8 s of silence: fired={any(wake.process(f) for f in frames_of(quiet))} (must be False)")
    wake_policy_check(s, tts, wake, stt)

    if args.mic or args.devices:
        return microphone_diagnostic(s, args.seconds, args.mic)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
