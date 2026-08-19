from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

_SENSITIVE_FIELD = re.compile(
    r"(?:password|passwd|secret|token|authorization|cookie|api[_-]?key)", re.I
)


def _sanitize_text(value: str) -> str:
    value = re.sub(
        r"(?i)([a-z][a-z0-9+.-]*://[^\s/@:]+):[^\s/@]+@",
        r"\1:***@",
        value,
    )
    value = re.sub(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+", "Bearer ***", value)
    value = re.sub(
        r"(?i)\b(password|passwd|secret|token|authorization|cookie|api[_-]?key)"
        r"\s*[:=]\s*([^\s,;]+)",
        lambda match: f"{match.group(1)}=***",
        value,
    )
    return value if len(value) <= 4000 else value[:3999] + "…"


def _sanitize_value(value: Any, *, key: str | None = None, depth: int = 0) -> Any:
    if key and _SENSITIVE_FIELD.search(key):
        return "***"
    if depth > 8:
        return "[truncated]"
    if isinstance(value, str):
        return _sanitize_text(value)
    if isinstance(value, Mapping):
        return {
            str(item_key): _sanitize_value(item, key=str(item_key), depth=depth + 1)
            for item_key, item in value.items()
        }
    if isinstance(value, (list, tuple, set)):
        return [_sanitize_value(item, depth=depth + 1) for item in value]
    return value


def utc_now() -> datetime:
    return datetime.now(UTC)


class RunStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    UNCERTAIN = "uncertain"
    CANCELLED = "cancelled"

    @property
    def terminal(self) -> bool:
        return self in {
            RunStatus.SUCCEEDED,
            RunStatus.FAILED,
            RunStatus.UNCERTAIN,
            RunStatus.CANCELLED,
        }


class BrowserEndPolicy(StrEnum):
    KEEP_OPEN = "keep_open"
    CLOSE = "close"


@dataclass(frozen=True, slots=True)
class Task:
    id: str
    name: str
    program_name: str
    account_ids: tuple[str, ...]
    params: Mapping[str, Any]
    enabled: bool = True
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        normalized = tuple(dict.fromkeys(value for value in self.account_ids if value))
        if not normalized:
            raise ValueError("Task must select at least one account")
        object.__setattr__(self, "account_ids", normalized)


@dataclass(frozen=True, slots=True)
class TaskRunSnapshot:
    id: str
    program_name: str
    account_id: str
    params: Mapping[str, Any]
    status: RunStatus
    created_at: datetime
    task_id: str | None = None
    trigger_id: str | None = None
    rerun_of: str | None = None
    requested_program_version: str | None = None
    program_version: str | None = None
    browser_end_policy: BrowserEndPolicy = BrowserEndPolicy.KEEP_OPEN
    deadline: datetime | None = None
    cancel_requested_at: datetime | None = None
    claimed_by: str | None = None
    claimed_at: datetime | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    output: Mapping[str, Any] | None = None
    error: Mapping[str, Any] | None = None
    cleanup_warnings: tuple[Mapping[str, Any], ...] = ()


@dataclass(frozen=True, slots=True)
class AccountRecord:
    id: str
    name: str
    browser_account_id: str | None
    username: str | None = None
    tags: tuple[str, ...] = ()
    metadata: Mapping[str, Any] = field(default_factory=dict)
    enabled: bool = True
    archived: bool = False


@dataclass(frozen=True, slots=True)
class TaskLogRecord:
    id: int
    task_run_id: str
    account_id: str
    level: str
    message: str
    fields: Mapping[str, Any]
    created_at: datetime


@dataclass(frozen=True, slots=True)
class RunError:
    code: str
    message: str
    source: str = "x-ops"
    retryable: bool = False
    details: Mapping[str, Any] = field(default_factory=dict)
    exception_type: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "message", _sanitize_text(self.message))
        object.__setattr__(self, "details", _sanitize_value(self.details))

    def to_dict(self) -> dict[str, Any]:
        value: dict[str, Any] = {
            "code": self.code,
            "message": self.message,
            "source": self.source,
            "retryable": self.retryable,
            "details": dict(self.details),
        }
        if self.exception_type:
            value["exception_type"] = self.exception_type
        return value


@dataclass(frozen=True, slots=True)
class RunOutcome:
    status: RunStatus
    output: Mapping[str, Any] | None = None
    error: RunError | None = None

    @classmethod
    def succeeded(cls, output: Mapping[str, Any]) -> RunOutcome:
        return cls(RunStatus.SUCCEEDED, output=output)

    @classmethod
    def failed(cls, error: RunError) -> RunOutcome:
        return cls(RunStatus.FAILED, error=error)

    @classmethod
    def uncertain(cls, error: RunError) -> RunOutcome:
        return cls(RunStatus.UNCERTAIN, error=error)

    @classmethod
    def cancelled(cls, error: RunError) -> RunOutcome:
        return cls(RunStatus.CANCELLED, error=error)


@dataclass(frozen=True, slots=True)
class CleanupReport:
    warnings: tuple[Mapping[str, Any], ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "warnings",
            tuple(_sanitize_value(warning) for warning in self.warnings),
        )
