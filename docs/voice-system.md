# Voice System (Phase 1)

## Overview

Phase 1 implements a functional, fully local voice pipeline:

```
Microphone
    v
Wake-word detection ("Hey JARVIS")   -- openWakeWord
    v
Voice capture (fixed window)
    v
Speech-to-text                        -- Faster-Whisper
    v
LLM                                   -- Ollama (via the Phase 0 LLMProvider interface)
    v
Text-to-speech                        -- Piper
    v
Speaker
```

`voice/engine.py`'s `VoiceEngine` drives exactly one wake-word cycle at a
time through a small state machine:

```
WAITING -> LISTENING -> TRANSCRIBING -> THINKING -> SPEAKING -> WAITING
```

Since Phase 3, one activation can hold several turns: after answering, the
engine listens again without the wake word until you stay silent, and
conversation context is kept in memory by `ConversationEngine` (see
`docs/conversation-engine.md`). Interruption handling is still not
implemented.

## Why these technologies

| Component  | Choice        | Why |
|------------|---------------|-----|
| Wake word  | openWakeWord  | Fully local, Apache-2.0, no vendor account or access key. Ships a pretrained model for the exact phrase JARVIS needs (`hey_jarvis_v0.1`), so "Hey JARVIS" isn't a fabricated claim — it's the model's actual training target. Porcupine was considered but requires a Picovoice account and access key, which this project avoids. |
| STT        | Faster-Whisper | CTranslate2-based reimplementation of Whisper; runs on CPU with int8 quantization, no GPU required for the `tiny`/`base` models. |
| LLM        | Ollama        | Already the Phase 0 default provider target; runs models fully locally. |
| TTS        | Piper         | Fully local neural TTS (ONNX-based), no network access needed once a voice model is downloaded, no external `espeak-ng` binary required (bundled). |

## Architecture / replaceability

Every provider sits behind a Phase 0/1 interface; nothing in `voice/engine.py`
or `voice/bootstrap.py` depends on a specific vendor:

```
voice/
├── base.py              VoiceProvider (shared marker interface, from Phase 0)
├── audio.py              AudioInput / AudioOutput (sounddevice/PortAudio)
├── exceptions.py          VoiceProviderError, ProviderNotConfiguredError, AudioDeviceError
├── engine.py              VoiceEngine + VoiceState state machine
├── bootstrap.py            build_voice_engine(settings) — provider selection by config
├── wakeword/
│   ├── base.py                  WakeWordProvider
│   └── openwakeword_provider.py  OpenWakeWordProvider
├── stt/
│   ├── base.py                  STTProvider
│   └── faster_whisper_provider.py  FasterWhisperProvider
└── tts/
    ├── base.py                  TTSProvider
    └── piper_provider.py         PiperProvider
```

`backend/core/llm/ollama_provider.py` adds `OllamaProvider`, the first
concrete implementation of Phase 0's `LLMProvider` interface, talking to a
local Ollama server's `/api/chat` REST endpoint over `httpx` (it used
`/api/generate` in Phase 1).

Swapping any provider (e.g. a different wake-word engine) means adding a
new class implementing the relevant `base.py` interface and a branch in
`voice/bootstrap.py` — call sites in `engine.py` never change.

## Configuration

Extends the existing Phase 0 `Settings` class (`backend/core/config.py`) —
no second configuration system. New fields, with `.env.example` defaults:

```
# Wake word
WAKE_WORD_ENABLED=true
WAKE_WORD_PROVIDER=openwakeword
WAKE_WORD_MODEL_PATH=            # path to hey_jarvis_v0.1.onnx — required
WAKE_WORD_THRESHOLD=0.5

# Audio I/O
MICROPHONE_DEVICE=               # empty = system default input device
AUDIO_SAMPLE_RATE=16000
AUDIO_LISTEN_SECONDS=5.0         # fixed capture window after activation

# Speech-to-text
STT_PROVIDER=faster_whisper
STT_MODEL=base                   # tiny | base | small | medium | large-v3
STT_LANGUAGE=en
STT_DEVICE=cpu

# LLM (LLM_PROVIDER / LLM_MODEL / OLLAMA_BASE_URL already existed in Phase 0)
LLM_PROVIDER=ollama
LLM_MODEL=llama3
OLLAMA_BASE_URL=http://localhost:11434

# Text-to-speech
TTS_PROVIDER=piper
TTS_MODEL_PATH=                  # path to a Piper .onnx voice — required
TTS_VOICE=en_US-lessac-medium
```

**Design note:** Phase 1 deliberately does not add a separate
`OLLAMA_MODEL` field — `LLM_MODEL` (from Phase 0) is reused as the single
source of truth for whichever `LLM_PROVIDER` is active, so the provider
abstraction stays provider-agnostic rather than accumulating one config
field per vendor.

No real secrets or model paths are committed — `.env.example` only
contains placeholders, and `.gitignore` excludes `.env`, `/models/`, and
`*.onnx`/`*.tflite` files.

## Installation & model setup (Windows)

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy .env.example .env
```

### 1. Wake-word model (openWakeWord)

Requires three ONNX files in the **same directory**: the wake-word model
itself plus openWakeWord's melspectrogram and embedding feature models.

```powershell
python -c "from openwakeword.utils import download_models; download_models(['hey_jarvis_v0.1'], target_directory='models/wakeword')"
```

This downloads `hey_jarvis_v0.1.onnx`, `melspectrogram.onnx`, and
`embedding_model.onnx` into `models/wakeword/` (plus `.tflite` variants
and a VAD model this project doesn't use — safe to delete). Set in `.env`:

```
WAKE_WORD_MODEL_PATH=models/wakeword/hey_jarvis_v0.1.onnx
```

### 2. Speech-to-text model (Faster-Whisper)

No manual download step — `STT_MODEL` (e.g. `base`) is fetched
automatically from Hugging Face on first use and cached locally. Requires
network access once; fully offline after that.

CPU/hardware tradeoffs:

| STT_MODEL | Approx. size | CPU RAM | Notes |
|-----------|-------------|---------|-------|
| tiny      | ~75 MB      | ~1 GB   | Fastest, least accurate |
| base      | ~145 MB     | ~1 GB   | Default — reasonable accuracy/speed balance on CPU |
| small     | ~480 MB     | ~2 GB   | Better accuracy, noticeably slower on CPU |
| medium/large-v3 | 1.5–3 GB | 5+ GB | Needs a GPU for real-time use; not recommended CPU-only |

### 3. LLM (Ollama)

```powershell
# Install Ollama separately: https://ollama.com/download
ollama pull llama3
ollama serve   # or run the Ollama desktop app, which serves automatically
```

Set `OLLAMA_BASE_URL` (default `http://localhost:11434`) and `LLM_MODEL`
(default `llama3`) in `.env` to match.

### 4. Text-to-speech voice (Piper)

```powershell
python -c "from pathlib import Path; from piper.download_voices import download_voice; download_voice('en_US-lessac-medium', Path('models/tts'))"
```

Downloads `en_US-lessac-medium.onnx` and its `.onnx.json` config into
`models/tts/`. Set in `.env`:

```
TTS_MODEL_PATH=models/tts/en_US-lessac-medium.onnx
```

Browse other voices at the Piper voices catalog if a different accent/
language is wanted; any Piper `.onnx` voice works.

### Run it

```powershell
python scripts/run_voice.py
```

Say "Hey JARVIS", wait for "Yes?", then ask a question. Ctrl+C to stop.

## Privacy behavior

- Wake-word detection, STT, and TTS all run **locally** — no audio leaves
  the machine for those stages.
- The LLM stage sends only the **transcribed text** (not audio) to Ollama,
  which itself runs locally by default (`OLLAMA_BASE_URL=http://localhost:11434`).
- Audio is held in memory only for the duration of one utterance
  (`AUDIO_LISTEN_SECONDS`, default 5s) and is never written to disk or
  persisted. There is no rolling/background recording — the microphone
  stream is only opened for the duration of `run_once()` and closed
  immediately after.
- No microphone data is exposed through any HTTP API (the FastAPI app has
  no voice endpoints in Phase 1).
- Logging (`VOICE_ENGINE_STARTED`, `WAKE_WORD_DETECTED`, `STT_STARTED`,
  `STT_COMPLETED`, `LLM_REQUEST_STARTED`, `LLM_RESPONSE_RECEIVED`,
  `TTS_STARTED`, `TTS_COMPLETED`, `VOICE_ENGINE_STOPPED`) records event
  names and text lengths, not raw audio, and never a full transcript or
  response body beyond what's needed to debug (the `text`/response values
  are logged via `%r`/length only where noted in `voice/engine.py`).

## Personal-information safety

JARVIS has no memory, Gmail, Calendar, messaging, or personal RAG yet. The
LLM's system prompt (`backend.core.conversation.prompts.SYSTEM_PROMPT`) explicitly instructs it
not to claim access to any of those and to say plainly that a capability
isn't available yet if asked (e.g. "what's my next meeting?"). This is a
prompt-level safeguard, not a hard guarantee against LLM confabulation —
later phases that add real integrations will replace "not available" with
actual tool calls through `PermissionManager`.

## Testing

```powershell
pytest                     # unit tests (default) — fast, no hardware/models/network needed
pytest tests/integration   # integration tests — auto-skip whatever isn't configured/reachable
```

- **Unit tests** (`tests/test_voice_*.py`, `tests/test_*_provider.py`,
  `tests/test_ollama_provider.py`): provider interfaces, config loading,
  `VoiceEngine` state transitions with fake providers, and provider error
  handling (missing model files, unreachable Ollama, unknown provider
  names) — all with real logic but mocked/fake backends. Never claim real
  transcription, inference, or synthesis succeeded.
- **Integration tests** (`tests/integration/test_voice_pipeline_integration.py`,
  marked `@pytest.mark.integration`): exercise the *real* Ollama server,
  openWakeWord model, and Piper model when they're actually configured and
  reachable; each test calls `pytest.skip()` with a clear reason otherwise
  — they never report a false pass.
- **Manual hardware tests**: the full microphone-to-speaker loop, see below.

## Manual end-to-end test

This is the only way to verify the complete pipeline including real
microphone input and speaker output, and cannot be automated.

1. Activate the virtual environment.
2. Start Ollama (`ollama serve`) and confirm the configured model is
   pulled (`ollama list`).
3. Confirm `WAKE_WORD_MODEL_PATH` points to a valid openWakeWord model
   (plus its feature models in the same directory).
4. Confirm `STT_MODEL` is a valid Faster-Whisper model size (downloads
   automatically on first run).
5. Confirm `TTS_MODEL_PATH` points to a valid Piper voice model.
6. Run `python scripts/run_voice.py`.
7. Wait for the "Voice engine ready" log line (WAITING state).
8. Say "Hey JARVIS".
9. Confirm the engine activates (`WAKE_WORD_DETECTED` logged) and speaks
   "Yes?".
10. Ask "What is today's date?"
11. Verify the transcript logged (`STT_COMPLETED`) matches what was said.
12. Verify an LLM response was received (`LLM_RESPONSE_RECEIVED`).
13. Verify the response is spoken aloud through the speakers.
14. Verify the engine returns to WAITING and reacts to "Hey JARVIS" again.

### What was actually verified in this environment, and what wasn't

This implementation was built and tested on the target machine, with real
(not mocked) components, as far as the environment allowed:

- **Verified with real models, on real hardware**: microphone capture
  (`AudioInput`, real PortAudio device), wake-word model loading and
  inference (`OpenWakeWordProvider`, real `hey_jarvis_v0.1.onnx`, correctly
  silent on background noise), Piper TTS synthesis and real speaker
  playback (`PiperProvider` + `AudioOutput`, audible, non-silent output),
  and Faster-Whisper transcription of that same synthesized audio played
  back through the STT pipeline (round-tripped a full sentence correctly).
- **Not verified**: a human saying "Hey JARVIS" out loud and a live Ollama
  server. Ollama was not installed in this environment, so `OllamaProvider`
  is verified only against unit tests with a mocked HTTP layer, plus a
  real, un-skipped integration test that will run the moment Ollama is
  installed and reachable (`tests/integration/test_ollama_generate_real_response`).
  The literal "say the wake word out loud" step requires a human physically
  present at the microphone and was not performed here — do this step
  yourself using `scripts/run_voice.py` per the checklist above.

## Known limitations

- Fixed-duration listening window (`AUDIO_LISTEN_SECONDS`) instead of
  proper end-of-speech / voice-activity detection — you have exactly that
  many seconds to speak after "Yes?". Not addressed in Phase 3.
- Multi-turn context is in memory only (Phase 3); see `docs/conversation-engine.md`.
- No interruption handling (can't stop JARVIS mid-sentence).
- Wake-word/STT/TTS model quality depends entirely on the chosen model
  size vs. available CPU/GPU.
- The LLM's refusal to claim access to email/calendar/memory is prompt-based,
  not enforced by any permission check (there's nothing to permission yet
  in Phase 1 — no tools exist).
