"""Concurrency-safe account-to-session registry."""
from __future__ import annotations

import asyncio
from collections.abc import Callable

from ..config import Account, AccountsConfig
from .procutil import kill_for_data_dir, process_stats
from .session import AccountSession


class SessionRegistry:
    def __init__(self, session_factory: Callable[[Account, AccountsConfig], AccountSession] = AccountSession) -> None:
        self._session_factory = session_factory
        self._sessions: dict[str, AccountSession] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    def _lock(self, acc: str) -> asyncio.Lock:
        return self._locks.setdefault(acc, asyncio.Lock())

    def get(self, acc: str) -> AccountSession | None:
        session = self._sessions.get(acc)
        return session if session and session.is_alive() else None

    def is_running(self, acc: str) -> bool:
        return self.get(acc) is not None

    async def ensure_started(self, account: Account, config: AccountsConfig) -> AccountSession:
        async with self._lock(account.acc):
            current = self.get(account.acc)
            if current:
                return current
            session = self._session_factory(account, config)
            self._sessions[account.acc] = session
            try:
                await session.start()
            except BaseException:
                self._sessions.pop(account.acc, None)
                raise
            return session

    async def close(self, account: Account) -> dict:
        async with self._lock(account.acc):
            session = self._sessions.pop(account.acc, None)
            if session:
                result = await session.close()
                return {"closed": True, **result}
            loop = asyncio.get_running_loop()
            killed = await loop.run_in_executor(None, kill_for_data_dir, account.data_dir)
            return {"closed": bool(killed), "closeError": None, "killed": killed}

    async def restart(self, account: Account, config: AccountsConfig) -> AccountSession:
        await self.close(account)
        return await self.ensure_started(account, config)

    async def close_all(self, accounts: list[Account]) -> None:
        by_id = {account.acc: account for account in accounts}
        for acc in list(self._sessions):
            account = by_id.get(acc) or self._sessions[acc].account
            try:
                await self.close(account)
            except Exception:  # noqa: BLE001
                pass

    def status(self, accounts: list[Account]) -> list[dict]:
        result = []
        for account in accounts:
            controlled = self.is_running(account.acc)
            stats = process_stats(account.data_dir)
            has_browser_process = bool(stats["mainPids"])
            state = "running" if controlled and has_browser_process else (
                "orphaned" if has_browser_process else "stopped"
            )
            result.append({
                "acc": account.acc,
                "name": account.display_name,
                "enabled": account.enabled,
                "status": state,
                "browserRunning": state == "running",
                "userDataDir": account.userDataDir,
                "proxy": account.proxy_display or None,
                **stats,
            })
        return result


session_registry = SessionRegistry()
