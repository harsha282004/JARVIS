"""Shared helpers for task/reminder tests."""

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from agent.tasks.repository import TaskRepository
from agent.tasks.service import ReminderService, TaskService
from agent.tasks.timeparse import TimeParser

IST = ZoneInfo("Asia/Kolkata")  # UTC+5:30, no daylight saving: easy to reason about


class Clock:
    """2030-03-04 is a Monday. 09:00 UTC is 14:30 in Asia/Kolkata."""

    def __init__(self, now=None):
        self.now = now or datetime(2030, 3, 4, 9, 0, tzinfo=timezone.utc)

    def __call__(self):
        return self.now

    def advance(self, **kwargs):
        self.now += timedelta(**kwargs)


def ist(year, month, day, hour=0, minute=0):
    return datetime(year, month, day, hour, minute, tzinfo=IST)


def make_services(session_factory, clock=None, zone=IST, **kw):
    clock = clock or Clock()
    repo = TaskRepository(session_factory)
    tasks = TaskService(repo, zone=zone, clock=clock, **{k: v for k, v in kw.items() if k == "default_priority"})
    reminders = ReminderService(repo, zone=zone, clock=clock, **{k: v for k, v in kw.items() if k != "default_priority"})
    return tasks, reminders, repo, clock


def make_parser(zone=IST):
    return TimeParser(zone)
