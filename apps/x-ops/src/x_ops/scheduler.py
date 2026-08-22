"""Minimal scheduler that creates ordinary TaskRuns through the admin backend.

There is intentionally no second execution path here.  A due plan calls
``backend.trigger_task(..., "schedule")`` -- the exact same TaskRun creation
service used by manual execution and rerun.
"""
from __future__ import annotations

import asyncio
import logging
import random
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .api.contracts import AdminBackend

LOGGER = logging.getLogger(__name__)


class TaskScheduler:
    def __init__(
        self,
        backend: AdminBackend,
        *,
        poll_interval_seconds: float = 30.0,
        poll_interval_provider: Callable[[], float] | None = None,
    ):
        self.backend = backend
        self.poll_interval_seconds = poll_interval_seconds
        self.poll_interval_provider = poll_interval_provider
        self._stopping = asyncio.Event()
        self._last_fired: dict[str, str] = {}

    async def run(self) -> None:
        self._stopping.clear()
        while not self._stopping.is_set():
            try:
                await self.poll_once()
            except Exception:  # noqa: BLE001
                LOGGER.exception("scheduler poll failed; the scheduler will continue")
            interval = (
                max(0.1, float(self.poll_interval_provider()))
                if self.poll_interval_provider
                else self.poll_interval_seconds
            )
            try:
                await asyncio.wait_for(
                    self._stopping.wait(), timeout=interval
                )
            except TimeoutError:
                pass

    def stop(self) -> None:
        self._stopping.set()

    async def poll_once(self, now: datetime | None = None) -> list[str]:
        now = (now or datetime.now(UTC)).astimezone(UTC)
        tasks = await self.backend.list_tasks({"enabled": True})
        triggered: list[str] = []
        for task in tasks:
            try:
                schedule = task.get("schedule")
                if not isinstance(schedule, Mapping) or not schedule.get("enabled", True):
                    continue
                schedule = await self._ensure_interval_next_run(task, schedule, now)
                fire_key = _fire_key(schedule, now)
                task_id = str(task.get("id") or "")
                if (
                    not task_id
                    or fire_key is None
                    or schedule.get("last_fire_key") == fire_key
                    or self._last_fired.get(task_id) == fire_key
                ):
                    continue
                result = await self.backend.trigger_task(
                    task_id,
                    "schedule",
                    fire_key=fire_key,
                )
                if result is None:
                    continue
                # The SQLite reservation and TaskRun creation are atomic. This
                # JSON field is only a presentation cache and local fast path.
                updated_schedule = dict(schedule)
                updated_schedule["last_fire_key"] = fire_key
                updated_schedule["last_run_at"] = now.isoformat()
                if _uses_interval_jitter(updated_schedule):
                    next_run_at = _next_jittered_interval_at(updated_schedule, now)
                    if next_run_at is not None:
                        updated_schedule["next_run_at"] = next_run_at.isoformat()
                if str(schedule.get("type") or "once") == "once":
                    updated_schedule["enabled"] = False
                await self.backend.update_task(task_id, {"schedule": updated_schedule})
                self._last_fired[task_id] = fire_key
                if result.get("runs"):
                    triggered.append(task_id)
            except Exception:  # noqa: BLE001
                LOGGER.exception(
                    "scheduled task processing failed",
                    extra={"task_id": str(task.get("id") or "")},
                )
        return triggered

    async def _ensure_interval_next_run(
        self,
        task: Mapping[str, Any],
        schedule: Mapping[str, Any],
        now: datetime,
    ) -> Mapping[str, Any]:
        if str(schedule.get("type") or "once") != "interval" or not _uses_interval_jitter(schedule):
            return schedule
        if _parse_datetime(schedule.get("next_run_at")) is not None:
            return schedule
        updated_schedule = dict(schedule)
        anchor = _parse_datetime(schedule.get("last_run_at")) or _parse_datetime(schedule.get("start_at")) or now
        next_run_at = _next_jittered_interval_at(updated_schedule, anchor)
        if next_run_at is None:
            return schedule
        updated_schedule["next_run_at"] = next_run_at.isoformat()
        task_id = str(task.get("id") or "")
        if task_id:
            await self.backend.update_task(task_id, {"schedule": updated_schedule})
        return updated_schedule


def _fire_key(schedule: Mapping[str, Any], now: datetime) -> str | None:
    kind = str(schedule.get("type") or "once")
    if kind == "once":
        run_at = _parse_datetime(schedule.get("run_at") or schedule.get("next_run_at"))
        if run_at is None or now < run_at:
            return None
        return f"once:{run_at.isoformat()}"
    if kind == "interval":
        try:
            seconds = int(schedule.get("interval_seconds") or 0)
        except (TypeError, ValueError):
            return None
        next_run_at = _parse_datetime(schedule.get("next_run_at"))
        if next_run_at is not None:
            if seconds < 1 or now < next_run_at:
                return None
            return f"interval:{next_run_at.isoformat()}"
        anchor = _parse_datetime(schedule.get("start_at")) or datetime(1970, 1, 1, tzinfo=UTC)
        if seconds < 1 or now < anchor:
            return None
        bucket = int((now - anchor).total_seconds() // seconds)
        return f"interval:{bucket}"
    if kind == "cron":
        expression = str(schedule.get("cron") or "")
        try:
            local_now = now.astimezone(ZoneInfo(str(schedule.get("timezone") or "UTC")))
        except ZoneInfoNotFoundError:
            return None
        if _cron_matches(expression, local_now):
            return f"cron:{now.strftime('%Y-%m-%dT%H:%M')}"
    return None


def _uses_interval_jitter(schedule: Mapping[str, Any]) -> bool:
    return _interval_jitter_seconds(schedule) > 0


def _interval_jitter_seconds(schedule: Mapping[str, Any]) -> int:
    try:
        seconds = int(schedule.get("jitter_seconds") or schedule.get("interval_jitter_seconds") or 0)
    except (TypeError, ValueError):
        seconds = 0
    if seconds > 0:
        return seconds
    try:
        interval = int(schedule.get("interval_seconds") or 0)
        ratio = float(schedule.get("interval_jitter_ratio") or 0)
    except (TypeError, ValueError):
        return 0
    if interval < 1 or ratio <= 0:
        return 0
    return max(1, int(interval * min(ratio, 1.0)))


def _next_jittered_interval_at(schedule: Mapping[str, Any], anchor: datetime) -> datetime | None:
    try:
        interval = int(schedule.get("interval_seconds") or 0)
    except (TypeError, ValueError):
        return None
    if interval < 1:
        return None
    jitter = min(interval - 1, _interval_jitter_seconds(schedule))
    offset = random.randint(-jitter, jitter) if jitter > 0 else 0
    return anchor.astimezone(UTC) + timedelta(seconds=interval + offset)


def _parse_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _cron_matches(expression: str, now: datetime) -> bool:
    """Match a deliberately small, dependency-free five-field cron subset.

    Each field supports ``*``, a single integer, comma-separated integers and
    inclusive ranges.  This covers common admin schedules while invalid input
    simply does not fire.
    """
    fields = expression.split()
    if len(fields) != 5:
        return False
    values = (now.minute, now.hour, now.day, now.month, (now.weekday() + 1) % 7)
    limits = ((0, 59), (0, 23), (1, 31), (1, 12), (0, 6))
    return all(
        _cron_field_matches(field, value, bounds)
        for field, value, bounds in zip(fields, values, limits, strict=True)
    )


def _cron_field_matches(field: str, value: int, bounds: tuple[int, int]) -> bool:
    if field == "*":
        return True
    try:
        accepted: set[int] = set()
        for part in field.split(","):
            if "-" in part:
                start, end = (int(item) for item in part.split("-", 1))
                if start > end:
                    return False
                accepted.update(range(start, end + 1))
            else:
                accepted.add(int(part))
    except ValueError:
        return False
    low, high = bounds
    return all(low <= item <= high for item in accepted) and value in accepted
