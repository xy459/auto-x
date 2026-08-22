"""Concurrency-safe account-to-session registry."""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import Any

from ..config import Account, AccountsConfig, store
from .lease import BrowserPageLease
from .procutil import kill_for_data_dir, process_stats_many
from .session import AccountSession

logger = logging.getLogger(__name__)


class SessionRegistry:
    def __init__(
        self,
        session_factory: Callable[..., AccountSession] = AccountSession,
        on_x_username: Callable[[str, str], bool] | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._on_x_username = on_x_username
        self._sessions: dict[str, AccountSession] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    def _lock(self, acc: str) -> asyncio.Lock:
        return self._locks.setdefault(acc, asyncio.Lock())

    def get(self, acc: str) -> AccountSession | None:
        session = self._sessions.get(acc)
        return session if session and session.is_alive() else None

    def is_running(self, acc: str) -> bool:
        return self.get(acc) is not None

    async def _ensure_started_locked(self, account: Account, config: AccountsConfig) -> AccountSession:
        previous = self._sessions.get(account.acc)
        if previous:
            if previous.is_alive():
                return previous
            # A manually closed Chromium context still owns the Playwright
            # driver until its patched close() method runs. Reap it before
            # replacing the registry entry with a new session.
            self._sessions.pop(account.acc, None)
            await previous.close()
        session = (
            self._session_factory(account, config, self._on_x_username)
            if self._on_x_username
            else self._session_factory(account, config)
        )
        self._sessions[account.acc] = session
        try:
            await session.start()
        except BaseException:
            self._sessions.pop(account.acc, None)
            try:
                await asyncio.shield(session.close())
            except BaseException as cleanup_error:  # noqa: BLE001
                logger.warning(
                    "failed to clean up partially started browser session %s: %s",
                    account.acc,
                    cleanup_error,
                )
            raise
        return session

    async def ensure_started(self, account: Account, config: AccountsConfig) -> AccountSession:
        async with self._lock(account.acc):
            return await self._ensure_started_locked(account, config)

    async def open(self, account: Account, config: AccountsConfig) -> dict[str, bool]:
        """Start an account browser or raise its existing window."""
        async with self._lock(account.acc):
            existing = self._sessions.get(account.acc)
            already_running = bool(existing and existing.is_alive())
            session = await self._ensure_started_locked(account, config)
            window_activated = await session.bring_to_front()
            return {
                "running": True,
                "activated": already_running,
                "windowActivated": window_activated,
            }

    async def acquire_page(self, account: Account, config: AccountsConfig) -> BrowserPageLease:
        """Return an isolated task page inside the account persistent context."""
        async with self._lock(account.acc):
            browser_was_started = not bool(
                (existing := self._sessions.get(account.acc)) and existing.is_alive()
            )
            session = await self._ensure_started_locked(account, config)
            try:
                acquire_task_page = getattr(session, "acquire_task_page", None)
                if callable(acquire_task_page):
                    page, reusable_page = await acquire_task_page()
                else:
                    # Preserve compatibility with custom Session factories that
                    # implement the original ``new_page`` interface only.
                    page = await session.new_page()
                    reusable_page = False
            except BaseException:
                if browser_was_started:
                    self._sessions.pop(account.acc, None)
                    await asyncio.shield(session.close())
                raise
            return BrowserPageLease(
                registry=self,
                account=account,
                session=session,
                page=page,
                reusable_page=reusable_page,
                browser_was_started=browser_was_started,
            )

    async def close(self, account: Account) -> dict[str, Any]:
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
            session = self._sessions.get(acc)
            if session is None:
                continue
            account = by_id.get(acc) or session.account
            try:
                await self.close(account)
            except Exception as exc:  # noqa: BLE001
                logger.warning("failed to close browser session %s during close_all: %s", acc, exc)

    def status(self, accounts: list[Account]) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        stats_by_account = process_stats_many([account.data_dir for account in accounts])
        for account, stats in zip(accounts, stats_by_account, strict=True):
            session = self._sessions.get(account.acc)
            controlled = bool(session and session.is_alive())
            has_browser_process = bool(stats["mainPids"])
            state = "running" if controlled and has_browser_process else (
                "orphaned" if has_browser_process else "stopped"
            )
            result.append({
                "acc": account.acc,
                "name": account.display_name,
                "status": state,
                "browserRunning": state == "running",
                "userDataDir": account.userDataDir,
                "proxy": account.proxy_display or None,
                "fingerprint": session.fingerprint_info() if session else None,
                **stats,
            })
        return result


session_registry = SessionRegistry(on_x_username=store.update_x_username)
