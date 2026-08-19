"""Dependency contracts used by the management API.

The HTTP layer deliberately talks in plain dictionaries.  This keeps it thin
and allows the storage/runner implementation to evolve without making FastAPI
routes part of the execution domain.
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

JsonObject = dict[str, Any]


class AdminBackend(Protocol):
    async def dashboard(self) -> JsonObject: ...

    async def list_accounts(self) -> list[JsonObject]: ...

    async def get_account(self, account_id: str) -> JsonObject | None: ...

    async def create_account(self, payload: Mapping[str, Any]) -> JsonObject: ...

    async def update_account(
        self, account_id: str, payload: Mapping[str, Any]
    ) -> JsonObject | None: ...

    async def browser_action(self, account_id: str, action: str) -> JsonObject: ...

    async def browser_batch(
        self, account_ids: Sequence[str], action: str
    ) -> JsonObject: ...

    async def browser_status(self) -> JsonObject: ...

    async def list_programs(self) -> list[JsonObject]: ...

    async def get_program(self, name: str) -> JsonObject | None: ...

    async def list_tasks(self, filters: Mapping[str, Any]) -> list[JsonObject]: ...

    async def create_task(self, payload: Mapping[str, Any]) -> JsonObject: ...

    async def get_task(self, task_id: str) -> JsonObject | None: ...

    async def update_task(
        self, task_id: str, payload: Mapping[str, Any]
    ) -> JsonObject | None: ...

    async def clone_task(self, task_id: str) -> JsonObject | None: ...

    async def set_task_enabled(self, task_id: str, enabled: bool) -> JsonObject | None: ...

    async def trigger_task(
        self, task_id: str, trigger: str, *, fire_key: str | None = None
    ) -> JsonObject | None: ...

    async def list_runs(self, filters: Mapping[str, Any]) -> list[JsonObject]: ...

    async def get_run(self, run_id: str) -> JsonObject | None: ...

    async def list_logs(self, run_id: str, after: str | None) -> list[JsonObject]: ...

    async def cancel_run(self, run_id: str) -> JsonObject | None: ...

    async def rerun(self, run_id: str) -> JsonObject | None: ...

    async def runtime_status(self) -> JsonObject: ...


class SettingsStore(Protocol):
    def get(self) -> JsonObject: ...

    def update(self, values: Mapping[str, Any]) -> JsonObject: ...


class AISettingsStore(SettingsStore, Protocol):
    def public(self) -> JsonObject: ...

    def templates(self) -> list[JsonObject]: ...

    def save_template(self, values: Mapping[str, Any]) -> JsonObject: ...


AITestCallable = Callable[[Mapping[str, Any]], Awaitable[JsonObject]]


@dataclass(slots=True)
class AdminServices:
    backend: AdminBackend
    ai_settings: AISettingsStore
    runtime_settings: SettingsStore
    test_ai: AITestCallable


class AdminAPIError(RuntimeError):
    """Expected service error that can safely be returned to an administrator."""

    def __init__(self, status_code: int, detail: str):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail
