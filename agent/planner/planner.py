"""Planner: turns a validated action request into an ordered, deterministic plan.

The plan only *describes* the workflow; nothing here runs a tool. Given the
same inputs it always produces the same plan:

    prepare steps (from the model, cleaned)  ->  permission step (if any tool
    needs it)  ->  one execute step per tool
"""

from collections.abc import Sequence

from agent.planner.models import Plan, PlanStep, StepKind

_MAX_DESCRIPTION_CHARS = 200
_FALLBACK_PREPARE = "Gather the details needed for the request"
_PERMISSION_STEP = "Ask the user for permission before acting"


class Planner:
    def __init__(self, max_steps: int):
        if max_steps < 1:
            raise ValueError("max_steps must be at least 1")
        self._max_steps = max_steps

    def build_plan(
        self,
        goal: str,
        prepare_steps: Sequence[str],
        tools: Sequence[str],
        requires_permission: bool,
    ) -> Plan:
        prepare = self._clean(prepare_steps) or [_FALLBACK_PREPARE]
        essential: list[tuple[StepKind, str, str | None]] = []
        if requires_permission:
            essential.append((StepKind.PERMISSION, _PERMISSION_STEP, None))
        if tools:
            essential.extend((StepKind.EXECUTE, f"Use the {name} tool", name) for name in tools)
        else:
            essential.append((StepKind.EXECUTE, "Carry out the requested action", None))

        # Essential steps (permission, execute) are kept over preparation detail.
        essential = essential[: self._max_steps]
        room = self._max_steps - len(essential)
        ordered = [(StepKind.PREPARE, text, None) for text in prepare[:room]] + essential
        steps = [
            PlanStep(order=i, kind=kind, description=text, tool=tool)
            for i, (kind, text, tool) in enumerate(ordered, start=1)
        ]
        return Plan(goal=goal, steps=steps)

    @staticmethod
    def _clean(descriptions: Sequence[str]) -> list[str]:
        cleaned: list[str] = []
        for text in descriptions:
            text = " ".join(text.split())[:_MAX_DESCRIPTION_CHARS]
            if text and text not in cleaned:
                cleaned.append(text)
        return cleaned
