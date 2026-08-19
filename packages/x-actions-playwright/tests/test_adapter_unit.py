from __future__ import annotations

from datetime import datetime

import pytest

from x_actions_playwright.adapter import XAdapter
from x_actions_playwright.errors import ActionError
from x_actions_playwright.models import ExecutionOptions


class FakeLocator:
    def __init__(self, *, fail_trial: bool = False, fail_actual: bool = False) -> None:
        self.first = self
        self.fail_trial = fail_trial
        self.fail_actual = fail_actual
        self.clicks: list[bool] = []

    async def count(self) -> int:
        return 1

    async def is_visible(self) -> bool:
        return True

    async def is_enabled(self) -> bool:
        return True

    async def scroll_into_view_if_needed(self, *, timeout: int) -> None:  # noqa: ASYNC109
        assert timeout > 0

    async def click(self, *, timeout: int, trial: bool = False) -> None:  # noqa: ASYNC109
        assert timeout > 0
        self.clicks.append(trial)
        if trial and self.fail_trial:
            raise TimeoutError("trial click timed out")
        if not trial and self.fail_actual:
            raise TimeoutError("actual click timed out")


class FakeTimezonePage:
    def __init__(self, timezone_name: str) -> None:
        self.timezone_name = timezone_name

    async def evaluate(self, _expression: str) -> str:
        return self.timezone_name


@pytest.mark.asyncio
async def test_mutation_click_preflight_failure_does_not_mark_mutation() -> None:
    locator = FakeLocator(fail_trial=True)
    options = ExecutionOptions(confirm_live=True)

    with pytest.raises(ActionError) as caught:
        await XAdapter()._click(locator, "write control", options, mutation=True)

    assert caught.value.code == "TIMEOUT"
    assert locator.clicks == [True]
    assert options.trace.mutation_triggered is False


@pytest.mark.asyncio
async def test_mutation_click_dispatches_one_real_event_after_preflight() -> None:
    locator = FakeLocator()
    options = ExecutionOptions(confirm_live=True)

    await XAdapter()._click(locator, "write control", options, mutation=True)

    assert locator.clicks == [True, False]
    assert options.trace.mutation_triggered is True


@pytest.mark.asyncio
async def test_actual_click_failure_after_preflight_is_mutation_uncertain_boundary() -> None:
    locator = FakeLocator(fail_actual=True)
    options = ExecutionOptions(confirm_live=True)

    with pytest.raises(ActionError) as caught:
        await XAdapter()._click(locator, "write control", options, mutation=True)

    assert caught.value.code == "TIMEOUT"
    assert locator.clicks == [True, False]
    assert options.trace.mutation_triggered is True


@pytest.mark.asyncio
async def test_profile_local_schedule_rejects_dst_gap_as_nonexistent() -> None:
    with pytest.raises(ActionError) as caught:
        await XAdapter()._schedule_wall_time(
            FakeTimezonePage("America/New_York"),
            datetime(2025, 3, 9, 2, 30),
        )

    assert caught.value.code == "INVALID_SCHEDULE_TIME"
    assert "does not exist" in caught.value.message


@pytest.mark.asyncio
async def test_profile_local_schedule_rejects_dst_fold_as_ambiguous() -> None:
    with pytest.raises(ActionError) as caught:
        await XAdapter()._schedule_wall_time(
            FakeTimezonePage("America/New_York"),
            datetime(2025, 11, 2, 1, 30),
        )

    assert caught.value.code == "INVALID_SCHEDULE_TIME"
    assert "ambiguous" in caught.value.message
