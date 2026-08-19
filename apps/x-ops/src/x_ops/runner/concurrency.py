from __future__ import annotations

import asyncio
from dataclasses import dataclass


@dataclass(slots=True)
class ExecutionSlot:
    _manager: ExecutionSlotManager
    released: bool = False

    async def __aenter__(self) -> ExecutionSlot:
        return self

    async def __aexit__(self, *_args: object) -> None:
        await self.release()

    async def release(self) -> None:
        if not self.released:
            self.released = True
            await self._manager._release()


class ExecutionSlotManager:
    def __init__(self, limit: int) -> None:
        if limit < 1:
            raise ValueError("execution slot limit must be positive")
        self.limit = limit
        self._condition = asyncio.Condition()
        self._active = 0
        self._max_active = 0

    @property
    def active(self) -> int:
        return self._active

    @property
    def max_active(self) -> int:
        return self._max_active

    async def acquire(self) -> ExecutionSlot:
        async with self._condition:
            await self._condition.wait_for(lambda: self._active < self.limit)
            self._active += 1
            self._max_active = max(self._max_active, self._active)
        return ExecutionSlot(self)

    async def set_limit(self, limit: int) -> None:
        if limit < 1:
            raise ValueError("execution slot limit must be positive")
        async with self._condition:
            self.limit = limit
            self._condition.notify_all()

    async def _release(self) -> None:
        async with self._condition:
            self._active -= 1
            self._condition.notify_all()
