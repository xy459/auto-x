from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol


class IdempotencyStore(Protocol):
    async def get(self, key: str) -> dict[str, Any] | None: ...

    async def put(self, key: str, value: dict[str, Any]) -> None: ...

    async def delete(self, key: str) -> None: ...


@dataclass(slots=True)
class MemoryIdempotencyStore:
    """Process-local reference store. Use a database implementation in production."""

    _values: dict[str, dict[str, Any]] = field(default_factory=dict, init=False)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False)

    async def get(self, key: str) -> dict[str, Any] | None:
        async with self._lock:
            value = self._values.get(key)
            return dict(value) if value else None

    async def put(self, key: str, value: dict[str, Any]) -> None:
        async with self._lock:
            self._values[key] = {**value, "storedAt": datetime.now(UTC).isoformat()}

    async def delete(self, key: str) -> None:
        async with self._lock:
            self._values.pop(key, None)
