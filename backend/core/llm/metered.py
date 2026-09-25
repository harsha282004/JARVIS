"""MeteredLLM: wraps any LLMProvider and counts calls and latency, without changing what it does.

The count is how "avoid unnecessary LLM use" is verified: deterministic answers (planning, conflicts, briefings) add nothing here.
"""

from collections.abc import Sequence

from backend.core.llm.base import LLMProvider
from backend.core.llm.messages import Message
from backend.core.metrics import Metrics, metrics as default_metrics


class MeteredLLM(LLMProvider):
    def __init__(self, inner: LLMProvider, metrics: Metrics = default_metrics):
        self._inner = inner
        self._metrics = metrics

    def chat(self, messages: Sequence[Message], json_mode: bool = False) -> str:
        self._metrics.incr("llm_calls")
        try:
            with self._metrics.timer("llm_ms"):
                return self._inner.chat(messages, json_mode)
        except Exception:
            self._metrics.incr("llm_errors")
            raise
