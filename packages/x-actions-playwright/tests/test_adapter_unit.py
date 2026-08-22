from __future__ import annotations

from datetime import datetime

import pytest

import x_actions_playwright.adapter as adapter_module
from x_actions_playwright.adapter import XAdapter
from x_actions_playwright.errors import ActionError
from x_actions_playwright.models import ExecutionOptions


class FakeLocator:
    def __init__(self, *, fail_trial: bool = False, fail_actual: bool = False) -> None:
        self.first = self
        self.fail_trial = fail_trial
        self.fail_actual = fail_actual
        self.clicks: list[bool] = []
        self.forced_clicks: list[bool] = []
        self.timeouts: list[int] = []

    async def count(self) -> int:
        return 1

    async def is_visible(self) -> bool:
        return True

    async def is_enabled(self) -> bool:
        return True

    async def scroll_into_view_if_needed(self, *, timeout: int) -> None:  # noqa: ASYNC109
        assert timeout > 0

    async def click(self, *, timeout: int, trial: bool = False, force: bool = False) -> None:  # noqa: ASYNC109
        assert timeout > 0
        self.clicks.append(trial)
        self.forced_clicks.append(force)
        self.timeouts.append(timeout)
        if trial and self.fail_trial:
            raise TimeoutError("trial click timed out")
        if not trial and self.fail_actual:
            raise TimeoutError("actual click timed out")


class FakeTimezonePage:
    def __init__(self, timezone_name: str) -> None:
        self.timezone_name = timezone_name

    async def evaluate(self, _expression: str) -> str:
        return self.timezone_name


class FakeCodeInput:
    def __init__(self) -> None:
        self.value = ""
        self.first = self

    async def count(self) -> int:
        return 1

    async def is_visible(self) -> bool:
        return True

    async def scroll_into_view_if_needed(self, *, timeout: int) -> None:  # noqa: ASYNC109
        assert timeout > 0

    async def click(self, *, timeout: int) -> None:  # noqa: ASYNC109
        assert timeout > 0

    async def press(self, _key: str, *, timeout: int) -> None:  # noqa: ASYNC109
        assert timeout > 0
        self.value = ""

    async def type(self, value: str, *, delay: int, timeout: int) -> None:  # noqa: ASYNC109
        assert delay > 0
        assert timeout > 0
        self.value = value[:1]


class FakeCodeInputs:
    def __init__(self, count: int) -> None:
        self.items = [FakeCodeInput() for _ in range(count)]
        self.first = self.items[0]

    async def count(self) -> int:
        return len(self.items)

    def nth(self, index: int) -> FakeCodeInput:
        return self.items[index]


class FakeCodePage:
    def __init__(self, count: int = 6) -> None:
        self.inputs = FakeCodeInputs(count)

    def locator(self, selector: str) -> FakeCodeInputs:
        assert selector == 'input[inputmode="numeric"]'
        return self.inputs


class FakeBody:
    def __init__(self, text: str) -> None:
        self.text = text

    async def inner_text(self, *, timeout: int) -> str:  # noqa: ASYNC109
        assert timeout > 0
        return self.text


class FakeTextPage:
    def __init__(self, text: str, *, url: str = "https://x.com/i/flow/login") -> None:
        self.body = FakeBody(text)
        self.url = url

    def locator(self, selector: str) -> FakeBody:
        assert selector == "body"
        return self.body


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
    assert locator.forced_clicks == [False, True]
    assert locator.timeouts == [10_000, 5_000]
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
async def test_segmented_two_factor_code_is_typed_one_digit_per_input() -> None:
    page = FakeCodePage()

    await XAdapter()._fill_two_factor_code(
        page,
        page.inputs.first,
        "123456",
        ExecutionOptions(timeout_ms=2_000),
        typing_delay_ms=20,
    )

    assert [item.value for item in page.inputs.items] == list("123456")


@pytest.mark.asyncio
async def test_japanese_account_not_found_is_credentials_rejected() -> None:
    page = FakeTextPage("そのユーザー名を使用している有効なアカウントが見つかりません。")

    assert await XAdapter()._login_challenge_reason(page) == "credentials_rejected"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "text",
    [
        "Enter your two factor code Use your authenticator app to generate the code.",
        "输入你的两步验证码",
        "認証アプリで確認コードを生成してください",
    ],
)
async def test_two_factor_prompt_is_not_an_extra_identity_challenge(text: str) -> None:
    page = FakeTextPage(
        text,
        url="https://x.com/i/jf/onboarding/web#/s/two_factor_code/r-test",
    )

    assert await XAdapter()._login_challenge_reason(page) is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "text",
    [
        "Incorrect. Please try again.",
        "验证码不正确，请重试。",
        "認証コードが正しくありません。",
    ],
)
async def test_rejected_two_factor_code_has_specific_reason(text: str) -> None:
    page = FakeTextPage(
        text,
        url="https://x.com/i/jf/onboarding/web#/s/two_factor_code/r-test",
    )

    assert await XAdapter()._login_challenge_reason(page) == "two_factor_code_rejected"


@pytest.mark.asyncio
async def test_login_waits_through_transitional_two_factor_page(monkeypatch) -> None:
    page = FakeTextPage(
        "Enter your two factor code",
        url="https://x.com/i/jf/onboarding/web#/s/two_factor_code/r-test",
    )
    adapter = XAdapter()
    sessions = iter(
        [
            {"loggedIn": False, "username": None},
            {"loggedIn": True, "username": "alice"},
        ]
    )

    async def session(_page):
        return next(sessions)

    async def no_sleep(_milliseconds, _cancellation):
        return None

    monkeypatch.setattr(adapter, "_account_session_data", session)
    monkeypatch.setattr(adapter_module, "cancellable_sleep", no_sleep)

    result = await adapter._wait_after_login_attempt(
        page,
        ExecutionOptions(timeout_ms=2_000),
        expected_username="alice",
    )

    assert result["status"] == "success"


@pytest.mark.asyncio
async def test_fresh_totp_waits_for_next_code(monkeypatch) -> None:
    codes = iter(["111111", "222222"])

    async def no_sleep(_milliseconds, _cancellation):
        return None

    monkeypatch.setattr(adapter_module, "totp_now", lambda _secret: next(codes))
    monkeypatch.setattr(adapter_module.time, "time", lambda: 10)
    monkeypatch.setattr(adapter_module, "cancellable_sleep", no_sleep)

    code = await XAdapter()._fresh_totp_code(
        "JBSWY3DPEHPK3PXP",
        ExecutionOptions(timeout_ms=2_000),
        previous_code="111111",
    )

    assert code == "222222"


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
