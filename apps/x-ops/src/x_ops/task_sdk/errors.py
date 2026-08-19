from __future__ import annotations

from collections.abc import Mapping
from typing import Any


class TaskCancelledError(RuntimeError):
    def __init__(self, task_run_id: str) -> None:
        super().__init__(f"TaskRun {task_run_id} was cancelled")
        self.task_run_id = task_run_id


class TaskTimeoutError(RuntimeError):
    def __init__(self, task_run_id: str) -> None:
        super().__init__(f"TaskRun {task_run_id} reached its deadline")
        self.task_run_id = task_run_id


class TaskUncertainError(RuntimeError):
    def __init__(
        self,
        message: str = "An action was triggered but its final state is unknown",
        *,
        action_id: str | None = None,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.action_id = action_id
        self.details = dict(details or {})
