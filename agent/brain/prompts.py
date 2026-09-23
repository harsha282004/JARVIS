"""Prompt construction for the Agent Brain (kept out of the logic module)."""

from collections.abc import Sequence
import json

from agent.tools.base import ToolDescriptor
from backend.core.conversation.prompts import SYSTEM_PROMPT

_DECISION_INSTRUCTIONS = """\
Before answering, decide what the user wants. Reply with ONE JSON object and nothing else:
{"intent": "...", "response": "...", "steps": ["..."], "tools": ["..."], "confidence": 0.0, "summary": "..."}

intent must be exactly one of:
- conversation: greetings, thanks, small talk.
- information_request: a question you can answer from general knowledge.
- action_request: the user wants JARVIS to DO something in the world (send, open, play, schedule, create, delete, control, ...).
- clarification_required: too vague to act on even using the earlier conversation (e.g. "do that", "send it to him" with no clear referent).
- unsupported_request: something JARVIS cannot or should not do at all.

Rules:
- response: for conversation, information_request and clarification_required, the short text to say aloud (for clarification, the question to ask). Use "" for other intents.
- action_request: steps = short preparation steps (permission and execution steps are added automatically); tools = names of the tools needed. Prefer names from AVAILABLE TOOLS; if none fits, give a short descriptive name such as "email".
- You cannot execute anything. Never say or imply that an action was done. Never output code or commands.
- Use the earlier conversation to resolve pronouns and follow-ups. Never invent missing details (recipients, times, contents); use clarification_required instead.
- confidence is a number from 0 to 1. summary is one short sentence describing the request (no step-by-step reasoning).
"""


def _describe_tools(tools: Sequence[ToolDescriptor]) -> str:
    if not tools:
        return "(none: no tools are available yet)"
    lines = []
    for tool in tools:
        permission = "requires permission" if tool.requires_permission else "no permission needed"
        schema = json.dumps(tool.input_schema, sort_keys=True)
        lines.append(f"- {tool.name}: {tool.description} (input: {schema}; {permission})")
    return "\n".join(lines)


def build_system_prompt(tools: Sequence[ToolDescriptor]) -> str:
    return f"{SYSTEM_PROMPT}\n\n{_DECISION_INSTRUCTIONS}\nAVAILABLE TOOLS:\n{_describe_tools(tools)}"


RETRY_PROMPT = (
    "Your previous reply was not a valid decision. Reply again with ONLY the JSON "
    "object described above, with no other text."
)
