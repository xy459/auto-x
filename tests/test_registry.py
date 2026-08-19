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

    async def start(self):
        type(self).starts += 1
        self.alive = True

    async def close(self):
        type(self).closes += 1
        self.alive = False
        return {"closeError": None, "killed": []}

    def is_alive(self):
        return self.alive


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
async def test_status_uses_process_state_after_manual_exit(tmp_path: Path, monkeypatch):
    account = Account(acc="a", userDataDir=str(tmp_path / "a"))
    config = AccountsConfig(accounts=[account])
    registry = SessionRegistry(FakeSession)
    await registry.ensure_started(account, config)
    monkeypatch.setattr(registry_module, "process_stats", lambda _data_dir: {
        "mainPids": [], "processCount": 0, "rssBytes": 0,
    })

    status = registry.status([account])[0]

    assert status["status"] == "stopped"
    assert status["browserRunning"] is False
