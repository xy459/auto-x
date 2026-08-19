from pathlib import Path

import pytest

from browser_custom.browser import registry as registry_module
from browser_custom.browser.registry import SessionRegistry
from browser_custom.config import Account, AccountsConfig


class FakeSession:
    starts = 0
    closes = 0

    def __init__(self, account, config):
        self.account = account
        self.config = config
        self.alive = False
        self.closed = False

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


@pytest.fixture(autouse=True)
def reset_fake():
    FakeSession.starts = 0
    FakeSession.closes = 0


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
