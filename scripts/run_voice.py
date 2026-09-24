#!/usr/bin/env python
"""Manual entrypoint for the Phase 1 voice pipeline.

Usage:
    python scripts/run_voice.py

Requires Ollama running locally with the configured model pulled, plus the
wake-word and Piper voice model files referenced in `.env` — see
docs/voice-system.md. Runs forever, cycling: wait for "Hey JARVIS" ->
listen -> transcribe -> ask the LLM -> speak the reply -> wait again.
Press Ctrl+C to stop.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.core.config import get_settings  # noqa: E402
from backend.core.logging import configure_logging, get_logger  # noqa: E402
from voice.bootstrap import build_reminder_scheduler, build_task_system, build_voice_engine  # noqa: E402
from voice.exceptions import VoiceProviderError  # noqa: E402


def main() -> int:
    settings = get_settings()
    configure_logging(settings.LOG_LEVEL)
    logger = get_logger(__name__)

    task_system = build_task_system(settings)
    try:
        engine = build_voice_engine(settings, task_system)
    except VoiceProviderError as exc:
        logger.error("Voice engine setup failed: %s", exc)
        print(f"Setup error: {exc}")
        return 1

    logger.info("Voice engine ready. Say \"Hey JARVIS\" to activate. Ctrl+C to stop.")
    # No tray in this script, so reminders are announced by voice only.
    scheduler = build_reminder_scheduler(settings, task_system, None)
    if scheduler is not None:
        scheduler.start()
    try:
        engine.run_forever()
    except KeyboardInterrupt:
        logger.info("Stopped by user")
    finally:
        if scheduler is not None:
            scheduler.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
