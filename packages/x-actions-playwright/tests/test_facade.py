from __future__ import annotations

import asyncio

import pytest

from x_actions_playwright import ActionError, ExecutionOptions, MemoryIdempotencyStore, XActions
from x_actions_playwright.core import cancellable_sleep, totp_now


class StubAdapter:
    selected_tweet_id = None

    def __getattr__(self, name):
        async def implementation(page, payload, options):
            if options.confirm_live and not options.dry_run:
                options.trace.mark_mutation_triggered()
            return {"status": "success", "handler": name, "payload": payload, "dryRun": options.dry_run}

        return implementation

    async def dispatch(self, page, handler, payload, options):
        return await getattr(self, handler)(page, payload, options)


class SlowWriteAdapter(StubAdapter):
    async def interaction_like(self, page, payload, options):
        options.trace.mark_mutation_triggered()
        await asyncio.sleep(2)
        return {"status": "success"}


class PreMutationSlowWriteAdapter(StubAdapter):
    async def interaction_like(self, page, payload, options):
        await asyncio.sleep(2)
        return {"status": "success"}


class SlowReadAdapter(StubAdapter):
    async def inspect(self, page, payload, options):
        await asyncio.sleep(2)
        return {"status": "success"}


class CountingAdapter(StubAdapter):
    def __init__(self):
        self.calls = 0

    async def interaction_like(self, page, payload, options):
        self.calls += 1
        options.trace.mark_mutation_triggered()
        await asyncio.sleep(0.05)
        return {"status": "success"}


class ClosingAdapter(StubAdapter):
    async def interaction_like(self, page, payload, options):
        options.trace.mark_mutation_triggered()
        raise RuntimeError("Target page, context or browser has been closed")


class PreMutationClosingAdapter(StubAdapter):
    async def interaction_like(self, page, payload, options):
        raise RuntimeError("Target page, context or browser has been closed")


class BlockingAdapter(StubAdapter):
    def __init__(self):
        self.calls = 0
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def inspect(self, page, payload, options):
        self.calls += 1
        if self.calls == 1:
            self.entered.set()
            await self.release.wait()
        return {"status": "success"}


@pytest.mark.asyncio
async def test_namespaced_and_generic_execution_return_same_contract():
    actions = XActions(adapter=StubAdapter())
    direct = await actions.execute(object(), "context.inspect")
    namespaced = await actions.context.inspect(object())
    assert direct.status == namespaced.status == "success"
    assert direct.action == namespaced.action == "context.inspect"


def test_totp_generation_uses_standard_rfc_vector():
    assert totp_now("GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ", timestamp=59, digits=8) == "94287082"


def test_account_login_is_exposed_in_catalog_namespace():
    actions = XActions(adapter=StubAdapter())
    definition = actions.get_action_definition("account.login")
    assert definition is not None
    assert definition.access == "write"
    assert callable(actions.account.login)


def test_author_follow_and_profile_post_actions_are_exposed():
    actions = XActions(adapter=StubAdapter())
    assert actions.get_action_definition("account.followHandle") is not None
    assert callable(actions.account.followHandle)
    assert callable(actions.account.listPosts)
    assert callable(actions.account.scrollPosts)


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
async def test_idempotency_claim_is_atomic_under_concurrency():
    adapter = CountingAdapter()
    actions = XActions(adapter=adapter, idempotency_store=MemoryIdempotencyStore())
    options = {"confirmLive": True, "idempotencyKey": "job:concurrent", "accountScope": "acc-1"}
    results = await asyncio.gather(
        actions.interaction.like(object(), {"tweetId": "1"}, options),
        actions.interaction.like(object(), {"tweetId": "1"}, options),
        return_exceptions=True,
    )
    completed = [result for result in results if not isinstance(result, BaseException)]
    rejected = [result for result in results if isinstance(result, ActionError)]
    assert len(completed) == len(rejected) == 1
    assert completed[0].status == "success"
    assert rejected[0].code == "IDEMPOTENCY_IN_PROGRESS"
    assert adapter.calls == 1
    repeated = await actions.interaction.like(object(), {"tweetId": "1"}, options)
    assert repeated.status == "skipped"


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
async def test_invalid_target_id_and_options_are_structured():
    actions = XActions(adapter=StubAdapter())
    with pytest.raises(ActionError) as invalid_id:
        await actions.interaction.like(object(), {"tweetId": "1'] button"}, {"dryRun": True})
    assert invalid_id.value.code == "CONTENT_MISMATCH"
    with pytest.raises(ActionError) as invalid_timeout:
        await actions.context.inspect(object(), options={"timeoutMs": "soon"})
    assert invalid_timeout.value.code == "CONTENT_MISMATCH"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("confirmLive", "false"),
        ("confirmLive", 1),
        ("dryRun", "true"),
        ("dryRun", 0),
        ("captureFailure", "false"),
        ("captureFailure", None),
    ],
)
async def test_boolean_options_require_actual_booleans(name, value):
    actions = XActions(adapter=StubAdapter())
    with pytest.raises(ActionError) as caught:
        await actions.context.inspect(object(), options={name: value})
    assert caught.value.code == "CONTENT_MISMATCH"
    assert name in caught.value.message


@pytest.mark.asyncio
async def test_boolean_payload_fallback_and_execution_options_are_strict():
    actions = XActions(adapter=StubAdapter())
    with pytest.raises(ActionError) as payload_error:
        await actions.interaction.like(object(), {"tweetId": "1", "confirmLive": "false"})
    assert payload_error.value.code == "CONTENT_MISMATCH"

    invalid_options = ExecutionOptions()
    invalid_options.dry_run = "false"  # type: ignore[assignment]
    with pytest.raises(ActionError) as options_error:
        await actions.context.inspect(object(), options=invalid_options)
    assert options_error.value.code == "CONTENT_MISMATCH"


@pytest.mark.asyncio
async def test_idempotency_requires_account_scope():
    actions = XActions(adapter=StubAdapter())
    with pytest.raises(ActionError) as caught:
        await actions.interaction.like(
            object(),
            {"tweetId": "1"},
            {"confirmLive": True, "idempotencyKey": "job:1"},
        )
    assert caught.value.code == "CONTENT_MISMATCH"


@pytest.mark.asyncio
async def test_cancellation_is_observed_before_dispatch():
    actions = XActions(adapter=StubAdapter())
    cancellation = asyncio.Event()
    cancellation.set()
    with pytest.raises(ActionError) as caught:
        await actions.context.inspect(object(), options={"cancellation": cancellation})
    assert caught.value.code == "USER_CANCELLED"


@pytest.mark.asyncio
async def test_cancellation_while_waiting_for_page_lock_does_not_leak_lock():
    adapter = BlockingAdapter()
    actions = XActions(adapter=adapter)
    page = object()
    first = asyncio.create_task(actions.context.inspect(page))
    await adapter.entered.wait()

    cancellation = asyncio.Event()
    second = asyncio.create_task(actions.context.inspect(page, options={"cancellation": cancellation}))
    await asyncio.sleep(0)
    cancellation.set()
    with pytest.raises(ActionError) as caught:
        await second
    assert caught.value.code == "USER_CANCELLED"

    adapter.release.set()
    assert (await first).status == "success"
    assert (await actions.context.inspect(page)).status == "success"


@pytest.mark.asyncio
async def test_cancellation_signal_exception_crosses_facade_unchanged():
    class DeadlineReached(RuntimeError):
        pass

    class DeadlineSignal:
        def is_set(self):
            return False

        async def wait(self):
            await asyncio.sleep(0.01)
            raise DeadlineReached("deadline")

    class CancellableAdapter(StubAdapter):
        async def inspect(self, _page, _payload, options):
            await cancellable_sleep(1_000, options.cancellation)
            return {"status": "success"}

    actions = XActions(adapter=CancellableAdapter())
    with pytest.raises(DeadlineReached, match="deadline"):
        await actions.context.inspect(
            object(),
            options={"cancellation": DeadlineSignal(), "timeoutMs": 2_000},
        )


@pytest.mark.asyncio
async def test_deadline_after_live_write_mutation_is_not_recorded_as_user_cancel():
    class DeadlineTimeoutError(RuntimeError):
        pass

    class DeadlineSignal:
        def is_set(self):
            return False

        async def wait(self):
            await asyncio.sleep(0.01)
            raise DeadlineTimeoutError("deadline")

    class CancellableWriteAdapter(StubAdapter):
        async def interaction_like(self, _page, _payload, options):
            options.trace.mark_mutation_triggered()
            await cancellable_sleep(1_000, options.cancellation)
            return {"status": "success"}

    result = await XActions(adapter=CancellableWriteAdapter()).interaction.like(
        object(),
        {"tweetId": "1"},
        {
            "confirmLive": True,
            "cancellation": DeadlineSignal(),
            "timeoutMs": 2_000,
        },
    )

    assert result.status == "uncertain"
    assert result.data["error"]["code"] == "TIMEOUT"


@pytest.mark.asyncio
async def test_live_write_timeout_is_uncertain_and_reservation_is_retained():
    actions = XActions(adapter=SlowWriteAdapter(), default_timeout_ms=250)
    options = {
        "confirmLive": True,
        "timeoutMs": 250,
        "idempotencyKey": "job:timeout",
        "accountScope": "acc-1",
    }
    first = await actions.interaction.like(object(), {"tweetId": "1"}, options)
    second = await actions.interaction.like(object(), {"tweetId": "1"}, options)
    assert first.status == "uncertain"
    assert first.data["error"]["code"] == "TIMEOUT"
    assert second.status == "uncertain"
    assert second.data["previous"]["state"] == "uncertain"


@pytest.mark.asyncio
async def test_pre_mutation_timeout_is_deterministic_and_releases_reservation():
    store = MemoryIdempotencyStore()
    actions = XActions(
        adapter=PreMutationSlowWriteAdapter(),
        idempotency_store=store,
        default_timeout_ms=250,
    )
    options = {
        "confirmLive": True,
        "timeoutMs": 250,
        "idempotencyKey": "job:pre-mutation-timeout",
        "accountScope": "acc-1",
    }
    with pytest.raises(ActionError) as caught:
        await actions.interaction.like(object(), {"tweetId": "1"}, options)
    assert caught.value.code == "TIMEOUT"
    assert await store.get("acc-1:interaction.like:job:pre-mutation-timeout") is None


@pytest.mark.asyncio
async def test_existing_pending_and_invalid_terminal_states_are_not_skipped():
    store = MemoryIdempotencyStore()
    actions = XActions(adapter=StubAdapter(), idempotency_store=store)
    pending_key = "acc-1:interaction.like:job:pending"
    await store.put(pending_key, {"state": "pending", "action": "interaction.like"})
    with pytest.raises(ActionError) as pending:
        await actions.interaction.like(
            object(),
            {"tweetId": "1"},
            {"confirmLive": True, "idempotencyKey": "job:pending", "accountScope": "acc-1"},
        )
    assert pending.value.code == "IDEMPOTENCY_IN_PROGRESS"
    assert pending.value.retryable is True

    failed_key = "acc-1:interaction.like:job:failed"
    await store.put(failed_key, {"state": "failed", "action": "interaction.like"})
    with pytest.raises(ActionError) as failed:
        await actions.interaction.like(
            object(),
            {"tweetId": "1"},
            {"confirmLive": True, "idempotencyKey": "job:failed", "accountScope": "acc-1"},
        )
    assert failed.value.code == "IDEMPOTENCY_STATE_CONFLICT"


@pytest.mark.asyncio
async def test_browser_close_during_live_write_is_uncertain():
    result = await XActions(adapter=ClosingAdapter()).interaction.like(
        object(),
        {"tweetId": "1"},
        {"confirmLive": True},
    )
    assert result.status == "uncertain"
    assert result.data["error"]["code"] == "BROWSER_CLOSED_DURING_RUN"


@pytest.mark.asyncio
async def test_browser_close_before_mutation_is_a_deterministic_error():
    with pytest.raises(ActionError) as caught:
        await XActions(adapter=PreMutationClosingAdapter()).interaction.like(
            object(),
            {"tweetId": "1"},
            {"confirmLive": True},
        )
    assert caught.value.code == "BROWSER_CLOSED_DURING_RUN"


@pytest.mark.asyncio
async def test_successful_live_write_exposes_complete_execution_trace():
    result = await XActions(adapter=StubAdapter()).interaction.like(
        object(),
        {"tweetId": "1"},
        {"confirmLive": True},
    )
    assert result.meta["executionTrace"] == {
        "dispatchStarted": True,
        "mutationTriggered": True,
        "postconditionVerified": True,
    }


@pytest.mark.asyncio
async def test_read_timeout_remains_a_deterministic_error():
    actions = XActions(adapter=SlowReadAdapter(), default_timeout_ms=250)
    with pytest.raises(ActionError) as caught:
        await actions.execute(object(), "context.inspect", options={"timeoutMs": 250})
    assert caught.value.code == "TIMEOUT"
