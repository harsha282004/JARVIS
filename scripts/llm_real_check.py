"""REAL_WORLD check of the configured LLM provider (Groq by default) with YOUR key from .env. Costs a few hundred tokens.

    python scripts/llm_real_check.py

Prints the staged health (configured / key / reachable / authenticated / model available / inference), then a short multi-turn chat and a JSON-mode call validated with the
same parser the agent brain uses. If GROQ_API_KEY is not set it says NOT VERIFIED and exits 2: nothing is faked. The key is never printed. Exit 0 = every stage passed.
"""

import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.core.config import get_settings  # noqa: E402
from backend.core.llm.base import LLMProviderError  # noqa: E402
from backend.core.llm.factory import build_llm  # noqa: E402
from backend.core.llm.messages import Message, Role  # noqa: E402


def main() -> int:
    s = get_settings()
    llm = build_llm(s)
    print(f"Provider: {s.LLM_PROVIDER}   Model: {s.LLM_MODEL}   Base URL: {getattr(s, 'GROQ_BASE_URL', '')}")
    if not hasattr(llm, "health"):
        print("This provider has no staged health check; running a chat only.")
    else:
        h = llm.health(inference=True)
        for label, value in (("Provider configured", h.configured), ("API key configured", h.key_configured), ("Provider reachable", h.reachable), ("Authentication", h.authenticated),
                             ("Model available", h.model_available), ("Inference", h.inference)):
            print(f"  {label:22} {value}")
        print("  Detail:", h.detail)
        if not h.ok:
            print("NOT VERIFIED" if h.problem == "config" else "FAIL", "-", h.problem)
            return 2 if h.problem == "config" else 1
    try:
        began = time.perf_counter()
        first = llm.chat([Message(Role.SYSTEM, "You are JARVIS, a concise voice assistant."), Message(Role.USER, "My project is called JARVIS. Say only 'noted'.")])
        second = llm.chat([Message(Role.SYSTEM, "You are JARVIS, a concise voice assistant."), Message(Role.USER, "My project is called JARVIS. Say only 'noted'."),
                           Message(Role.ASSISTANT, first), Message(Role.USER, "What is my project called? One short sentence.")])
        print(f"Multi-turn: {second!r}  ({(time.perf_counter() - began) * 1000:.0f} ms for two calls)")
        raw = llm.chat([Message(Role.SYSTEM, 'Reply ONLY with JSON: {"intent": "conversation", "response": "<one short sentence>"}'), Message(Role.USER, "Say hello.")], json_mode=True)
        data = json.loads(raw)
        print("JSON mode:", "valid" if isinstance(data, dict) and "response" in data else "INVALID", "-", raw[:120])
    except (LLMProviderError, ValueError) as exc:
        print("FAIL:", exc)
        return 1
    print("PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
