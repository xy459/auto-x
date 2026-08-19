from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from x_actions_playwright import XActions
from x_actions_playwright.core import cancellable_sleep

from x_ops.integrations.browser_custom import BrowserCustomGateway
from x_ops.integrations.x_actions import BoundXActionsFactory
from x_ops.task_programs._common import write_options
from x_ops.task_sdk import CancellationToken, TaskTimeoutError


class NativeLease:
    page = object()
    browser_was_started = True

    async def release(self, *, close_browser=False):
        return {
            "released": True,
            "pageErrors": ["page failed"],
            "browser": {"closeError": "browser failed"} if close_browser else None,
        }


class Registry:
    def __init__(self):
        self.args = None

    async def acquire_page(self, account, config):
        self.args = (account, config)
        return NativeLease()


async def test_browser_custom_gateway_uses_registry_page_lease():
    registry = Registry()
    gateway = BrowserCustomGateway(lambda account_id: (f"account:{account_id}", "config"), registry=registry)
    lease = await gateway.acquire(browser_account_id="b-1", task_run_id="run")
    assert registry.args == ("account:b-1", "config")
    assert lease.page is NativeLease.page
    report = await lease.release(close_browser=True)
    assert [item["code"] for item in report.warnings] == [
        "TASK_PAGE_CLOSE_FAILED",
        "BROWSER_CLOSE_FAILED",
    ]


async def test_task_program_write_options_execute_through_real_x_actions_facade():
    class Adapter:
        async def dispatch(self, _page, _handler, _payload, options):
            options.trace.mark_mutation_triggered()
            return {"status": "success", "evidence": ["clicked"]}

    class Page:
        pass

    cancellation = asyncio.Event()
    context = SimpleNamespace(
        account=SimpleNamespace(account_id="account-1"),
        cancellation=cancellation,
    )
    actions = BoundXActionsFactory(XActions(adapter=Adapter())).bind(Page())

    result = await actions.interaction.like(
        {"tweetId": "123"},
        options=write_options(context, "like:account-1:123"),
    )

    assert result.status == "success"


async def test_task_deadline_crosses_x_actions_as_task_timeout():
    class Adapter:
        async def dispatch(self, _page, _handler, _payload, options):
            await cancellable_sleep(1_000, options.cancellation)
            return {"status": "success"}

    class Page:
        pass

    cancellation = CancellationToken(
        task_run_id="run",
        is_cancel_requested=lambda: asyncio.sleep(0, result=False),
        deadline=datetime.now(UTC) + timedelta(seconds=0.02),
        poll_interval=0.005,
    )
    context = SimpleNamespace(
        account=SimpleNamespace(account_id="account-1"),
        cancellation=cancellation,
    )
    actions = BoundXActionsFactory(XActions(adapter=Adapter())).bind(Page())

    with pytest.raises(TaskTimeoutError):
        await actions.interaction.like(
            {"tweetId": "123"},
            options=write_options(context, "like:account-1:123", timeout_ms=2_000),
        )
