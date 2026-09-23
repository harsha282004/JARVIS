"""Parsing and validation of the LLM's structured output.

LLM text is untrusted input. It is only ever parsed as JSON data into a
strict model; it is never evaluated, imported, or passed to a shell.
"""

import json

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from agent.brain.models import Intent

MAX_OUTPUT_CHARS = 20_000
MAX_TOOLS = 5
MAX_SUMMARY_CHARS = 200
MAX_QUERY_CHARS = 300


class InvalidAgentOutput(Exception):
    """The LLM output was not a valid decision. Carries a safe (content-free) reason."""


class LLMDecisionOutput(BaseModel):
    """The JSON object the LLM is asked to produce. Unknown keys are ignored;
    known keys are type-checked."""

    model_config = ConfigDict(extra="ignore")

    intent: Intent
    response: str = ""
    steps: list[str] = Field(default_factory=list)
    tools: list[str] = Field(default_factory=list)
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    summary: str = ""
    query: str = ""

    @field_validator("query")
    @classmethod
    def _clean_query(cls, value: str) -> str:
        return " ".join(value.split())[:MAX_QUERY_CHARS]

    @field_validator("intent", mode="before")
    @classmethod
    def _normalize_intent(cls, value: object) -> object:
        return value.strip().lower() if isinstance(value, str) else value

    @field_validator("summary")
    @classmethod
    def _truncate_summary(cls, value: str) -> str:
        return " ".join(value.split())[:MAX_SUMMARY_CHARS]

    @field_validator("tools")
    @classmethod
    def _limit_tools(cls, value: list[str]) -> list[str]:
        names = list(dict.fromkeys(name.strip() for name in value if name.strip()))
        return names[:MAX_TOOLS]


def _extract_json_object(text: str) -> object:
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        text = text.removeprefix("json").strip()
    start = text.find("{")
    if start == -1:
        raise InvalidAgentOutput("no JSON object found")
    try:
        obj, _end = json.JSONDecoder().raw_decode(text[start:])
    except json.JSONDecodeError as exc:
        raise InvalidAgentOutput(f"malformed JSON ({exc.msg})") from exc
    return obj


def parse_decision_output(text: str) -> LLMDecisionOutput:
    """Parse and validate raw LLM text. Raises InvalidAgentOutput on any problem."""
    if len(text) > MAX_OUTPUT_CHARS:
        raise InvalidAgentOutput("output too long")
    obj = _extract_json_object(text)
    if not isinstance(obj, dict):
        raise InvalidAgentOutput("JSON output is not an object")
    try:
        return LLMDecisionOutput.model_validate(obj)
    except ValidationError as exc:
        # Only field locations and messages: error inputs would echo model output.
        problems = "; ".join(f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors())
        raise InvalidAgentOutput(f"schema validation failed ({problems})") from exc
