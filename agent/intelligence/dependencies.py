"""Task dependencies: "train the model" cannot start until "collect the dataset" is done.

Dependencies are stored explicitly (task id -> the task ids it waits for) in a small local JSON file, because the task table has no
such column and the user's statement "B depends on A" is the only evidence there is. A cycle is refused. The derived status is
computed from the tasks' real current state on every question, never stored, so it cannot go stale:

    BLOCKED      open, and at least one task it depends on is still open
    READY        open, nothing it depends on is open, and not started
    IN_PROGRESS  started
    COMPLETED / CANCELLED   as in the task list
A dependency on a task that no longer exists is ignored (it cannot block anything) and pruned.
"""

from enum import StrEnum
from pathlib import Path

from agent.intelligence.models import TaskItem
from backend.core.state_store import JsonFile


class DepStatus(StrEnum):
    BLOCKED = "blocked"
    READY = "ready"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


class DependencyError(ValueError):
    """The dependency was refused; the message is safe to say aloud."""


class DependencyStore:
    def __init__(self, path: Path | None = None):
        self._file = JsonFile(path, {}) if path else None
        self._mem: dict[str, list[str]] = {}

    def _all(self) -> dict[str, list[str]]:
        if self._file is None:
            return self._mem
        data = self._file.read()
        return {k: [x for x in v if isinstance(x, str)] for k, v in data.items() if isinstance(v, list)} if isinstance(data, dict) else {}

    def _save(self, data: dict[str, list[str]]) -> None:
        if self._file is None:
            self._mem = data
        else:
            self._file.write(data)

    def dependencies_of(self, task_id: str) -> list[str]:
        return list(self._all().get(task_id, []))

    def dependents_of(self, task_id: str) -> list[str]:
        return [t for t, deps in self._all().items() if task_id in deps]

    def add(self, task_id: str, depends_on: str) -> bool:
        """Record that `task_id` waits for `depends_on`. Returns False if it was already recorded. Raises DependencyError for a
        self-dependency or one that would create a cycle."""
        if task_id == depends_on:
            raise DependencyError("A task can't depend on itself.")
        data = self._all()
        if self._reaches(data, depends_on, task_id):
            raise DependencyError("That would make the tasks wait for each other in a circle, so I didn't record it.")
        deps = data.setdefault(task_id, [])
        if depends_on in deps:
            return False
        deps.append(depends_on)
        self._save(data)
        return True

    def remove(self, task_id: str, depends_on: str) -> bool:
        data = self._all()
        deps = data.get(task_id, [])
        if depends_on not in deps:
            return False
        deps.remove(depends_on)
        if not deps:
            data.pop(task_id)
        self._save(data)
        return True

    @staticmethod
    def _reaches(data: dict[str, list[str]], start: str, target: str) -> bool:
        """True if `target` is reachable from `start` by following "depends on" edges."""
        seen, stack = set(), [start]
        while stack:
            node = stack.pop()
            if node == target:
                return True
            if node in seen:
                continue
            seen.add(node)
            stack.extend(data.get(node, []))
        return False

    def prune(self, existing_task_ids: set[str]) -> int:
        """Drop dependencies that mention tasks which no longer exist. Returns how many edges were removed."""
        data, removed = self._all(), 0
        for task_id in list(data):
            if task_id not in existing_task_ids:
                removed += len(data.pop(task_id))
                continue
            keep = [d for d in data[task_id] if d in existing_task_ids]
            removed += len(data[task_id]) - len(keep)
            if keep:
                data[task_id] = keep
            else:
                data.pop(task_id)
        if removed:
            self._save(data)
        return removed

    def status_for(self, task: TaskItem, tasks_by_id: dict[str, TaskItem]) -> DepStatus:
        if task.status == "completed":
            return DepStatus.COMPLETED
        if task.status == "cancelled":
            return DepStatus.CANCELLED
        if any((dep := tasks_by_id.get(d)) is not None and dep.is_open for d in self.dependencies_of(task.task_id)):
            return DepStatus.BLOCKED
        return DepStatus.IN_PROGRESS if task.status == "in_progress" else DepStatus.READY

    def blockers(self, task: TaskItem, tasks_by_id: dict[str, TaskItem]) -> list[TaskItem]:
        return [dep for d in self.dependencies_of(task.task_id) if (dep := tasks_by_id.get(d)) is not None and dep.is_open]

    def all_edges(self) -> list[tuple[str, str]]:
        return [(t, d) for t, deps in self._all().items() for d in deps]
