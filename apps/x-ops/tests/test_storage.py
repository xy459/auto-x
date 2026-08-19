from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest

from x_ops.models import AccountRecord, RunOutcome, RunStatus, Task, TaskRunSnapshot
from x_ops.storage import InMemoryAccountStore, JsonAccountStore, SQLiteStore


@pytest.fixture
async def store(tmp_path):
    value = SQLiteStore(tmp_path / "x-ops.sqlite3")
    await value.initialize()
    yield value
    await value.close()


async def test_only_minimal_tables_are_created(store):
    rows = store._connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
    ).fetchall()
    assert [row[0] for row in rows] == ["scheduled_fires", "task_logs", "task_runs", "tasks"]


async def test_task_trigger_creates_one_run_per_account_with_shared_trigger(store):
    task = Task(
        id="task-1",
        name="multi",
        program_name="browse_only",
        account_ids=("a-1", "a-2", "a-2", "a-3"),
        params={"scroll_count": 2},
    )
    await store.create_task(task)

    runs = await store.trigger_task("task-1", trigger_id="trigger-1")

    assert [run.account_id for run in runs] == ["a-1", "a-2", "a-3"]
    assert {run.trigger_id for run in runs} == {"trigger-1"}
    assert all(run.status is RunStatus.QUEUED for run in runs)
    assert len({run.id for run in runs}) == 3
    assert len(await store.list_runs(task_id="task-1")) == 3


async def test_task_trigger_reads_task_inside_its_write_transaction(store):
    await store.create_task(
        Task(
            id="transactional-task",
            name="transactional",
            program_name="browse_only",
            account_ids=("a-1",),
            params={},
        )
    )

    async def stale_preflight_lookup(_task_id):
        raise AssertionError("trigger_task must not preflight outside its transaction")

    store.get_task = stale_preflight_lookup
    runs = await store.trigger_task("transactional-task")

    assert len(runs) == 1
    assert runs[0].task_id == "transactional-task"


async def test_scheduled_fire_reservation_and_runs_are_atomic(store, monkeypatch):
    task = Task(
        id="scheduled-task",
        name="scheduled",
        program_name="browse_only",
        account_ids=("a-1", "a-2"),
        params={"scroll_count": 1},
    )
    await store.create_task(task)

    original_insert = store._insert_run
    attempts = 0

    def fail_first_insert(run):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("simulated crash before TaskRun commit")
        original_insert(run)

    monkeypatch.setattr(store, "_insert_run", fail_first_insert)
    with pytest.raises(RuntimeError, match="simulated crash"):
        await store.trigger_task("scheduled-task", fire_key="cron:2026-08-20T09:30")

    monkeypatch.setattr(store, "_insert_run", original_insert)
    runs = await store.trigger_task(
        "scheduled-task",
        trigger_id="schedule:cron:2026-08-20T09:30",
        fire_key="cron:2026-08-20T09:30",
    )
    duplicate = await store.trigger_task(
        "scheduled-task",
        trigger_id="schedule:cron:2026-08-20T09:30",
        fire_key="cron:2026-08-20T09:30",
    )

    assert len(runs) == 2
    assert duplicate == []
    assert len(await store.list_runs(task_id="scheduled-task")) == 2


async def test_scheduled_fire_is_unique_across_store_instances(tmp_path):
    path = tmp_path / "scheduled.sqlite3"
    first = SQLiteStore(path)
    second = SQLiteStore(path)
    await first.initialize()
    await second.initialize()
    await first.create_task(
        Task(
            id="scheduled-task",
            name="scheduled",
            program_name="browse_only",
            account_ids=("a-1", "a-2"),
            params={"scroll_count": 1},
        )
    )

    results = await asyncio.gather(
        first.trigger_task("scheduled-task", fire_key="cron:2026-08-20T09:30"),
        second.trigger_task("scheduled-task", fire_key="cron:2026-08-20T09:30"),
    )

    assert sorted(len(runs) for runs in results) == [0, 2]
    assert len(await first.list_runs(task_id="scheduled-task")) == 2
    await first.close()
    await second.close()


async def test_atomic_claim_allows_only_one_runner(tmp_path):
    path = tmp_path / "claims.sqlite3"
    first = SQLiteStore(path)
    second = SQLiteStore(path)
    await first.initialize()
    await second.initialize()
    run = TaskRunSnapshot(
        id="run-1",
        program_name="browse_only",
        account_id="account-1",
        params={},
        status=RunStatus.QUEUED,
        created_at=datetime.now(UTC),
    )
    await first.create_run(run)

    results = await asyncio.gather(first.claim("run-1", "runner-a"), second.claim("run-1", "runner-b"))

    assert sorted(results) == [False, True]
    claimed = await first.get_run("run-1")
    assert claimed.claimed_by in {"runner-a", "runner-b"}
    await first.close()
    await second.close()


async def test_cancel_unclaimed_queue_finishes_immediately_but_claimed_queue_sets_flag(store):
    for run_id in ("unclaimed", "claimed"):
        await store.create_run(
            TaskRunSnapshot(
                id=run_id,
                program_name="browse_only",
                account_id="a",
                params={},
                status=RunStatus.QUEUED,
                created_at=datetime.now(UTC),
            )
        )

    assert await store.request_cancel("unclaimed")
    assert (await store.get_run("unclaimed")).status is RunStatus.CANCELLED

    assert await store.claim("claimed", "runner")
    assert await store.request_cancel("claimed")
    claimed = await store.get_run("claimed")
    assert claimed.status is RunStatus.QUEUED
    assert claimed.cancel_requested_at is not None
    assert await store.finish("claimed", "runner", RunOutcome.cancelled(None))
    assert (await store.get_run("claimed")).status is RunStatus.CANCELLED


async def test_recovery_marks_running_uncertain_releases_claims_and_finishes_cancelled(store):
    for run_id in ("running", "queued", "cancelled"):
        await store.create_run(
            TaskRunSnapshot(
                id=run_id,
                program_name="browse_only",
                account_id="a",
                params={},
                status=RunStatus.QUEUED,
                created_at=datetime.now(UTC),
            )
        )
        assert await store.claim(run_id, "old-runner")

    assert await store.mark_running("running", "old-runner", "1.0.0")
    assert await store.request_cancel("cancelled")

    assert await store.recover_interrupted() == 1

    running = await store.get_run("running")
    assert running.status is RunStatus.UNCERTAIN
    assert running.error["code"] == "RUNNER_INTERRUPTED"
    assert running.error["retryable"] is False
    queued = await store.get_run("queued")
    assert queued.status is RunStatus.QUEUED
    assert queued.claimed_by is None
    cancelled = await store.get_run("cancelled")
    assert cancelled.status is RunStatus.CANCELLED


async def test_rerun_is_new_queued_snapshot(store):
    original = TaskRunSnapshot(
        id="old",
        program_name="browse_only",
        account_id="a",
        params={"scroll_count": 7},
        status=RunStatus.QUEUED,
        created_at=datetime.now(UTC),
    )
    await store.create_run(original)
    rerun = await store.create_rerun("old", new_run_id="new")
    assert rerun.id == "new"
    assert rerun.rerun_of == "old"
    assert rerun.params == original.params
    assert rerun.status is RunStatus.QUEUED


async def test_in_memory_accounts_are_mutable_business_metadata():
    accounts = InMemoryAccountStore()
    created = await accounts.create_account(
        AccountRecord("a", "A", "browser-a", username="alice", tags=("normal",))
    )
    assert await accounts.get_account("a") == created
    updated = await accounts.update_account("a", enabled=False, metadata={"note": "paused"})
    assert updated.enabled is False
    assert (await accounts.list_accounts())[0].metadata == {"note": "paused"}


@pytest.mark.parametrize("store_kind", ["memory", "json"])
async def test_active_accounts_cannot_share_browser_profile(store_kind, tmp_path):
    accounts = (
        InMemoryAccountStore()
        if store_kind == "memory"
        else JsonAccountStore(tmp_path / "accounts.json")
    )
    await accounts.create_account(AccountRecord("a", "A", "browser-a"))

    with pytest.raises(ValueError, match="browser_account_id already belongs"):
        await accounts.create_account(AccountRecord("b", "B", "browser-a"))

    await accounts.create_account(AccountRecord("b", "B", "browser-b", enabled=False))
    with pytest.raises(ValueError, match="browser_account_id already belongs"):
        await accounts.update_account("b", enabled=True, browser_account_id="browser-a")


async def test_json_account_store_persists_business_metadata_atomically(tmp_path):
    path = tmp_path / "accounts.json"
    store = JsonAccountStore(path)
    await store.create_account(
        AccountRecord(
            "a",
            "Alice",
            "browser-a",
            username="alice",
            tags=("normal",),
            metadata={"note": "business only"},
        )
    )
    await store.update_account("a", enabled=False, tags=("paused",))

    reloaded = JsonAccountStore(path)
    account = await reloaded.get_account("a")
    assert account.browser_account_id == "browser-a"
    assert account.enabled is False
    assert account.tags == ("paused",)
    assert not list(tmp_path.glob(".accounts-*.tmp"))
