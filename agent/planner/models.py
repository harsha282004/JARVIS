"""Plan models. A plan is a description of intended work, never an executor."""

from enum import StrEnum

from pydantic import BaseModel, Field


class StepKind(StrEnum):
    PREPARE = "prepare"
    PERMISSION = "permission"
    EXECUTE = "execute"


class PlanStep(BaseModel):
    order: int = Field(ge=1)
    kind: StepKind
    description: str = Field(min_length=1)
    tool: str | None = None


class Plan(BaseModel):
    goal: str
    steps: list[PlanStep]
