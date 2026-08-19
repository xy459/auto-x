from __future__ import annotations

import asyncio

import pytest

from x_actions_playwright import ActionError, MemoryIdempotencyStore, XActions


class StubAdapter:
    selected_tweet_id = None

    def __getattr__(self, name):
        async def implementation(page, payload, options):
            return {"status": "success", "handler": name, "payload": payload, "dryRun": options.dry_run}

        return implementation

    async def dispatch(self, page, handler, payload, options):
        return await getattr(self, handler)(page, payload, options)


@pytest.mark.asyncio
async def test_namespaced_and_generic_execution_return_same_contract():
    actions = XActions(adapter=StubAdapter())
    direct = await actions.execute(object(), "context.inspect")
    namespaced = await actions.context.inspect(object())
    assert direct.status == namespaced.status == "success"
    assert direct.action == namespaced.action == "context.inspect"


@pytest.mark.asyncio
async def test_write_requires_confirmation_but_dry_run_does_not():
    actions = XActions(adapter=StubAdapter())
    with pytest.raises(ActionError) as caught:
        await actions.interaction.like(object(), {"tweetId": "1"})
    assert caught.value.code == "CONFIRMATION_REQUIRED"
    result = await actions.interaction.like(object(), {"tweetId": "1"}, {"dryRun": True})
    assert result.status == "success"


@pytest.mark.asyncio
async def test_idempotency_store_skips_reused_live_write_key():
    store = MemoryIdempotencyStore()
    actions = XActions(adapter=StubAdapter(), idempotency_store=store)
    options = {"confirmLive": True, "idempotencyKey": "job:1", "accountScope": "acc-1"}
    first = await actions.interaction.like(object(), {"tweetId": "1"}, options)
    second = await actions.interaction.like(object(), {"tweetId": "1"}, options)
    assert first.status == "success"
    assert second.status == "skipped"
    assert second.data["reason"] == "idempotency-key-reused"


@pytest.mark.asyncio
async def test_invalid_idempotency_key_and_unknown_action_are_structured():
    actions = XActions(adapter=StubAdapter())
    with pytest.raises(ActionError) as invalid:
        await actions.interaction.like(object(), {"tweetId": "1"}, {"confirmLive": True, "idempotencyKey": " "})
    assert invalid.value.code == "CONTENT_MISMATCH"
    with pytest.raises(ActionError) as unknown:
        await actions.execute(object(), "missing.action")
    assert unknown.value.code == "ACTION_UNSUPPORTED"


@pytest.mark.asyncio
async def test_workflow_only_retries_safe_actions_and_supports_conditions():
    actions = XActions(adapter=StubAdapter())
    result = await actions.workflow.run(
        object(),
        [
            {"action": "context.inspect", "when": lambda _: False},
            {"action": "browse.wait", "payload": {"durationMs": 0}, "retries": 2},
        ],
    )
    assert [step["status"] for step in result.data["steps"]] == ["skipped", "success"]
    with pytest.raises(ActionError) as caught:
        await actions.workflow.run(object(), [{"action": "interaction.like", "payload": {"tweetId": "1"}, "options": {"confirmLive": True}, "retries": 1}])
    assert caught.value.code == "TARGET_UNSAFE"


@pytest.mark.asyncio
async def test_workflow_cancellation_is_observed_between_steps():
    actions = XActions(adapter=StubAdapter())
    cancellation = asyncio.Event()
    cancellation.set()
    with pytest.raises(ActionError) as caught:
        await actions.workflow.run(object(), [{"action": "context.inspect"}], cancellation=cancellation)
    assert caught.value.code == "USER_CANCELLED"
