"""In-memory conversation session model. Never persisted (see docs/conversation-engine.md)."""

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from uuid import uuid4

from backend.core.llm.messages import Message


class SessionState(StrEnum):
    ACTIVE = "active"
    ENDED = "ended"


@dataclass
class ConversationSession:
    """One conversation. `messages` holds user/assistant turns only (the
    system prompt is added when a request is built). The id is a random UUID
    with no user-identifying information."""

    created_at: datetime
    last_activity: datetime
    session_id: str = field(default_factory=lambda: str(uuid4()))
    messages: list[Message] = field(default_factory=list)
    state: SessionState = SessionState.ACTIVE

    @property
    def is_active(self) -> bool:
        return self.state is SessionState.ACTIVE

    def add(self, message: Message) -> None:
        self.messages.append(message)
        self.last_activity = message.timestamp

    def end(self) -> None:
        """Mark ended and drop the history (it is not retained anywhere)."""
        self.state = SessionState.ENDED
        self.messages.clear()
