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


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mic", action="store_true", help="sample one second of microphone level (discarded)")
    args = ap.parse_args()

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

    from voice.audio import list_input_devices

    try:
        devices = list_input_devices()
        print("\nInput devices:", "; ".join(f"[{d['index']}] {d['name']}{' (default)' if d['default'] else ''}" for d in devices[:6]))
    except Exception as exc:  # noqa: BLE001
        print("\nInput devices could not be listed:", exc)
    if args.mic:
        from voice.audio import AudioInput

        with AudioInput(RATE, s.MICROPHONE_DEVICE) as mic:
            levels = [frame_level(mic.read_frame()) for _ in range(12)]
        print(f"Microphone opened; level over ~1 s: min {min(levels):.4f} max {max(levels):.4f} (samples discarded, nothing stored)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
