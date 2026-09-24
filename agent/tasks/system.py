"""The assembled task/reminder subsystem, shared by the conversation path and the scheduler."""

from dataclasses import dataclass

from agent.tasks.notifications import AnnouncementQueue
from agent.tasks.service import ReminderService, TaskService
from agent.tasks.timeparse import TimeParser
from agent.tasks.tools import TaskTool


@dataclass(frozen=True)
class TaskSystem:
    tasks: TaskService | None
    reminders: ReminderService | None
    parser: TimeParser
    tools: list[TaskTool]
    # Hand-off from the scheduler thread to the VoiceEngine thread for spoken reminders.
    announcements: AnnouncementQueue
