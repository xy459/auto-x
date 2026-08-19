from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

from ..models import utc_now


class LogSink(Protocol):
    def append_log_now(
        self,
        *,
        task_run_id: str,
        account_id: str,
        level: str,
        message: str,
        fields: dict[str, Any],
        created_at: datetime | None = None,
    ) -> None: ...


@dataclass(frozen=True, slots=True)
class TaskLogger:
    sink: LogSink
    task_run_id: str
    task_id: str | None
    program_name: str
    program_version: str
    account_id: str
    runner_id: str

    def _write(self, level: str, message: str, **fields: Any) -> None:
        common = {
            "task_id": self.task_id,
            "task_run_id": self.task_run_id,
            "program_name": self.program_name,
            "program_version": self.program_version,
            "account_id": self.account_id,
            "runner_id": self.runner_id,
        }
        try:
            self.sink.append_log_now(
                task_run_id=self.task_run_id,
                account_id=self.account_id,
                level=level,
                message=message,
                fields={**common, **fields},
                created_at=utc_now(),
            )
        except Exception:
            # Observability must never prevent browser cleanup or final state
            # persistence. Storage health is exposed separately by the service.
            return

    def debug(self, message: str, **fields: Any) -> None:
        self._write("debug", message, **fields)

    def info(self, message: str, **fields: Any) -> None:
        self._write("info", message, **fields)

    def warning(self, message: str, **fields: Any) -> None:
        self._write("warning", message, **fields)

    def error(self, message: str, **fields: Any) -> None:
        self._write("error", message, **fields)


class TaskLoggerFactory:
    def __init__(self, sink: LogSink) -> None:
        self._sink = sink

    def create(
        self,
        *,
        task_run_id: str,
        task_id: str | None,
        program_name: str,
        program_version: str,
        account_id: str,
        runner_id: str,
    ) -> TaskLogger:
        return TaskLogger(
            sink=self._sink,
            task_run_id=task_run_id,
            task_id=task_id,
            program_name=program_name,
            program_version=program_version,
            account_id=account_id,
            runner_id=runner_id,
        )
