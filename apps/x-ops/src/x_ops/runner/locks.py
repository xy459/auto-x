from __future__ import annotations

import asyncio
from dataclasses import dataclass


@dataclass(slots=True)
class AccountLockLease:
    account_id: str
    _lock: asyncio.Lock
    released: bool = False

    async def __aenter__(self) -> AccountLockLease:
        return self

    async def __aexit__(self, *_args: object) -> None:
        self.release()

    def release(self) -> None:
        if not self.released:
            self.released = True
            self._lock.release()


class AccountLockManager:
    def __init__(self) -> None:
        self._locks: dict[str, asyncio.Lock] = {}

    async def acquire(self, account_id: str) -> AccountLockLease:
        lock = self._locks.setdefault(account_id, asyncio.Lock())
        await lock.acquire()
        return AccountLockLease(account_id, lock)

    async def try_acquire(self, account_id: str) -> AccountLockLease | None:
        lock = self._locks.setdefault(account_id, asyncio.Lock())
        if lock.locked():
            return None
        await lock.acquire()
        return AccountLockLease(account_id, lock)

    def locked(self, account_id: str) -> bool:
        lock = self._locks.get(account_id)
        return bool(lock and lock.locked())
