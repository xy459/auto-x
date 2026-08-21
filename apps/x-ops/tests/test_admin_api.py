from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from x_ops.ai.settings import AIConfigStore
from x_ops.api.backend import (
    CoreAdminBackend,
    InProcessBrowserCustomClient,
    default_data_dir,
)
from x_ops.api.config_store import DEFAULT_RUNTIME_SETTINGS, JsonSettingsStore
from x_ops.api.contracts import AdminAPIError, AdminServices
from x_ops.app import create_app
from x_ops.models import AccountRecord, RunStatus, TaskRunSnapshot
from x_ops.runner import AccountLockManager, ExecutionSlotManager
from x_ops.storage import JsonAccountStore, SQLiteStore
from x_ops.task_programs.registry import TaskProgramRegistry


class FakeBackend:
    def __init__(self):
        self.accounts = [
            {
                "id": "account-1",
                "name": "测试账户",
                "browser_account_id": "browser-1",
                "browser_status": "running",
                "task_status": "idle",
            },
            {
                "id": "account-2",
                "name": "第二账户",
                "browser_account_id": "browser-2",
                "browser_status": "stopped",
                "task_status": "idle",
            },
        ]
        self.program = {
            "name": "browse_only",
            "title": "浏览时间线",
            "version": "1.0.0",
            "description": "只浏览时间线",
            "params_schema": {
                "type": "object",
                "properties": {"scroll_count": {"type": "integer", "minimum": 1}},
                "required": ["scroll_count"],
            },
        }
        self.tasks = []
        self.runs = []
        self.logs = {}
        self.browser_calls = []

    async def dashboard(self):
        return {
            "summary": {"account_count": len(self.accounts), "running_browsers": 1},
            "running": [],
            "failures": [],
            "upcoming": [],
        }

    async def list_accounts(self):
        return deepcopy(self.accounts)

    async def get_account(self, account_id):
        return next((deepcopy(x) for x in self.accounts if x["id"] == account_id), None)

    async def create_account(self, payload):
        value = {"id": payload["browser_account_id"], **payload}
        self.accounts.append(value)
        return deepcopy(value)

    async def update_account(self, account_id, payload):
        value = next((x for x in self.accounts if x["id"] == account_id), None)
        if value:
            value.update(payload)
        return deepcopy(value)

    async def browser_action(self, account_id, action):
        self.browser_calls.append((action, [account_id]))
        return {"ok": True}

    async def browser_batch(self, account_ids, action):
        self.browser_calls.append((action, list(account_ids)))
        return {"ok": True, "results": []}

    async def browser_status(self):
        return {"accounts": []}

    async def list_programs(self):
        return [deepcopy(self.program)]

    async def get_program(self, name):
        return deepcopy(self.program) if name == self.program["name"] else None

    async def list_tasks(self, filters):
        return deepcopy(self.tasks)

    async def create_task(self, payload):
        task = {"id": "task-1", **deepcopy(payload)}
        self.tasks.append(task)
        return deepcopy(task)

    async def get_task(self, task_id):
        return next((deepcopy(x) for x in self.tasks if x["id"] == task_id), None)

    async def update_task(self, task_id, payload):
        task = next((x for x in self.tasks if x["id"] == task_id), None)
        if task:
            task.update(payload)
        return deepcopy(task)

    async def clone_task(self, task_id):
        source = await self.get_task(task_id)
        if source is None:
            return None
        source.update(id="task-copy", name=source["name"] + "（副本）")
        self.tasks.append(source)
        return deepcopy(source)

    async def delete_task(self, task_id):
        task = next((x for x in self.tasks if x["id"] == task_id), None)
        if task is None:
            return False
        self.tasks.remove(task)
        for run in self.runs:
            if run.get("task_id") == task_id:
                run["task_id"] = None
        return True

    async def set_task_enabled(self, task_id, enabled):
        return await self.update_task(task_id, {"enabled": enabled})

    async def trigger_task(self, task_id, trigger):
        if await self.get_task(task_id) is None:
            return None
        created = []
        for index, account_id in enumerate(self.tasks[0]["account_ids"], 1):
            run = {
                "id": f"run-{index}",
                "task_id": task_id,
                "account_id": account_id,
                "program_name": "browse_only",
                "params": {"scroll_count": 2},
                "status": "queued",
                "trigger_id": "trigger-1",
            }
            self.runs.append(run)
            created.append(deepcopy(run))
        return {"trigger": trigger, "trigger_id": "trigger-1", "runs": created}

    async def list_runs(self, filters):
        runs = self.runs
        if filters.get("status"):
            runs = [run for run in runs if run["status"] == filters["status"]]
        return deepcopy(runs)

    async def get_run(self, run_id):
        return next((deepcopy(x) for x in self.runs if x["id"] == run_id), None)

    async def list_logs(self, run_id, after):
        return deepcopy(self.logs.get(run_id, []))

    async def cancel_run(self, run_id):
        run = next((x for x in self.runs if x["id"] == run_id), None)
        if run is None:
            return None
        run["status"] = "cancelled"
        return {"accepted": True, "run": deepcopy(run)}

    async def rerun(self, run_id):
        old = await self.get_run(run_id)
        if old is None:
            return None
        new = {**old, "id": "rerun-1", "status": "queued", "rerun_of": run_id}
        self.runs.append(new)
        return {"run": deepcopy(new)}

    async def delete_run(self, run_id):
        run = next((x for x in self.runs if x["id"] == run_id), None)
        if run is None:
            return False
        if run["status"] in {"queued", "running"}:
            raise AdminAPIError(409, "运行记录尚未结束")
        self.runs.remove(run)
        self.logs.pop(run_id, None)
        return True

    async def runtime_status(self):
        return {"browser-custom": True, "Task Runner": True}


class MemorySecrets:
    def __init__(self):
        self.value = ""

    def get(self):
        return self.value

    def set(self, value):
        self.value = value


def services(tmp_path):
    backend = FakeBackend()
    ai = AIConfigStore(tmp_path / "ai.json", secret_store=MemorySecrets())
    runtime = JsonSettingsStore(tmp_path / "runtime.json", DEFAULT_RUNTIME_SETTINGS)

    async def test_ai(_request):
        return {"ok": True, "message": "连接成功"}

    return backend, AdminServices(backend, ai, runtime, test_ai)


def test_console_and_read_apis(tmp_path):
    _backend, configured = services(tmp_path)
    with TestClient(create_app(configured)) as client:
        console = client.get("/")
        assert console.status_code == 200
        assert "账户与浏览器" in console.text
        assert "TaskRun 状态" in console.text
        assert "工作流" not in console.text
        browser_console = client.get("/browser-custom/")
        assert browser_console.status_code == 200
        assert "Browser Custom" in browser_console.text
        assert client.get("/browser-custom/app.js").status_code == 200
        assert client.get("/api/health").json()["ok"] is True
        assert client.get("/api/dashboard").json()["summary"]["account_count"] == 2
        assert client.get("/api/task-programs/browse_only").json()["program"]["version"] == "1.0.0"


def test_task_multi_account_run_cancel_and_rerun(tmp_path):
    _backend, configured = services(tmp_path)
    with TestClient(create_app(configured)) as client:
        created = client.post(
            "/api/tasks",
            json={
                "name": "批量浏览",
                "program_name": "browse_only",
                "account_ids": ["account-1", "account-2"],
                "params": {"scroll_count": 2},
                "browser_end_policy": "keep_open",
            },
        )
        assert created.status_code == 201
        task_id = created.json()["task"]["id"]
        triggered = client.post(f"/api/tasks/{task_id}/run")
        assert triggered.status_code == 202
        assert {run["account_id"] for run in triggered.json()["runs"]} == {
            "account-1",
            "account-2",
        }
        cancelled = client.post("/api/task-runs/run-1/cancel")
        assert cancelled.status_code == 202
        assert cancelled.json()["run"]["status"] == "cancelled"
        rerun = client.post("/api/task-runs/run-1/rerun")
        assert rerun.status_code == 201
        assert rerun.json()["run"]["rerun_of"] == "run-1"

        deleted_run = client.delete("/api/task-runs/run-1")
        assert deleted_run.status_code == 200
        assert deleted_run.json()["deleted"] is True
        assert client.delete("/api/task-runs/run-1").status_code == 404

        deleted_task = client.delete(f"/api/tasks/{task_id}")
        assert deleted_task.status_code == 200
        assert deleted_task.json()["deleted"] is True
        assert client.delete(f"/api/tasks/{task_id}").status_code == 404


def test_browser_batch_and_runtime_settings(tmp_path):
    backend, configured = services(tmp_path)
    with TestClient(create_app(configured)) as client:
        response = client.post(
            "/api/accounts/browser/batch",
            json={"action": "restart", "account_ids": ["account-1", "account-2"]},
        )
        assert response.status_code == 200
        assert backend.browser_calls == [("restart", ["account-1", "account-2"])]
        settings = client.put(
            "/api/settings/runtime",
            json={
                "max_concurrent_browser_tasks": 8,
                "cancellation_poll_interval_seconds": 0.5,
                "default_task_timeout_seconds": 900,
                "browser_acquire_timeout_seconds": 90,
                "default_browser_end_policy": "close",
                "task_log_retention_days": 60,
                "queue_poll_interval_seconds": 2,
            },
        )
        assert settings.status_code == 200
        assert settings.json()["settings"]["max_concurrent_browser_tasks"] == 8


def test_ai_key_is_never_returned_or_written_to_settings_json(tmp_path):
    _backend, configured = services(tmp_path)
    with TestClient(create_app(configured)) as client:
        response = client.put(
            "/api/ai/settings",
            json={
                "provider": "openai",
                "base_url": "https://example.invalid/v1",
                "api_key": "secret-value",
                "model": "test-model",
                "timeout_seconds": 10,
            },
        )
        assert response.status_code == 200
        assert response.json()["api_key_configured"] is True
        assert "secret-value" not in response.text
        public = client.get("/api/ai/settings")
        assert "secret-value" not in public.text
        assert "secret-value" not in (tmp_path / "ai.json").read_text(encoding="utf-8")

        preserved = client.put("/api/ai/settings", json={"model": "next-model"})
        assert preserved.status_code == 200
        assert preserved.json()["api_key_configured"] is True

        cleared = client.put("/api/ai/settings", json={"api_key": ""})
        assert cleared.status_code == 200
        assert cleared.json()["api_key_configured"] is False


class FakeBrowserClient:
    async def list_accounts(self):
        return [
            {"acc": "account-1", "name": "A", "runtime": {"status": "running"}},
            {"acc": "account-2", "name": "B", "runtime": {"status": "stopped"}},
        ]

    async def status(self):
        return {"accounts": []}

    async def action(self, account_id, action):
        return {"ok": True, "account_id": account_id, "action": action}

    async def batch(self, account_ids, action):
        return {"ok": True, "account_ids": list(account_ids), "action": action}


class MutableBrowserClient(FakeBrowserClient):
    def __init__(self, accounts):
        self.accounts = accounts

    async def list_accounts(self):
        return list(self.accounts)


class NoopRunner:
    def __init__(self):
        self.executed = []
        self.account_locks = AccountLockManager()

    async def execute(self, run_id):
        self.executed.append(run_id)


async def test_core_backend_rejects_close_while_profile_is_busy(tmp_path):
    store = SQLiteStore(tmp_path / "runs.sqlite3")
    await store.initialize()
    account_store = JsonAccountStore(tmp_path / "accounts.json")
    await account_store.create_account(AccountRecord("account-1", "A", "browser-1"))
    runner = NoopRunner()
    backend = CoreAdminBackend(
        store=store,
        account_store=account_store,
        registry=TaskProgramRegistry.default(),
        runner=runner,
        browser_client=FakeBrowserClient(),
        task_metadata=JsonSettingsStore(tmp_path / "task-meta.json", {"tasks": {}}),
        execution_slots=ExecutionSlotManager(2),
    )
    held = await runner.account_locks.acquire("browser-1")

    with pytest.raises(AdminAPIError) as caught:
        await backend.browser_action("account-1", "close")

    assert caught.value.status_code == 409
    held.release()
    assert (await backend.browser_action("account-1", "close"))["ok"] is True

    await store.create_run(
        TaskRunSnapshot(
            id="running",
            program_name="browse_only",
            account_id="account-1",
            params={},
            status=RunStatus.QUEUED,
            created_at=datetime.now(UTC),
        )
    )
    assert await store.claim("running", "runner")
    assert await store.mark_running("running", "runner", "1.0.0")
    with pytest.raises(AdminAPIError) as active:
        await backend.browser_action("account-1", "restart")
    assert active.value.status_code == 409
    await store.close()


async def test_account_list_follows_browser_creation_order_and_hides_deleted_accounts(tmp_path):
    store = SQLiteStore(tmp_path / "runs.sqlite3")
    await store.initialize()
    account_store = JsonAccountStore(tmp_path / "accounts.json")
    browser_client = MutableBrowserClient([
        {"acc": "browser-z", "name": "最先创建"},
        {"acc": "browser-a", "name": "后来创建"},
    ])
    backend = CoreAdminBackend(
        store=store,
        account_store=account_store,
        registry=TaskProgramRegistry.default(),
        runner=NoopRunner(),
        browser_client=browser_client,
        task_metadata=JsonSettingsStore(tmp_path / "task-meta.json", {"tasks": {}}),
        execution_slots=ExecutionSlotManager(2),
    )

    first = await backend.list_accounts()
    assert [account["id"] for account in first] == ["browser-z", "browser-a"]

    browser_client.accounts = [{"acc": "browser-a", "name": "后来创建"}]
    second = await backend.list_accounts()
    assert [account["id"] for account in second] == ["browser-a"]
    deleted = await account_store.get_account("browser-z")
    assert deleted is not None
    assert deleted.archived is True

    await store.close()


class FakeInProcessRegistry:
    def __init__(self):
        self.calls = []

    def status(self, accounts):
        return [{"acc": account.acc, "status": "stopped"} for account in accounts]

    async def ensure_started(self, account, config):
        self.calls.append(("open", account.acc, config))

    async def open(self, account, config):
        self.calls.append(("open", account.acc, config))
        return {"running": True, "activated": False, "windowActivated": True}

    async def close(self, account):
        self.calls.append(("close", account.acc, None))
        return {"closed": True}

    async def restart(self, account, config):
        self.calls.append(("restart", account.acc, config))


def test_default_data_dir_is_app_local_and_supports_override(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("X_OPS_DATA_DIR", raising=False)
    assert default_data_dir() == Path(__file__).resolve().parents[1] / "data"

    custom = tmp_path / "custom-data"
    monkeypatch.setenv("X_OPS_DATA_DIR", str(custom))
    assert default_data_dir() == custom.resolve()


async def test_in_process_browser_client_uses_one_registry(monkeypatch):
    account = SimpleNamespace(acc="browser-1")
    accounts = SimpleNamespace(accounts=[account], get=lambda account_id: account if account_id == account.acc else None)
    config_store = SimpleNamespace(accounts=accounts)
    registry = FakeInProcessRegistry()
    client = InProcessBrowserCustomClient(config_store, registry)
    monkeypatch.setattr(
        "browser_custom.api.list_accounts",
        lambda: {"accounts": [{"acc": "browser-1", "name": "测试浏览器"}]},
    )

    assert await client.list_accounts() == [{"acc": "browser-1", "name": "测试浏览器"}]
    assert await client.status() == {
        "accounts": [{"acc": "browser-1", "status": "stopped"}]
    }
    assert (await client.action("browser-1", "open"))["running"] is True
    assert (await client.batch(["browser-1", "browser-1"], "restart"))["ok"] is True
    assert [call[0] for call in registry.calls] == ["open", "restart"]


async def test_backend_start_resubmits_persisted_queued_runs(tmp_path):
    store = SQLiteStore(tmp_path / "runs.sqlite3")
    await store.initialize()
    await store.create_run(
        TaskRunSnapshot(
            id="queued-before-restart",
            program_name="browse_only",
            account_id="account-1",
            params={},
            status=RunStatus.QUEUED,
            created_at=datetime.now(UTC),
        )
    )
    runner = NoopRunner()
    backend = CoreAdminBackend(
        store=store,
        account_store=JsonAccountStore(tmp_path / "accounts.json"),
        registry=TaskProgramRegistry.default(),
        runner=runner,
        browser_client=FakeBrowserClient(),
        task_metadata=JsonSettingsStore(tmp_path / "task-meta.json", {"tasks": {}}),
        execution_slots=ExecutionSlotManager(2),
    )

    await backend.start()
    await asyncio.sleep(0)

    assert runner.executed == ["queued-before-restart"]
    await backend.close()


async def test_core_backend_creates_one_normal_run_per_account(tmp_path):
    store = SQLiteStore(tmp_path / "runs.sqlite3")
    backend = CoreAdminBackend(
        store=store,
        account_store=JsonAccountStore(tmp_path / "accounts.json"),
        registry=TaskProgramRegistry.default(),
        runner=NoopRunner(),
        browser_client=FakeBrowserClient(),
        task_metadata=JsonSettingsStore(tmp_path / "task-meta.json", {"tasks": {}}),
        execution_slots=ExecutionSlotManager(2),
    )
    await backend.start()
    task = await backend.create_task(
        {
            "name": "多账户浏览",
            "program_name": "browse_only",
            "account_ids": ["account-1", "account-2"],
            "params": {
                "feed": "for_you",
                "scroll_count": 1,
                "scroll_interval_seconds": 0.25,
            },
            "enabled": True,
            "browser_end_policy": "keep_open",
        }
    )
    triggered = await backend.trigger_task(task["id"], "manual")
    assert triggered is not None
    assert len(triggered["runs"]) == 2
    assert len({run["trigger_id"] for run in triggered["runs"]}) == 1
    assert {run["account_id"] for run in triggered["runs"]} == {
        "account-1",
        "account-2",
    }
    cancelled = await backend.cancel_run(triggered["runs"][0]["id"])
    assert cancelled["accepted"] is True
    assert cancelled["run"]["status"] == "cancelled"

    interaction = await backend.create_task(
        {
            "name": "集合参数可持久化",
            "program_name": "like_posts",
            "account_ids": ["account-1"],
            "params": {
                "target_authors": ["target"],
                "keywords": ["python"],
            },
        }
    )
    persisted = await store.get_task(interaction["id"])
    assert persisted.params["target_authors"] == ["target"]
    TaskProgramRegistry.default().require("like_posts").Params.model_validate(
        persisted.params
    )
    await backend.close()
