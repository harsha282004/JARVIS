"""Provider-neutral chat message model shared by the conversation engine and
every LLMProvider, so no provider-specific format leaks into callers."""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum


class Role(StrEnum):
    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class Message:
    role: Role
    content: str
    timestamp: datetime = field(default_factory=_utcnow)
