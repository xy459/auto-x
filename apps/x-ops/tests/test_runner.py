from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from pydantic import BaseModel

from x_ops.integrations import InMemoryBrowserGateway
from x_ops.models import AccountRecord, BrowserEndPolicy, CleanupReport, RunStatus, TaskRunSnapshot
from x_ops.runner import AccountLockManager, ExecutionSlotManager, TaskRunner
from x_ops.storage import InMemoryAccountStore, SQLiteStore
from x_ops.task_programs import ProgramSpec, TaskProgram, TaskProgramRegistry
from x_ops.task_sdk import DisabledAIService, TaskCancelledError, TaskLoggerFactory, TaskUncertainError


class Params(BaseModel):
    value: int = 1


class ActionsFactory:
    def bind(self, page):
        return SimpleNamespace(page=page)


async def create_run(store, run_id, account_id="account-1", **kwargs):
    run = TaskRunSnapshot(
        id=run_id,
        program_name="test_program",
        account_id=account_id,
        params=kwargs.pop("params", {"value": 2}),
        status=RunStatus.QUEUED,
        created_at=datetime.now(UTC),
        **kwargs,
    )
    await store.create_run(run)
    return run


def make_runner(
    store,
    program_run,
    *,
    accounts=None,
    browser=None,
    locks=None,
    slots=None,
    browser_timeout=120.0,
):
    registry = TaskProgramRegistry()
    registry.register(
        TaskProgram(
            ProgramSpec("test_program", "1.2.3", "Test", "test program"),
            Params,
            program_run,
        )
    )
    return TaskRunner(
        runner_id="runner-1",
        run_store=store,
        account_store=InMemoryAccountStore(
            accounts
            or [AccountRecord("account-1", "Account", "browser-1")]
        ),
        program_registry=registry,
        account_locks=locks or AccountLockManager(),
        execution_slots=slots or ExecutionSlotManager(2),
        browser_gateway=browser or InMemoryBrowserGateway(),
        actions_factory=ActionsFactory(),
        ai_service=DisabledAIService(),
        logger_factory=TaskLoggerFactory(store),
        browser_acquire_timeout_seconds=browser_timeout,
    )


@pytest.fixture
async def store(tmp_path):
    value = SQLiteStore(tmp_path / "runner.sqlite3")
    await value.initialize()
    yield value
    await value.close()


async def test_success_maps_output_version_and_releases_browser(store):
    browser = InMemoryBrowserGateway()

    async def program(context, params):
        assert context.account.account_id == "account-1"
        assert context.actions.page is not None
        return {"value": params.value}

    await create_run(store, "run", browser_end_policy=BrowserEndPolicy.CLOSE)
    runner = make_runner(store, program, browser=browser)
    await runner.execute("run")

    result = await store.get_run("run")
    assert result.status is RunStatus.SUCCEEDED
    assert result.program_version == "1.2.3"
    assert result.output == {"value": 2}
    assert browser.active_leases == 0
    assert "browser-1" not in browser.running_accounts


@pytest.mark.parametrize(
    ("exception", "status", "code"),
    [
        (TaskCancelledError("run"), RunStatus.CANCELLED, "TASK_CANCELLED"),
        (TaskUncertainError(action_id="interaction.like"), RunStatus.UNCERTAIN, "ACTION_STATE_UNKNOWN"),
        (RuntimeError("boom"), RunStatus.FAILED, "UNHANDLED_TASK_ERROR"),
    ],
)
async def test_top_level_outcome_mapping_always_releases(store, exception, status, code):
    browser = InMemoryBrowserGateway()

    async def program(_context, _params):
        raise exception

    run_id = f"run-{status.value}"
    await create_run(store, run_id)
    runner = make_runner(store, program, browser=browser)
    await runner.execute(run_id)
    result = await store.get_run(run_id)
    assert result.status is status
    assert result.error["code"] == code
    assert browser.active_leases == 0


async def test_error_persistence_redacts_credentials_and_sensitive_details(store):
    class SensitiveError(RuntimeError):
        details = {
            "api_key": "sk-live-secret",
            "nested": {"authorization": "Bearer abc123"},
        }

    async def program(_context, _params):
        raise SensitiveError(
            "proxy http://alice:proxy-secret@example.test authorization=Bearer-raw"
        )

    await create_run(store, "sensitive-error")
    await make_runner(store, program).execute("sensitive-error")
    error = (await store.get_run("sensitive-error")).error
    serialized = str(error)
    assert "proxy-secret" not in serialized
    assert "sk-live-secret" not in serialized
    assert "Bearer-raw" not in serialized
    assert error["details"]["api_key"] == "***"


async def test_invalid_params_and_browser_failure_never_call_program(store):
    called = False

    async def program(_context, _params):
        nonlocal called
        called = True
        return {}

    await create_run(store, "invalid", params={"value": "wrong"})
    browser = InMemoryBrowserGateway()
    runner = make_runner(store, program, browser=browser)
    await runner.execute("invalid")
    assert (await store.get_run("invalid")).error["code"] == "INVALID_TASK_PARAMS"
    assert browser.acquire_count == 0

    await create_run(store, "browser-error")
    browser.fail_acquire = RuntimeError("cannot launch")
    await runner.execute("browser-error")
    assert (await store.get_run("browser-error")).error["code"] == "BROWSER_START_FAILED"
    assert called is False


async def test_same_account_is_mutually_exclusive(store):
    active = maximum = 0
    first_started = asyncio.Event()
    release = asyncio.Event()

    async def program(_context, _params):
        nonlocal active, maximum
        active += 1
        maximum = max(maximum, active)
        first_started.set()
        await release.wait()
        active -= 1
        return {}

    await create_run(store, "one")
    await create_run(store, "two")
    browser = InMemoryBrowserGateway()
    runner = make_runner(store, program, browser=browser)
    tasks = [asyncio.create_task(runner.execute(run_id)) for run_id in ("one", "two")]
    await asyncio.wait_for(first_started.wait(), 1)
    await asyncio.sleep(0.05)
    assert maximum == 1
    assert browser.active_leases == 1
    release.set()
    await asyncio.gather(*tasks)
    assert maximum == 1


async def test_legacy_accounts_sharing_browser_profile_are_mutually_exclusive(store):
    active = maximum = 0
    first_started = asyncio.Event()
    release = asyncio.Event()

    async def program(_context, _params):
        nonlocal active, maximum
        active += 1
        maximum = max(maximum, active)
        first_started.set()
        await release.wait()
        active -= 1
        return {}

    await create_run(store, "one", "a-1")
    await create_run(store, "two", "a-2")
    accounts = [
        AccountRecord("a-1", "A1", "shared-browser"),
        AccountRecord("a-2", "A2", "shared-browser"),
    ]
    runner = make_runner(store, program, accounts=accounts)
    tasks = [asyncio.create_task(runner.execute(run_id)) for run_id in ("one", "two")]
    await asyncio.wait_for(first_started.wait(), 1)
    await asyncio.sleep(0.05)
    assert maximum == 1

    release.set()
    await asyncio.gather(*tasks)
    assert maximum == 1


async def test_different_accounts_run_in_parallel_but_slots_limit_capacity(store):
    release = asyncio.Event()
    both_started = asyncio.Event()
    active = 0

    async def program(_context, _params):
        nonlocal active
        active += 1
        if active == 2:
            both_started.set()
        await release.wait()
        active -= 1
        return {}

    await create_run(store, "one", "a-1")
    await create_run(store, "two", "a-2")
    accounts = [
        AccountRecord("a-1", "A1", "b-1"),
        AccountRecord("a-2", "A2", "b-2"),
    ]
    slots = ExecutionSlotManager(2)
    runner = make_runner(store, program, accounts=accounts, slots=slots)
    tasks = [asyncio.create_task(runner.execute(run_id)) for run_id in ("one", "two")]
    await asyncio.wait_for(both_started.wait(), 1)
    assert active == 2
    assert slots.max_active == 2
    release.set()
    await asyncio.gather(*tasks)


async def test_execution_slot_limit_can_be_updated_without_restart():
    slots = ExecutionSlotManager(1)
    first = await slots.acquire()
    waiting = asyncio.create_task(slots.acquire())
    await asyncio.sleep(0)
    assert not waiting.done()

    await slots.set_limit(2)
    second = await asyncio.wait_for(waiting, 1)
    assert slots.active == 2

    await second.release()
    await first.release()
    assert slots.active == 0


async def test_cancel_while_waiting_for_account_lock_does_not_acquire_browser(store):
    release = asyncio.Event()
    started = asyncio.Event()

    async def program(_context, _params):
        started.set()
        await release.wait()
        return {}

    await create_run(store, "first")
    await create_run(store, "waiting")
    browser = InMemoryBrowserGateway()
    runner = make_runner(store, program, browser=browser)
    first = asyncio.create_task(runner.execute("first"))
    await asyncio.wait_for(started.wait(), 1)
    second = asyncio.create_task(runner.execute("waiting"))
    for _ in range(50):
        if (await store.get_run("waiting")).claimed_by:
            break
        await asyncio.sleep(0.01)
    await store.request_cancel("waiting")
    await asyncio.wait_for(second, 1)
    assert (await store.get_run("waiting")).status is RunStatus.CANCELLED
    assert browser.acquire_count == 1
    release.set()
    await first


async def test_browser_acquire_has_hard_timeout_and_releases_resources(store):
    class HangingBrowser:
        async def acquire(self, **_kwargs):
            await asyncio.Event().wait()

    async def program(_context, _params):
        raise AssertionError("program must not start")

    locks = AccountLockManager()
    slots = ExecutionSlotManager(1)
    await create_run(store, "browser-timeout")
    runner = make_runner(
        store,
        program,
        browser=HangingBrowser(),
        locks=locks,
        slots=slots,
        browser_timeout=0.02,
    )

    await runner.execute("browser-timeout")

    result = await store.get_run("browser-timeout")
    assert result.status is RunStatus.FAILED
    assert result.error["code"] == "BROWSER_START_TIMEOUT"
    assert locks.locked("browser-1") is False
    assert slots.active == 0


async def test_cancel_during_browser_acquire_releases_resources(store):
    class HangingBrowser:
        def __init__(self):
            self.started = asyncio.Event()

        async def acquire(self, **_kwargs):
            self.started.set()
            await asyncio.Event().wait()

    async def program(_context, _params):
        raise AssertionError("program must not start")

    browser = HangingBrowser()
    locks = AccountLockManager()
    slots = ExecutionSlotManager(1)
    await create_run(store, "browser-cancel")
    runner = make_runner(store, program, browser=browser, locks=locks, slots=slots)
    running = asyncio.create_task(runner.execute("browser-cancel"))
    await asyncio.wait_for(browser.started.wait(), 1)

    await store.request_cancel("browser-cancel")
    await asyncio.wait_for(running, 1)

    result = await store.get_run("browser-cancel")
    assert result.status is RunStatus.CANCELLED
    assert locks.locked("browser-1") is False
    assert slots.active == 0


async def test_external_task_cancel_releases_browser_lock_and_slot(store):
    started = asyncio.Event()

    async def program(_context, _params):
        started.set()
        await asyncio.Event().wait()

    browser = InMemoryBrowserGateway()
    locks = AccountLockManager()
    slots = ExecutionSlotManager(1)
    await create_run(store, "external-cancel")
    runner = make_runner(
        store,
        program,
        browser=browser,
        locks=locks,
        slots=slots,
    )
    running = asyncio.create_task(runner.execute("external-cancel"))
    await asyncio.wait_for(started.wait(), 1)

    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await running

    assert browser.active_leases == 0
    assert locks.locked("browser-1") is False
    assert slots.active == 0
    result = await store.get_run("external-cancel")
    assert result.status is RunStatus.UNCERTAIN
    assert result.error["code"] == "RUNNER_INTERRUPTED"

    # Prove both synchronization primitives remain usable, not merely that
    # their observable counters happen to be zero.
    lock = await asyncio.wait_for(locks.acquire("browser-1"), 1)
    slot = await asyncio.wait_for(slots.acquire(), 1)
    lock.release()
    await slot.release()


async def test_external_task_cancel_before_program_start_is_cancelled(store):
    release = asyncio.Event()
    first_started = asyncio.Event()

    async def program(_context, _params):
        first_started.set()
        await release.wait()
        return {}

    await create_run(store, "holder")
    await create_run(store, "hard-cancel-waiting")
    runner = make_runner(store, program)
    holder = asyncio.create_task(runner.execute("holder"))
    await asyncio.wait_for(first_started.wait(), 1)
    waiting = asyncio.create_task(runner.execute("hard-cancel-waiting"))
    for _ in range(50):
        if (await store.get_run("hard-cancel-waiting")).claimed_by:
            break
        await asyncio.sleep(0.01)

    waiting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiting

    result = await store.get_run("hard-cancel-waiting")
    assert result.status is RunStatus.CANCELLED
    assert result.error["code"] == "TASK_CANCELLED"
    release.set()
    await holder


async def test_cleanup_warning_does_not_overwrite_success(store):
    class Lease:
        page = object()
        browser_was_started = True

        async def release(self, *, close_browser):
            return CleanupReport(({"code": "BROWSER_CLOSE_FAILED", "message": "warning"},))

    class Browser:
        async def acquire(self, **_kwargs):
            return Lease()

    async def program(_context, _params):
        return {"done": True}

    await create_run(store, "cleanup")
    runner = make_runner(store, program, browser=Browser())
    await runner.execute("cleanup")
    result = await store.get_run("cleanup")
    assert result.status is RunStatus.SUCCEEDED
    assert result.cleanup_warnings[0]["code"] == "BROWSER_CLOSE_FAILED"


async def test_duplicate_message_does_not_execute_twice(store):
    calls = 0

    async def program(_context, _params):
        nonlocal calls
        calls += 1
        return {}

    await create_run(store, "run")
    runner = make_runner(store, program)
    await asyncio.gather(runner.execute("run"), runner.execute("run"))
    await runner.execute("run")
    assert calls == 1
