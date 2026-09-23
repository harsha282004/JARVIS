"""Voice pipeline: wakeword/, stt/, tts/ providers, audio I/O (audio.py), and
the VoiceEngine orchestrator (engine.py) wiring them into a single
wake-word -> listen -> transcribe -> LLM -> speak cycle. See
docs/voice-system.md for architecture and setup."""
