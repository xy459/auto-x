"""Thin adapter from management operations to the x-ops core services."""
from __future__ import annotations

import asyncio
import logging
import os
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import fields, is_dataclass, replace
from datetime import UTC, date, datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import Any, Protocol, cast

import httpx
from pydantic import ValidationError

from x_ops.ai import AIConfigStore, ConfiguredAIService, test_ai_connection
from x_ops.integrations.browser_custom import BrowserCustomGateway
from x_ops.integrations.x_actions import BoundXActionsFactory
from x_ops.models import AccountRecord, BrowserEndPolicy, RunStatus, Task, utc_now
from x_ops.runner import AccountLockManager, ExecutionSlotManager, TaskRunner
from x_ops.storage import AccountStore, JsonAccountStore, SQLiteStore
from x_ops.task_programs.registry import TaskProgramRegistry
from x_ops.task_sdk import CancellationTokenFactory, TaskLoggerFactory

from .config_store import DEFAULT_RUNTIME_SETTINGS, JsonSettingsStore
from .contracts import AdminAPIError, AdminServices, JsonObject

LOGGER = logging.getLogger(__name__)


class BrowserControlClient(Protocol):
    async def list_accounts(self) -> list[JsonObject]: ...

    async def status(self) -> JsonObject: ...

    async def action(self, account_id: str, action: str) -> JsonObject: ...

    async def batch(self, account_ids: Sequence[str], action: str) -> JsonObject: ...


class BrowserCustomClient:
    """HTTP browser-custom client for explicitly remote management deployments."""

    def __init__(self, base_url: str, *, timeout: float = 20.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    async def _request(self, method: str, path: str, **kwargs: Any) -> JsonObject:
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.request(method, f"{self.base_url}{path}", **kwargs)
                response.raise_for_status()
                value = response.json()
        except httpx.HTTPStatusError as exc:
            detail = exc.response.text[:500]
            raise AdminAPIError(exc.response.status_code, f"browser-custom: {detail}") from exc
        except (httpx.HTTPError, ValueError) as exc:
            raise AdminAPIError(502, f"browser-custom 不可用：{exc}") from exc
        return value if isinstance(value, dict) else {"value": value}

    async def list_accounts(self) -> list[JsonObject]:
        return list((await self._request("GET", "/api/accounts")).get("accounts", []))

    async def status(self) -> JsonObject:
        return await self._request("GET", "/api/browser/status")

    async def action(self, account_id: str, action: str) -> JsonObject:
        return await self._request("POST", f"/api/browser/{account_id}/{action}")

    async def batch(self, account_ids: Sequence[str], action: str) -> JsonObject:
        return await self._request(
            "POST", "/api/browser/batch", json={"action": action, "accounts": list(account_ids)}
        )


class InProcessBrowserCustomClient:
    """Manage browser-custom through the same process and SessionRegistry.

    Task Runner needs process-local Playwright ``Page`` objects. Keeping the
    management operations on the same registry also prevents a second process
    from trying to own the same persistent profile.
    """

    def __init__(self, config_store: Any, registry: Any) -> None:
        self._config_store = config_store
        self._registry = registry

    async def list_accounts(self) -> list[JsonObject]:
        # Reuse browser-custom's public representation so proxy passwords and
        # secret references stay out of x-ops responses.
        from browser_custom.api import list_accounts

        try:
            payload = list_accounts()
        except Exception as exc:  # noqa: BLE001
            raise AdminAPIError(500, f"browser-custom 账户读取失败：{exc}") from exc
        accounts = payload.get("accounts", [])
        return [dict(item) for item in accounts if isinstance(item, Mapping)]

    async def status(self) -> JsonObject:
        try:
            return {
                "accounts": self._registry.status(self._config_store.accounts.accounts)
            }
        except Exception as exc:  # noqa: BLE001
            raise AdminAPIError(500, f"browser-custom 状态读取失败：{exc}") from exc

    async def action(self, account_id: str, action: str) -> JsonObject:
        account = self._config_store.accounts.get(account_id)
        if account is None:
            raise AdminAPIError(404, f"未知 browser-custom 账户：{account_id}")
        try:
            if action == "open":
                return {
                    "ok": True,
                    **await self._registry.open(account, self._config_store.accounts),
                }
            if action == "close":
                return {"ok": True, **await self._registry.close(account)}
            if action == "restart":
                await self._registry.restart(account, self._config_store.accounts)
                return {"ok": True, "running": True}
        except Exception as exc:  # noqa: BLE001
            raise AdminAPIError(500, f"browser-custom {action} 失败：{exc}") from exc
        raise AdminAPIError(400, f"未知浏览器操作：{action}")

    async def batch(self, account_ids: Sequence[str], action: str) -> JsonObject:
        results: list[JsonObject] = []
        for account_id in dict.fromkeys(account_ids):
            try:
                value = await self.action(account_id, action)
                results.append({"acc": account_id, "ok": True, **value})
            except AdminAPIError as exc:
                results.append({"acc": account_id, "ok": False, "error": exc.detail})
        return {
            "ok": all(item["ok"] for item in results),
            "action": action,
            "results": results,
        }

    async def close_all(self) -> None:
        await self._registry.close_all(self._config_store.accounts.accounts)


class CoreAdminBackend:
    def __init__(
        self,
        *,
        store: SQLiteStore,
        account_store: AccountStore,
        registry: TaskProgramRegistry,
        runner: TaskRunner,
        browser_client: BrowserControlClient,
        task_metadata: JsonSettingsStore,
        execution_slots: ExecutionSlotManager,
        local_browser_store: Any | None = None,
        runtime_settings: JsonSettingsStore | None = None,
    ) -> None:
        self.store = store
        self.account_store = account_store
        self.registry = registry
        self.runner = runner
        self.browser_client = browser_client
        self.task_metadata = task_metadata
        self.execution_slots = execution_slots
        self.local_browser_store = local_browser_store
        self.runtime_settings = runtime_settings
        self.account_locks = getattr(runner, "account_locks", AccountLockManager())
        self._jobs: set[asyncio.Task[None]] = set()

    async def start(self) -> None:
        await self.store.initialize()
        await self.store.recover_interrupted()
        retention_days = int(self._runtime().get("task_log_retention_days", 30))
        await self.store.delete_logs_before(utc_now() - timedelta(days=retention_days))
        if self.local_browser_store is not None:
            self.local_browser_store.reload()
        await self._sync_browser_accounts(tolerate_unavailable=True)
        # A process may stop after a run is persisted but before its asyncio
        # job starts. Recovery clears stale queue claims; submit every remaining
        # queued run so restart never leaves it stranded indefinitely.
        self._submit_runs(await self.store.list_runs(status=RunStatus.QUEUED))

    async def close(self) -> None:
        if self._jobs:
            jobs = tuple(self._jobs)
            for job in jobs:
                if job.get_name().startswith("task-run:"):
                    await self.store.request_cancel(job.get_name().split(":", 1)[1])
            _done, pending = await asyncio.wait(jobs, timeout=10)
            for job in pending:
                job.cancel()
            if pending:
                _done, pending = await asyncio.wait(pending, timeout=5)
            if pending:
                LOGGER.error("runner jobs did not stop during shutdown", extra={"count": len(pending)})
            await self.store.recover_interrupted()
        close_browsers = getattr(self.browser_client, "close_all", None)
        if close_browsers is not None:
            try:
                await close_browsers()
            except Exception:  # noqa: BLE001
                LOGGER.exception("failed to close browser-custom sessions during shutdown")
        await self.store.close()

    def _runtime(self) -> JsonObject:
        return self.runtime_settings.get() if self.runtime_settings else dict(DEFAULT_RUNTIME_SETTINGS)

    async def apply_runtime_settings(self, settings: Mapping[str, Any]) -> None:
        await self.execution_slots.set_limit(int(settings["max_concurrent_browser_tasks"]))
        self.runner.cancellation_factory.set_poll_interval(
            float(settings["cancellation_poll_interval_seconds"])
        )
        self.runner.browser_acquire_timeout_seconds = float(
            settings["browser_acquire_timeout_seconds"]
        )
        retention_days = int(settings["task_log_retention_days"])
        await self.store.delete_logs_before(utc_now() - timedelta(days=retention_days))

    async def _sync_browser_accounts(self, *, tolerate_unavailable: bool) -> list[JsonObject]:
        try:
            browser_accounts = await self.browser_client.list_accounts()
        except AdminAPIError:
            if not tolerate_unavailable:
                raise
            return []
        known_accounts = await self.account_store.list_accounts()
        known = {account.id: account for account in known_accounts}
        known_by_browser_id = {
            account.browser_account_id: account
            for account in known_accounts
            if account.browser_account_id
        }
        live_browser_ids: set[str] = set()
        for browser in browser_accounts:
            browser_id = str(browser.get("acc") or browser.get("id") or "")
            if not browser_id:
                continue
            live_browser_ids.add(browser_id)
            existing = known_by_browser_id.get(browser_id) or known.get(browser_id)
            detected_username = str(browser.get("xUsername") or "").strip().lstrip("@") or None
            if existing is None:
                await self.account_store.create_account(
                    AccountRecord(
                        id=browser_id,
                        name=str(browser.get("name") or browser.get("display_name") or browser_id),
                        browser_account_id=browser_id,
                        username=detected_username,
                    )
                )
            elif existing.archived or (
                detected_username and detected_username != existing.username
            ):
                await self.account_store.update_account(
                    existing.id,
                    archived=False,
                    username=detected_username or existing.username,
                )
        for account in known_accounts:
            if (
                account.browser_account_id
                and account.browser_account_id not in live_browser_ids
                and not account.archived
            ):
                await self.account_store.update_account(account.id, archived=True)
        return browser_accounts

    async def dashboard(self) -> JsonObject:
        accounts = await self.list_accounts()
        runs = await self.list_runs({})
        tasks = await self.list_tasks({"enabled": True})
        today = datetime.now(UTC).date()
        running = [run for run in runs if run["status"] == "running"]
        failures = [run for run in runs if run["status"] in {"failed", "uncertain"}]
        return {
            "summary": {
                "account_count": len(accounts),
                "running_browsers": sum(browser_state(item) == "running" for item in accounts),
                "running_runs": len(running),
                "queued_runs": sum(run["status"] == "queued" for run in runs),
                "succeeded_today": sum(
                    run["status"] == "succeeded" and _date(run.get("finished_at")) == today
                    for run in runs
                ),
                "failed_today": sum(
                    run["status"] == "failed" and _date(run.get("finished_at")) == today
                    for run in runs
                ),
                "uncertain_runs": sum(run["status"] == "uncertain" for run in runs),
                "slots_used": self.execution_slots.active,
                "slots_total": self.execution_slots.limit,
            },
            "running": running[:20],
            "failures": failures[:20],
            "upcoming": [
                task
                for task in tasks
                if isinstance(task.get("schedule"), Mapping)
                and task["schedule"].get("enabled", True)
            ][:20],
        }

    async def list_accounts(self) -> list[JsonObject]:
        browser_accounts = await self._sync_browser_accounts(tolerate_unavailable=True)
        browser_by_id = {
            str(item.get("acc") or item.get("id")): item for item in browser_accounts
        }
        browser_order = {
            str(item.get("acc") or item.get("id")): index
            for index, item in enumerate(browser_accounts)
            if item.get("acc") or item.get("id")
        }
        runs = await self.store.list_runs()
        result = []
        accounts = [
            account
            for account in await self.account_store.list_accounts()
            if not account.archived
        ]
        accounts.sort(
            key=lambda account: (
                0,
                browser_order[account.browser_account_id or account.id],
            )
            if (account.browser_account_id or account.id) in browser_order
            else (1, account.id)
        )
        for account in accounts:
            browser = browser_by_id.get(account.browser_account_id or account.id, {})
            account_runs = [run for run in runs if run.account_id == account.id]
            task_status = (
                "running"
                if any(run.status is RunStatus.RUNNING for run in account_runs)
                else "queued"
                if any(run.status is RunStatus.QUEUED for run in account_runs)
                else "idle"
            )
            result.append(
                {
                    **browser,
                    **serialize(account),
                    "id": account.id,
                    "browser_account_id": account.browser_account_id,
                    "task_status": task_status,
                    "browser_status": browser_state(browser),
                    "last_run": serialize_run(account_runs[-1]) if account_runs else None,
                }
            )
        return result

    async def get_account(self, account_id: str) -> JsonObject | None:
        accounts = await self.list_accounts()
        return next((item for item in accounts if item["id"] == account_id), None)

    async def create_account(self, payload: Mapping[str, Any]) -> JsonObject:
        account_id = str(payload.get("id") or payload.get("browser_account_id") or uuid.uuid4())
        try:
            account = AccountRecord(
                id=account_id,
                name=str(payload["name"]),
                browser_account_id=str(payload["browser_account_id"]),
                username=payload.get("username"),
                tags=tuple(payload.get("tags") or ()),
                metadata={
                    "x_user_id": payload.get("x_user_id"),
                    "note": payload.get("note", ""),
                },
                enabled=bool(payload.get("enabled", True)),
            )
            await self.account_store.create_account(account)
        except ValueError as exc:
            raise AdminAPIError(409, str(exc)) from exc
        except (KeyError, TypeError) as exc:
            raise AdminAPIError(400, str(exc)) from exc
        return cast(JsonObject, serialize(account))

    async def update_account(
        self, account_id: str, payload: Mapping[str, Any]
    ) -> JsonObject | None:
        account = await self.account_store.get_account(account_id)
        if account is None:
            return None
        metadata = dict(account.metadata)
        if "x_user_id" in payload:
            metadata["x_user_id"] = payload["x_user_id"]
        if "note" in payload:
            metadata["note"] = payload["note"]
        changes = {
            key: value
            for key, value in {
                "name": payload.get("name"),
                "browser_account_id": payload.get("browser_account_id"),
                "username": payload.get("username") if "username" in payload else account.username,
                "tags": tuple(payload["tags"]) if "tags" in payload else account.tags,
                "enabled": payload.get("enabled") if "enabled" in payload else account.enabled,
                "metadata": metadata,
            }.items()
            if value is not None
        }
        try:
            updated = await self.account_store.update_account(account_id, **changes)
        except ValueError as exc:
            raise AdminAPIError(409, str(exc)) from exc
        return serialize(updated) if updated else None

    async def browser_action(self, account_id: str, action: str) -> JsonObject:
        account = await self.account_store.get_account(account_id)
        if account is None:
            raise AdminAPIError(404, "账户不存在")
        if not account.browser_account_id:
            raise AdminAPIError(409, "账户尚未绑定 browser-custom 账户")
        if action not in {"close", "restart"}:
            return await self.browser_client.action(account.browser_account_id, action)

        profile_lock = await self.account_locks.try_acquire(account.browser_account_id)
        if profile_lock is None:
            raise AdminAPIError(409, "浏览器 Profile 正在执行任务，请先取消任务或等待任务结束")
        async with profile_lock:
            active_runs = await self._active_runs_for_profile(account.browser_account_id)
            if active_runs:
                raise AdminAPIError(409, "浏览器 Profile 正在执行任务，请先取消任务或等待任务结束")
            return await self.browser_client.action(account.browser_account_id, action)

    async def browser_batch(
        self, account_ids: Sequence[str], action: str
    ) -> JsonObject:
        if action == "refresh":
            return await self.browser_status()
        resolved = []
        for account_id in account_ids:
            account = await self.account_store.get_account(account_id)
            if account and account.browser_account_id:
                resolved.append(account.browser_account_id)
        if not resolved:
            raise AdminAPIError(400, "没有可操作的 browser-custom 账户")
        if action not in {"close", "restart"}:
            return await self.browser_client.batch(resolved, action)
        results = []
        for account_id in dict.fromkeys(account_ids):
            try:
                value = await self.browser_action(account_id, action)
                results.append({"account_id": account_id, "ok": True, **value})
            except AdminAPIError as exc:
                results.append({"account_id": account_id, "ok": False, "error": exc.detail})
        return {"ok": all(item["ok"] for item in results), "action": action, "results": results}

    async def _active_runs_for_profile(self, browser_account_id: str) -> list[Any]:
        accounts = await self.account_store.list_accounts()
        account_ids = {
            account.id
            for account in accounts
            if account.browser_account_id == browser_account_id
        }
        return [
            run
            for run in await self.store.list_runs(status=RunStatus.RUNNING)
            if run.account_id in account_ids
        ]

    async def browser_status(self) -> JsonObject:
        return await self.browser_client.status()

    async def list_programs(self) -> list[JsonObject]:
        return self.registry.describe()

    async def get_program(self, name: str) -> JsonObject | None:
        return next((item for item in self.registry.describe() if item["name"] == name), None)

    async def list_tasks(self, filters: Mapping[str, Any]) -> list[JsonObject]:
        tasks = await self.store.list_tasks(enabled=filters.get("enabled"))
        result = [self._task_public(task) for task in tasks]
        if program_name := filters.get("program_name"):
            result = [task for task in result if task["program_name"] == program_name]
        if account_id := filters.get("account_id"):
            result = [task for task in result if account_id in task["account_ids"]]
        if search := str(filters.get("search") or "").casefold():
            result = [task for task in result if search in task["name"].casefold()]
        return result

    async def create_task(self, payload: Mapping[str, Any]) -> JsonObject:
        program = self.registry.get(str(payload.get("program_name") or ""))
        if program is None:
            raise AdminAPIError(400, "任务程序不存在")
        try:
            params = program.Params.model_validate(payload.get("params") or {}).model_dump(
                mode="json"
            )
            task = Task(
                id=str(uuid.uuid4()),
                name=str(payload["name"]),
                program_name=program.SPEC.name,
                account_ids=tuple(payload.get("account_ids") or ()),
                params=params,
                enabled=bool(payload.get("enabled", True)),
            )
            await self.store.create_task(task)
        except (ValidationError, KeyError, TypeError, ValueError) as exc:
            raise AdminAPIError(400, str(exc)) from exc
        self._save_task_meta(task.id, payload)
        return self._task_public(task)

    async def get_task(self, task_id: str) -> JsonObject | None:
        task = await self.store.get_task(task_id)
        return self._task_public(task) if task else None

    async def update_task(
        self, task_id: str, payload: Mapping[str, Any]
    ) -> JsonObject | None:
        task = await self.store.get_task(task_id)
        if task is None:
            return None
        program_name = str(payload.get("program_name") or task.program_name)
        program = self.registry.get(program_name)
        if program is None:
            raise AdminAPIError(400, "任务程序不存在")
        try:
            params = (
                program.Params.model_validate(payload["params"]).model_dump(mode="json")
                if "params" in payload
                else dict(task.params)
            )
            updated = replace(
                task,
                name=str(payload.get("name") or task.name),
                program_name=program_name,
                account_ids=tuple(payload.get("account_ids") or task.account_ids),
                params=params,
                enabled=bool(payload.get("enabled", task.enabled)),
                updated_at=utc_now(),
            )
            await self.store.update_task(updated)
        except (ValidationError, TypeError, ValueError) as exc:
            raise AdminAPIError(400, str(exc)) from exc
        self._save_task_meta(task_id, payload)
        return self._task_public(updated)

    async def clone_task(self, task_id: str) -> JsonObject | None:
        task = await self.store.get_task(task_id)
        if task is None:
            return None
        clone = replace(task, id=str(uuid.uuid4()), name=f"{task.name}（副本）", created_at=utc_now(), updated_at=utc_now())
        await self.store.create_task(clone)
        self._save_task_meta(clone.id, self._task_meta(task.id))
        return self._task_public(clone)

    async def delete_task(self, task_id: str) -> bool:
        try:
            deleted = await self.store.delete_task(task_id)
        except ValueError as exc:
            raise AdminAPIError(409, str(exc)) from exc
        if deleted:
            settings = self.task_metadata.get()
            tasks = dict(settings.get("tasks", {}))
            tasks.pop(task_id, None)
            self.task_metadata.update({"tasks": tasks})
        return deleted

    async def set_task_enabled(self, task_id: str, enabled: bool) -> JsonObject | None:
        return await self.update_task(task_id, {"enabled": enabled})

    async def trigger_task(
        self, task_id: str, trigger: str, *, fire_key: str | None = None
    ) -> JsonObject | None:
        task = await self.store.get_task(task_id)
        if task is None:
            return None
        meta = self._task_meta(task_id)
        runtime = self._runtime()
        try:
            runs = await self.store.trigger_task(
                task_id,
                trigger_id=f"{trigger}:{fire_key or uuid.uuid4()}",
                browser_end_policy=BrowserEndPolicy(
                    meta.get("browser_end_policy")
                    or runtime.get("default_browser_end_policy", "keep_open")
                ),
                deadline=utc_now()
                + timedelta(
                    seconds=int(
                        meta.get("task_timeout_seconds")
                        or runtime.get("default_task_timeout_seconds", 3600)
                    )
                ),
                fire_key=fire_key,
            )
        except KeyError:
            return None
        except ValueError as exc:
            raise AdminAPIError(409, str(exc)) from exc
        self._submit_runs(runs)
        return {
            "trigger": trigger,
            "trigger_id": runs[0].trigger_id if runs else None,
            "runs": [serialize_run(run) for run in runs],
            "duplicate": fire_key is not None and not runs,
        }

    async def list_runs(self, filters: Mapping[str, Any]) -> list[JsonObject]:
        status = filters.get("status")
        try:
            run_status = RunStatus(status) if status else None
        except ValueError as exc:
            raise AdminAPIError(400, "未知 TaskRun 状态") from exc
        runs = await self.store.list_runs(
            task_id=filters.get("task_id"), account_id=filters.get("account_id"), status=run_status
        )
        result = [serialize_run(run) for run in reversed(runs)]
        if program := filters.get("program_name"):
            result = [run for run in result if run["program_name"] == program]
        if trigger := filters.get("trigger"):
            result = [run for run in result if run["trigger"] == trigger]
        return result

    async def get_run(self, run_id: str) -> JsonObject | None:
        run = await self.store.get_run(run_id)
        return serialize_run(run) if run else None

    async def list_logs(self, run_id: str, after: str | None) -> list[JsonObject]:
        logs = await self.store.list_logs(run_id)
        if after is not None:
            try:
                after_id = int(after)
                logs = [log for log in logs if log.id > after_id]
            except ValueError as exc:
                raise AdminAPIError(400, "after 必须是日志 ID") from exc
        return [serialize(log) for log in logs]

    async def cancel_run(self, run_id: str) -> JsonObject | None:
        run = await self.store.get_run(run_id)
        if run is None:
            return None
        accepted = await self.store.request_cancel(run_id)
        updated = await self.store.get_run(run_id)
        return {"accepted": accepted, "run": serialize_run(updated or run)}

    async def rerun(self, run_id: str) -> JsonObject | None:
        if await self.store.get_run(run_id) is None:
            return None
        run = await self.store.create_rerun(run_id)
        self._submit_runs([run])
        return {"run": serialize_run(run)}

    async def delete_run(self, run_id: str) -> bool:
        try:
            return await self.store.delete_run(run_id)
        except ValueError as exc:
            raise AdminAPIError(409, str(exc)) from exc

    async def runtime_status(self) -> JsonObject:
        runs = await self.store.list_runs()
        try:
            await self.browser_client.status()
            browser_available = True
        except AdminAPIError:
            browser_available = False
        return {
            "browser-custom": browser_available,
            "Task Runner": True,
            "已注册任务程序": len(self.registry.list()),
            "浏览器并发槽位": f"{self.execution_slots.active} / {self.execution_slots.limit}",
            "当前任务队列": sum(run.status is RunStatus.QUEUED for run in runs),
        }

    def _submit_runs(self, runs: Sequence[Any]) -> None:
        for run in runs:
            job = asyncio.create_task(self.runner.execute(run.id), name=f"task-run:{run.id}")
            self._jobs.add(job)
            job.add_done_callback(self._job_done)

    def _job_done(self, job: asyncio.Task[None]) -> None:
        self._jobs.discard(job)
        if job.cancelled():
            return
        error = job.exception()
        if error is not None:
            LOGGER.error(
                "Task Runner job terminated unexpectedly",
                exc_info=(type(error), error, error.__traceback__),
            )

    def _task_public(self, task: Task) -> JsonObject:
        return {**serialize(task), **self._task_meta(task.id)}

    def _task_meta(self, task_id: str) -> JsonObject:
        tasks = self.task_metadata.get().get("tasks", {})
        value = tasks.get(task_id, {}) if isinstance(tasks, dict) else {}
        return dict(value) if isinstance(value, dict) else {}

    def _save_task_meta(self, task_id: str, payload: Mapping[str, Any]) -> None:
        settings = self.task_metadata.get()
        tasks = dict(settings.get("tasks", {}))
        current = dict(tasks.get(task_id, {}))
        for key in (
            "description",
            "schedule",
            "browser_end_policy",
            "task_timeout_seconds",
        ):
            if key in payload:
                current[key] = payload[key]
        tasks[task_id] = current
        self.task_metadata.update({"tasks": tasks})


def default_data_dir() -> Path:
    """Return a stable app-local data directory independent of the shell cwd."""
    app_root = Path(__file__).resolve().parents[3]
    configured = os.environ.get("X_OPS_DATA_DIR")
    return Path(configured).expanduser().resolve() if configured else app_root / "data"


def build_default_services() -> AdminServices:
    data_dir = default_data_dir()
    data_dir.mkdir(parents=True, exist_ok=True)
    store = SQLiteStore(data_dir / "x-ops.sqlite3")
    account_store = JsonAccountStore(data_dir / "accounts.json")
    registry = TaskProgramRegistry.default()
    runtime = JsonSettingsStore(data_dir / "runtime.json", DEFAULT_RUNTIME_SETTINGS)
    ai_settings = AIConfigStore(data_dir / "ai.json")
    execution_slots = ExecutionSlotManager(
        int(runtime.get()["max_concurrent_browser_tasks"])
    )
    try:
        from browser_custom.browser.registry import session_registry
        from browser_custom.config import store as browser_store

        def resolve_browser(account_id: str) -> tuple[Any, Any]:
            account = browser_store.accounts.get(account_id)
            if account is None:
                raise KeyError(f"未知 browser-custom 账户: {account_id}")
            return account, browser_store.accounts

        browser_gateway = BrowserCustomGateway(resolve_browser, registry=session_registry)
        browser_client: BrowserControlClient = InProcessBrowserCustomClient(
            browser_store, session_registry
        )
    except ImportError as exc:
        raise RuntimeError(
            "x-ops 需要安装同仓库的 browser-custom；"
            "请先执行 python -m pip install -e apps/browser-custom"
        ) from exc
    runner = TaskRunner(
        runner_id=f"admin-{uuid.uuid4().hex[:8]}",
        run_store=store,
        account_store=account_store,
        program_registry=registry,
        account_locks=AccountLockManager(),
        execution_slots=execution_slots,
        browser_gateway=browser_gateway,
        actions_factory=BoundXActionsFactory(),
        ai_service=ConfiguredAIService(ai_settings),
        logger_factory=TaskLoggerFactory(store),
        cancellation_factory=CancellationTokenFactory(
            store.is_cancel_requested,
            poll_interval=float(runtime.get()["cancellation_poll_interval_seconds"]),
        ),
        browser_acquire_timeout_seconds=float(
            runtime.get()["browser_acquire_timeout_seconds"]
        ),
    )
    backend = CoreAdminBackend(
        store=store,
        account_store=account_store,
        registry=registry,
        runner=runner,
        browser_client=browser_client,
        task_metadata=JsonSettingsStore(data_dir / "task-metadata.json", {"tasks": {}}),
        execution_slots=execution_slots,
        local_browser_store=browser_store,
        runtime_settings=runtime,
    )
    return AdminServices(
        backend=backend,
        ai_settings=ai_settings,
        runtime_settings=runtime,
        test_ai=lambda request: test_ai_connection(ai_settings, request),
    )


def serialize(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat()
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value):
        return {field.name: serialize(getattr(value, field.name)) for field in fields(value)}
    if isinstance(value, Mapping):
        return {str(key): serialize(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [serialize(item) for item in value]
    return str(value)


def serialize_run(run: Any) -> JsonObject:
    value = cast(JsonObject, serialize(run))
    trigger_id = str(value.get("trigger_id") or "")
    if value.get("rerun_of") or trigger_id.startswith("rerun:"):
        trigger = "rerun"
    elif ":" in trigger_id:
        trigger = trigger_id.split(":", 1)[0]
    else:
        trigger = "manual"
    value["trigger"] = trigger
    return value


def browser_state(account: Mapping[str, Any]) -> str:
    runtime = account.get("runtime")
    if isinstance(runtime, Mapping):
        return str(runtime.get("status") or "stopped")
    return str(account.get("browser_status") or account.get("status") or "stopped")


def _date(value: Any) -> date | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value)).date()
    except ValueError:
        return None
