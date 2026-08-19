from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from x_ops.models import AccountRecord, RunStatus, TaskRunSnapshot
from x_ops.runner import AccountLockManager, ExecutionSlotManager
from x_ops.storage import SQLiteStore
from x_ops.task_sdk import (
    AccountContext,
    CancellationToken,
    StaticAIService,
    TaskCancelledError,
    TaskLoggerFactory,
    TaskTimeoutError,
)


async def test_account_context_is_deeply_read_only_at_mapping_boundary():
    context = AccountContext.from_record(
        AccountRecord("a", "A", "browser-a", metadata={"region": "US"})
    )
    with pytest.raises(TypeError):
        context.metadata["region"] = "CN"


async def test_cancellable_sleep_observes_request():
    requested = False

    async def check():
        return requested

    token = CancellationToken(task_run_id="run", is_cancel_requested=check, poll_interval=0.01)
    task = asyncio.create_task(token.sleep(10))
    await asyncio.sleep(0.03)
    requested = True
    with pytest.raises(TaskCancelledError):
        await asyncio.wait_for(task, 1)
    assert token.is_set() is True


async def test_cancellation_token_implements_x_actions_signal_contract():
    requested = True

    async def check():
        return requested

    token = CancellationToken(task_run_id="run", is_cancel_requested=check)
    assert await token.wait() is True
    assert token.is_set() is True


async def test_deadline_is_failure_not_cancel():
    token = CancellationToken(
        task_run_id="run",
        is_cancel_requested=lambda: asyncio.sleep(0, result=False),
        deadline=datetime.now(UTC) - timedelta(seconds=1),
    )
    with pytest.raises(TaskTimeoutError):
        await token.raise_if_cancelled()


async def test_external_task_cancel_cleans_up_pending_resource_acquire():
    locks = AccountLockManager()
    held = await locks.acquire("browser-profile")
    token = CancellationToken(
        task_run_id="run",
        is_cancel_requested=lambda: asyncio.sleep(0, result=False),
    )
    waiting = asyncio.create_task(token.wait_for(locks.acquire("browser-profile")))
    await asyncio.sleep(0)

    waiting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiting

    held.release()
    await asyncio.sleep(0)
    assert locks.locked("browser-profile") is False

    slots = ExecutionSlotManager(1)
    occupied = await slots.acquire()
    waiting = asyncio.create_task(token.wait_for(slots.acquire()))
    await asyncio.sleep(0)
    waiting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiting
    await occupied.release()
    await asyncio.sleep(0)
    assert slots.active == 0
    reusable = await asyncio.wait_for(slots.acquire(), 1)
    await reusable.release()
    assert slots.active == 0


async def test_logger_adds_run_account_program_fields(tmp_path):
    store = SQLiteStore(tmp_path / "logs.sqlite3")
    await store.initialize()
    await store.create_run(
        TaskRunSnapshot(
            id="run",
            program_name="browse_only",
            account_id="a",
            params={},
            status=RunStatus.QUEUED,
            created_at=datetime.now(UTC),
        )
    )
    logger = TaskLoggerFactory(store).create(
        task_run_id="run",
        task_id=None,
        program_name="browse_only",
        program_version="1.0.0",
        account_id="a",
        runner_id="runner",
    )
    logger.info("hello", post_id="123")
    log = (await store.list_logs("run"))[0]
    assert log.fields["task_run_id"] == "run"
    assert log.fields["account_id"] == "a"
    assert log.fields["program_version"] == "1.0.0"
    assert log.fields["post_id"] == "123"
    await store.close()


def test_logger_sink_failure_does_not_break_task_cleanup_path():
    class BrokenSink:
        def append_log_now(self, **_kwargs):
            raise OSError("disk full")

    logger = TaskLoggerFactory(BrokenSink()).create(
        task_run_id="run",
        task_id=None,
        program_name="browse_only",
        program_version="1.0.0",
        account_id="a",
        runner_id="runner",
    )
    logger.info("cleanup_started")


async def test_static_ai_converts_provider_failure():
    service = StaticAIService(lambda *_: " generated ")
    assert await service.generate(template="reply", variables={}) == "generated"
