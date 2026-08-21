import asyncio
from pathlib import Path

import pytest

from browser_custom.browser import registry as registry_module
from browser_custom.browser.registry import SessionRegistry
from browser_custom.config import Account, AccountsConfig


class FakeSession:
    starts = 0
    closes = 0
    activations = 0

    def __init__(self, account, config):
        self.account = account
        self.config = config
        self.alive = False
        self.closed = False
        self.context = object()
        self.pages = []

    async def start(self):
        type(self).starts += 1
        self.alive = True

    async def close(self):
        type(self).closes += 1
        self.alive = False
        self.closed = True
        return {"closeError": None, "killed": []}

    def is_alive(self):
        return self.alive

    def fingerprint_info(self):
        return {"source": "runtime", "webrtcIp": "203.0.113.8"} if self.alive else None

    async def new_page(self):
        page = FakePage()
        self.pages.append(page)
        return page

    async def bring_to_front(self):
        type(self).activations += 1
        return True


class FakePage:
    def __init__(self):
        self.closed = False
        self.handlers = {}

    def on(self, event, handler):
        self.handlers[event] = handler

    def is_closed(self):
        return self.closed

    async def close(self):
        self.closed = True

    async def bring_to_front(self):
        pass

    def emit_popup(self, page):
        self.handlers["popup"](page)


@pytest.fixture(autouse=True)
def reset_fake():
    FakeSession.starts = 0
    FakeSession.closes = 0
    FakeSession.activations = 0


@pytest.mark.asyncio
async def test_ensure_started_is_idempotent(tmp_path: Path):
    account = Account(acc="a", userDataDir=str(tmp_path / "a"))
    config = AccountsConfig(accounts=[account])
    registry = SessionRegistry(FakeSession)
    first = await registry.ensure_started(account, config)
    second = await registry.ensure_started(account, config)
    assert first is second
    assert FakeSession.starts == 1


@pytest.mark.asyncio
async def test_open_starts_then_raises_existing_account_browser(tmp_path: Path):
    account = Account(acc="a", userDataDir=str(tmp_path / "a"))
    config = AccountsConfig(accounts=[account])
    registry = SessionRegistry(FakeSession)

    first = await registry.open(account, config)
    second = await registry.open(account, config)

    assert first == {"running": True, "activated": False, "windowActivated": True}
    assert second == {"running": True, "activated": True, "windowActivated": True}
    assert FakeSession.starts == 1
    assert FakeSession.activations == 2


@pytest.mark.asyncio
async def test_restart_closes_then_starts(tmp_path: Path):
    account = Account(acc="a", userDataDir=str(tmp_path / "a"))
    config = AccountsConfig(accounts=[account])
    registry = SessionRegistry(FakeSession)
    await registry.ensure_started(account, config)
    await registry.restart(account, config)
    assert FakeSession.closes == 1
    assert FakeSession.starts == 2


@pytest.mark.asyncio
async def test_reopen_after_manual_exit_reaps_previous_session(tmp_path: Path):
    account = Account(acc="a", userDataDir=str(tmp_path / "a"))
    config = AccountsConfig(accounts=[account])
    registry = SessionRegistry(FakeSession)
    previous = await registry.ensure_started(account, config)
    previous.alive = False

    current = await registry.ensure_started(account, config)

    assert current is not previous
    assert previous.closed is True
    assert FakeSession.closes == 1
    assert FakeSession.starts == 2


@pytest.mark.asyncio
async def test_acquire_page_starts_browser_and_releases_owned_page(tmp_path: Path):
    account = Account(acc="a", userDataDir=str(tmp_path / "a"))
    config = AccountsConfig(accounts=[account])
    registry = SessionRegistry(FakeSession)

    lease = await registry.acquire_page(account, config)

    assert lease.browser_was_started is True
    assert lease.session.is_alive() is True
    assert lease.page.closed is False

    result = await lease.release()

    assert lease.page.closed is True
    assert lease.session.is_alive() is True
    assert result == {"released": True, "pageErrors": [], "browser": None}


@pytest.mark.asyncio
async def test_acquire_page_tracks_popups_but_not_unrelated_pages(tmp_path: Path):
    account = Account(acc="a", userDataDir=str(tmp_path / "a"))
    config = AccountsConfig(accounts=[account])
    registry = SessionRegistry(FakeSession)
    existing = await registry.ensure_started(account, config)
    unrelated = FakePage()

    lease = await registry.acquire_page(account, config)
    popup = FakePage()
    lease.page.emit_popup(popup)
    result = await lease.release()

    assert lease.browser_was_started is False
    assert lease.page.closed is True
    assert popup.closed is True
    assert unrelated.closed is False
    assert existing.is_alive() is True
    assert result["pageErrors"] == []


@pytest.mark.asyncio
async def test_release_can_close_whole_account_browser_and_is_idempotent(tmp_path: Path):
    account = Account(acc="a", userDataDir=str(tmp_path / "a"))
    config = AccountsConfig(accounts=[account])
    registry = SessionRegistry(FakeSession)
    lease = await registry.acquire_page(account, config)

    first = await lease.release(close_browser=True)
    second = await lease.release(close_browser=True)

    assert first == second
    assert first["browser"]["closed"] is True
    assert registry.is_running(account.acc) is False
    assert FakeSession.closes == 1


@pytest.mark.asyncio
async def test_concurrent_release_waits_for_same_complete_result(tmp_path: Path):
    account = Account(acc="a", userDataDir=str(tmp_path / "a"))
    config = AccountsConfig(accounts=[account])
    registry = SessionRegistry(FakeSession)
    lease = await registry.acquire_page(account, config)
    close_started = asyncio.Event()
    allow_close = asyncio.Event()

    async def slow_close():
        close_started.set()
        await allow_close.wait()
        lease.page.closed = True

    lease.page.close = slow_close
    first = asyncio.create_task(lease.release())
    await close_started.wait()
    second = asyncio.create_task(lease.release())
    await asyncio.sleep(0)
    assert second.done() is False

    allow_close.set()
    first_result, second_result = await asyncio.gather(first, second)
    assert first_result == second_result == {"released": True, "pageErrors": [], "browser": None}


@pytest.mark.asyncio
async def test_cancelled_release_can_be_retried_to_complete_cleanup(tmp_path: Path):
    account = Account(acc="a", userDataDir=str(tmp_path / "a"))
    config = AccountsConfig(accounts=[account])
    registry = SessionRegistry(FakeSession)
    lease = await registry.acquire_page(account, config)
    close_started = asyncio.Event()
    allow_close = asyncio.Event()

    async def cancellable_close():
        close_started.set()
        await allow_close.wait()
        lease.page.closed = True

    lease.page.close = cancellable_close
    first = asyncio.create_task(lease.release())
    await close_started.wait()
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first

    allow_close.set()
    second = await lease.release()
    assert second == {"released": True, "pageErrors": [], "browser": None}
    assert lease.page.closed is True


@pytest.mark.asyncio
async def test_release_closes_popup_opened_while_owner_page_is_closing(tmp_path: Path):
    account = Account(acc="a", userDataDir=str(tmp_path / "a"))
    config = AccountsConfig(accounts=[account])
    registry = SessionRegistry(FakeSession)
    lease = await registry.acquire_page(account, config)
    popup = FakePage()

    async def close_with_popup():
        lease.page.emit_popup(popup)
        lease.page.closed = True

    lease.page.close = close_with_popup
    result = await lease.release()

    assert result["pageErrors"] == []
    assert popup.closed is True


@pytest.mark.asyncio
async def test_start_failure_closes_partially_created_session(tmp_path: Path):
    class FailingSession(FakeSession):
        async def start(self):
            self.alive = True
            raise RuntimeError("launch failed after context creation")

    account = Account(acc="a", userDataDir=str(tmp_path / "a"))
    config = AccountsConfig(accounts=[account])
    registry = SessionRegistry(FailingSession)

    with pytest.raises(RuntimeError, match="launch failed"):
        await registry.ensure_started(account, config)

    assert registry.get(account.acc) is None
    assert FailingSession.closes == 1


@pytest.mark.asyncio
async def test_close_all_tolerates_session_closed_during_iteration(tmp_path: Path):
    accounts = [
        Account(acc="a", userDataDir=str(tmp_path / "a")),
        Account(acc="b", userDataDir=str(tmp_path / "b")),
    ]
    config = AccountsConfig(accounts=accounts)
    registry = SessionRegistry(FakeSession)
    for account in accounts:
        await registry.ensure_started(account, config)

    original_close = registry.close

    async def close_with_concurrent_removal(account):
        if account.acc == "a":
            registry._sessions.pop("b", None)
        return await original_close(account)

    registry.close = close_with_concurrent_removal
    await registry.close_all(accounts)

    assert registry.is_running("a") is False
    assert registry.is_running("b") is False


@pytest.mark.asyncio
async def test_status_uses_process_state_after_manual_exit(tmp_path: Path, monkeypatch):
    account = Account(acc="a", userDataDir=str(tmp_path / "a"))
    config = AccountsConfig(accounts=[account])
    registry = SessionRegistry(FakeSession)
    await registry.ensure_started(account, config)
    monkeypatch.setattr(registry_module, "process_stats_many", lambda data_dirs: [
        {"mainPids": [], "processCount": 0} for _path in data_dirs
    ])

    status = registry.status([account])[0]

    assert status["status"] == "stopped"
    assert status["browserRunning"] is False


def test_status_uses_one_process_snapshot_for_all_accounts(tmp_path: Path, monkeypatch):
    accounts = [
        Account(acc="a", userDataDir=str(tmp_path / "a")),
        Account(acc="b", userDataDir=str(tmp_path / "b")),
    ]
    calls = []

    def fake_stats(data_dirs):
        calls.append(data_dirs)
        return [
            {"mainPids": [100], "processCount": 3},
            {"mainPids": [], "processCount": 0},
        ]

    monkeypatch.setattr(registry_module, "process_stats_many", fake_stats)
    statuses = SessionRegistry(FakeSession).status(accounts)

    assert len(calls) == 1
    assert calls[0] == [accounts[0].data_dir, accounts[1].data_dir]
    assert statuses[0]["status"] == "orphaned"
    assert statuses[0]["processCount"] == 3
    assert statuses[1]["status"] == "stopped"
