"""Typed models for the Agent Brain: request in, structured decision out.

A decision is data. Nothing in it can execute anything; it only *describes*
what a future tool layer would need to do (see docs/agent-brain.md).
"""

from enum import StrEnum

from pydantic import BaseModel, Field, model_validator

from agent.planner.models import Plan
from agent.tools.base import ToolDescriptor
from backend.core.llm.messages import Message
from backend.core.security import PermissionRequest


class Intent(StrEnum):
    CONVERSATION = "conversation"
    INFORMATION_REQUEST = "information_request"
    ACTION_REQUEST = "action_request"
    CLARIFICATION_REQUIRED = "clarification_required"
    UNSUPPORTED_REQUEST = "unsupported_request"


class AgentRequest(BaseModel):
    """What the brain is asked to decide on.

    `context` is the recent conversation supplied by ConversationEngine (the
    brain keeps no history of its own); `tools` are read-only descriptions.
    """

    user_text: str = Field(min_length=1)
    context: list[Message] = Field(default_factory=list)
    tools: list[ToolDescriptor] = Field(default_factory=list)
    # Delimited, sanitized personal-memory block (agent.memory.context); untrusted data.
    memory_context: str = ""


class ToolSelection(BaseModel):
    """A tool the request would need. `available` is False when no registered
    tool has that name; such a tool can never be run."""

    name: str = Field(min_length=1)
    available: bool
    requires_permission: bool


class AgentErrorCode(StrEnum):
    INVALID_OUTPUT = "invalid_output"


class AgentError(BaseModel):
    """A recorded failure. `detail` never contains the model's raw output."""

    code: AgentErrorCode
    detail: str


class AgentDecision(BaseModel):
    intent: Intent
    action_required: bool
    plan: Plan | None = None
    response: str = Field(min_length=1)
    selected_tools: list[ToolSelection] = Field(default_factory=list)
    requires_permission: bool = False
    confidence: float = Field(ge=0.0, le=1.0)
    reasoning_summary: str = ""
    error: AgentError | None = None

    @model_validator(mode="after")
    def _check_consistency(self) -> "AgentDecision":
        is_action = self.intent is Intent.ACTION_REQUEST
        if self.action_required != is_action:
            raise ValueError("action_required must be true exactly for action_request")
        if not is_action and (self.plan or self.selected_tools or self.requires_permission):
            raise ValueError("only action_request decisions may carry a plan, tools or permission needs")
        return self

    @property
    def missing_tools(self) -> list[str]:
        return [t.name for t in self.selected_tools if not t.available]

    def permission_requests(self) -> list[PermissionRequest]:
        """Requests a future executor must put to the PermissionManager before
        running anything. Building them authorizes nothing."""
        return [
            PermissionRequest(tool_name=t.name, action="execute", requested_by="agent")
            for t in self.selected_tools
            if t.requires_permission
        ]
