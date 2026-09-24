"""The structured task/reminder action the LLM may propose (models only; nothing here executes).

The model supplies a name and a few arguments as plain text. Times stay natural-language
phrases ("tomorrow at 9 AM") that deterministic code parses later; the model never computes
a date and never supplies a task or reminder id. An action that does not validate is rejected
(the AgentBrain then treats the whole output as invalid), so arbitrary model output cannot
become a database operation.
"""

from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from agent.tasks.models import TaskPriority


class TaskActionName(StrEnum):
    CREATE_TASK = "create_task"
    LIST_TASKS = "list_tasks"
    COMPLETE_TASK = "complete_task"
    CANCEL_TASK = "cancel_task"
    CREATE_REMINDER = "create_reminder"
    LIST_REMINDERS = "list_reminders"
    CANCEL_REMINDER = "cancel_reminder"


TASK_ACTION_NAMES = frozenset(a.value for a in TaskActionName)
REMINDER_ACTIONS = frozenset({TaskActionName.CREATE_REMINDER, TaskActionName.LIST_REMINDERS, TaskActionName.CANCEL_REMINDER})

_TASK_SCOPES = {"today": "today", "overdue": "overdue", "upcoming": "upcoming", "incomplete": "incomplete",
                "pending": "incomplete", "open": "incomplete", "all": "incomplete", "due": "today"}
_REMINDER_SCOPES = {"today": "today", "tomorrow": "tomorrow", "upcoming": "upcoming", "next": "next", "all": "upcoming"}


class InvalidTaskAction(ValueError):
    """The proposed action is not a valid task/reminder action. The message never contains model text."""


class _Args(BaseModel):
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)

    @field_validator("*", mode="before")
    @classmethod
    def _blank_is_none(cls, value: Any) -> Any:
        return None if isinstance(value, str) and not value.strip() else value


class CreateTaskArgs(_Args):
    title: str = Field(min_length=1, max_length=200)
    notes: str | None = Field(default=None, max_length=1000)
    due: str | None = Field(default=None, max_length=100)  # a phrase such as "tomorrow at 5 pm"
    priority: TaskPriority | None = None
    remind: bool = False  # also remind at the due time

    @field_validator("remind", mode="before")
    @classmethod
    def _remind(cls, value: Any) -> Any:
        return False if value is None else value

    @field_validator("priority", mode="before")
    @classmethod
    def _priority(cls, value: Any) -> Any:
        if isinstance(value, str):  # an unrecognised priority word is ignored: it is cosmetic, never guessed
            return TaskPriority.__members__.get(value.strip().upper())
        return value if isinstance(value, TaskPriority) or value is None else None


class CreateReminderArgs(_Args):
    message: str = Field(min_length=1, max_length=300)
    when: str | None = Field(default=None, max_length=100)
    recurrence: str | None = Field(default=None, max_length=150)  # a phrase such as "every Monday at 8 AM"


class ListTasksArgs(_Args):
    scope: Literal["today", "overdue", "upcoming", "incomplete"] = "incomplete"

    @field_validator("scope", mode="before")
    @classmethod
    def _scope(cls, value: Any) -> Any:
        return _TASK_SCOPES.get(value.strip().lower(), value) if isinstance(value, str) else "incomplete"


class ListRemindersArgs(_Args):
    scope: Literal["today", "tomorrow", "upcoming", "next"] = "upcoming"

    @field_validator("scope", mode="before")
    @classmethod
    def _scope(cls, value: Any) -> Any:
        return _REMINDER_SCOPES.get(value.strip().lower(), value) if isinstance(value, str) else "upcoming"


class TaskQueryArgs(_Args):
    query: str = Field(min_length=1, max_length=200)  # words describing the task; matched by code, never an id


class CompleteTaskArgs(TaskQueryArgs):
    pass


class CancelTaskArgs(TaskQueryArgs):
    pass


class CancelReminderArgs(TaskQueryArgs):
    when: str | None = Field(default=None, max_length=100)


ARGUMENT_MODELS: dict[TaskActionName, type[BaseModel]] = {
    TaskActionName.CREATE_TASK: CreateTaskArgs,
    TaskActionName.LIST_TASKS: ListTasksArgs,
    TaskActionName.COMPLETE_TASK: CompleteTaskArgs,
    TaskActionName.CANCEL_TASK: CancelTaskArgs,
    TaskActionName.CREATE_REMINDER: CreateReminderArgs,
    TaskActionName.LIST_REMINDERS: ListRemindersArgs,
    TaskActionName.CANCEL_REMINDER: CancelReminderArgs,
}


class TaskAction(BaseModel):
    """A validated proposal: a known action name with type-checked arguments. Executes nothing."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    name: TaskActionName
    arguments: BaseModel


def parse_task_action(raw: object) -> TaskAction:
    """Validate the model's `action` object. Raises InvalidTaskAction on anything unexpected."""
    if not isinstance(raw, dict):
        raise InvalidTaskAction("action is not an object")
    name = raw.get("name")
    if not isinstance(name, str) or name.strip().lower() not in TASK_ACTION_NAMES:
        raise InvalidTaskAction("unknown action name")
    action = TaskActionName(name.strip().lower())
    arguments = raw.get("arguments", {})
    if arguments is None:
        arguments = {}
    if not isinstance(arguments, dict):
        raise InvalidTaskAction("action arguments are not an object")
    try:
        parsed = ARGUMENT_MODELS[action].model_validate(arguments)
    except ValidationError as exc:
        # Field names only: error inputs would echo model output.
        problems = ", ".join(sorted({".".join(str(p) for p in e["loc"]) or "arguments" for e in exc.errors()}))
        raise InvalidTaskAction(f"invalid arguments for {action.value} ({problems})") from None
    return TaskAction(name=action, arguments=parsed)
