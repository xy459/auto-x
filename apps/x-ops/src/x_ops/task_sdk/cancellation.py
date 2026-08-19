from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import TypeVar

from .errors import TaskCancelledError, TaskTimeoutError

T = TypeVar("T")


class CancellationToken:
    def __init__(
        self,
        *,
        task_run_id: str,
        is_cancel_requested: Callable[[], Awaitable[bool]],
        deadline: datetime | None = None,
        poll_interval: float = 0.05,
    ) -> None:
        self.task_run_id = task_run_id
        self.deadline = deadline
        self.poll_interval = max(0.01, poll_interval)
        self._is_cancel_requested = is_cancel_requested
        self._local_event = asyncio.Event()

    def request_local_cancel(self) -> None:
        self._local_event.set()

    def is_set(self) -> bool:
        """Synchronous view used by x-actions-playwright cancellation hooks."""
        deadline_reached = bool(self.deadline and datetime.now(UTC) >= self.deadline)
        return self._local_event.is_set() or deadline_reached

    async def wait(self) -> bool:
        """Wait until cooperative cancellation is observed."""
        return await self.wait_cancelled()

    async def _reason(self) -> str | None:
        if self.deadline and datetime.now(UTC) >= self.deadline:
            return "deadline"
        if self._local_event.is_set():
            return "cancelled"
        if await self._is_cancel_requested():
            # Cache the remote request so synchronous CancellationSignal checks
            # see the same state after wait() wakes.
            self._local_event.set()
            return "cancelled"
        return None

    async def raise_if_cancelled(self) -> None:
        reason = await self._reason()
        if reason == "deadline":
            raise TaskTimeoutError(self.task_run_id)
        if reason == "cancelled":
            raise TaskCancelledError(self.task_run_id)

    async def wait_cancelled(self, timeout: float | None = None) -> bool:
        loop = asyncio.get_running_loop()
        end = None if timeout is None else loop.time() + max(0.0, timeout)
        while True:
            reason = await self._reason()
            if reason == "deadline":
                raise TaskTimeoutError(self.task_run_id)
            if reason == "cancelled":
                return True
            if end is not None and loop.time() >= end:
                return False
            delay = self.poll_interval if end is None else min(self.poll_interval, max(0.0, end - loop.time()))
            try:
                await asyncio.wait_for(self._local_event.wait(), timeout=delay)
            except TimeoutError:
                pass

    async def sleep(self, seconds: float) -> None:
        if seconds < 0:
            raise ValueError("seconds must be non-negative")
        if await self.wait_cancelled(timeout=seconds):
            raise TaskCancelledError(self.task_run_id)

    async def wait_for(self, awaitable: Awaitable[T]) -> T:
        """Runner helper: wait for a resource without adding another SDK capability."""

        resource = asyncio.ensure_future(awaitable)
        cancelled = asyncio.create_task(self.wait_cancelled())

        async def settle_resource() -> None:
            if not resource.done():
                resource.cancel()
            acquired = (await asyncio.gather(resource, return_exceptions=True))[0]
            if isinstance(acquired, BaseException):
                return
            release = getattr(acquired, "release", None)
            if release is None:
                return
            released = release()
            if hasattr(released, "__await__"):
                await released

        try:
            done, _ = await asyncio.wait({resource, cancelled}, return_when=asyncio.FIRST_COMPLETED)
            if cancelled in done:
                cancellation_error = cancelled.exception()
                await settle_resource()
                if cancellation_error is not None:
                    raise cancellation_error
                raise TaskCancelledError(self.task_run_id)
            return resource.result()
        except asyncio.CancelledError:
            await asyncio.shield(settle_resource())
            raise
        finally:
            cancelled.cancel()
            await asyncio.gather(cancelled, return_exceptions=True)


class CancellationTokenFactory:
    def __init__(self, is_cancel_requested: Callable[[str], Awaitable[bool]], *, poll_interval: float = 0.05) -> None:
        self._check = is_cancel_requested
        self._poll_interval = poll_interval

    def set_poll_interval(self, value: float) -> None:
        self._poll_interval = max(0.01, value)

    def create(self, *, task_run_id: str, deadline: datetime | None = None) -> CancellationToken:
        return CancellationToken(
            task_run_id=task_run_id,
            deadline=deadline,
            poll_interval=self._poll_interval,
            is_cancel_requested=lambda: self._check(task_run_id),
        )
