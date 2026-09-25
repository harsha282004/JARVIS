#!/usr/bin/env python
"""Measure the intelligence layer's latency on the synthetic scenario (no network, no real data, no LLM).

    python scripts/benchmark.py [--runs 200]

Reports mean / p95 / max milliseconds per operation over N runs, and the number of LLM calls (must be 0). Voice-path timings (wake word,
STT, TTS, LLM) are only measurable on a real run: see the metrics shown by the dashboard (`/metrics`) after using JARVIS by voice.
"""

import argparse
import json
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tests.intelligence_helpers import NoLLM, scenario_harness  # noqa: E402


def measure(fn, runs: int) -> dict:
    samples = []
    for _ in range(runs):
        start = time.perf_counter()
        fn()
        samples.append((time.perf_counter() - start) * 1000)
    samples.sort()
    return {"mean_ms": round(sum(samples) / len(samples), 3), "p95_ms": round(samples[int(len(samples) * 0.95)], 3), "max_ms": round(samples[-1], 3)}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runs", type=int, default=200)
    runs = parser.parse_args().runs
    h = scenario_harness(Path(tempfile.mkdtemp()))
    svc = h.service

    def cold(fn):
        def run():
            svc._invalidate()
            fn()
        return run

    results = {
        "snapshot_and_context_build (cold)": measure(cold(lambda: svc.bundle()), runs),
        "focus_answer (warm)": measure(lambda: h.say("What should I focus on today?"), runs),
        "focus_answer (cold, re-reads sources)": measure(cold(lambda: h.say("What should I focus on today?")), runs),
        "plan_my_day (cold)": measure(cold(lambda: h.say("Plan my day")), runs),
        "prepare_for_event (warm)": measure(lambda: h.say("Prepare me for tomorrow's project review"), runs),
        "conflict_check (warm)": measure(lambda: h.say("Any conflicts?"), runs),
        "why_explanation": measure(lambda: h.say("Why did you schedule that?"), runs),
    }
    print(json.dumps({"runs": runs, "llm_calls": NoLLM.calls, "operations": results}, indent=2))
    return 0 if NoLLM.calls == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
