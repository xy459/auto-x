from __future__ import annotations

from types import SimpleNamespace

import pytest

from x_ops.task_programs import browse_match_engage
from x_ops.task_programs._common import require_certain, run_timeline, write_options
from x_ops.task_sdk import TaskCancelledError, TaskUncertainError


class Cancellation:
    def __init__(self, cancel_after: int | None = None):
        self.checks = 0
        self.cancel_after = cancel_after

    async def raise_if_cancelled(self):
        self.checks += 1
        if self.cancel_after is not None and self.checks >= self.cancel_after:
            raise TaskCancelledError("run")


class Logger:
    def info(self, *_args, **_kwargs):
        pass


class AI:
    def __init__(self):
        self.calls = []

    async def generate(self, **kwargs):
        self.calls.append(kwargs)
        return "AI reply"


class Namespace:
    def __init__(self, owner, name):
        self.owner = owner
        self.name = name

    def __getattr__(self, method):
        async def call(payload=None, options=None, **kwargs):
            self.owner.calls.append((f"{self.name}.{method}", payload, options, kwargs))
            if f"{self.name}.{method}" == "timeline.collect":
                return {
                    "status": "success",
                    "data": {
                        "posts": [
                            {"postId": "1", "author": {"username": "target"}, "content": {"text": "Python news"}},
                            {"postId": "2", "author": {"username": "other"}, "content": {"text": "noise"}},
                        ]
                    },
                }
            return {"status": self.owner.write_status, "data": {}}

        return call


class Actions:
    def __init__(self, write_status="success"):
        self.calls = []
        self.write_status = write_status
        self.timeline = Namespace(self, "timeline")
        self.interaction = Namespace(self, "interaction")


def context(*, actions=None, cancellation=None, ai=None):
    return SimpleNamespace(
        actions=actions or Actions(),
        cancellation=cancellation or Cancellation(),
        ai=ai or AI(),
        logger=Logger(),
        account=SimpleNamespace(account_id="account-1"),
    )


async def test_engage_program_owns_matching_and_action_order():
    ctx = context()
    params = browse_match_engage.Params(
        target_authors={"target"}, keywords={"python"}, like=True, reply_mode="fixed", fixed_reply="hi"
    )
    output = await browse_match_engage.run(ctx, params)
    assert [call[0] for call in ctx.actions.calls] == [
        "timeline.collect",
        "interaction.like",
        "interaction.reply",
        "timeline.browse",
    ]
    assert output == {"posts_seen": 2, "matched": 1, "liked": 1, "replied": 1, "actions_completed": 2}


async def test_uncertain_write_is_propagated_to_runner_boundary():
    ctx = context(actions=Actions(write_status="uncertain"))
    params = browse_match_engage.Params(target_authors={"target"}, like=True)
    with pytest.raises(TaskUncertainError):
        await browse_match_engage.run(ctx, params)


async def test_cancel_between_actions_prevents_next_write():
    cancellation = Cancellation(cancel_after=4)
    ctx = context(cancellation=cancellation)
    params = browse_match_engage.Params(
        target_authors={"target"}, like=True, reply_mode="fixed", fixed_reply="hi"
    )
    with pytest.raises(TaskCancelledError):
        await browse_match_engage.run(ctx, params)
    assert [call[0] for call in ctx.actions.calls] == ["timeline.collect", "interaction.like"]


async def test_interaction_happens_before_advancing_virtualized_timeline():
    class Timeline:
        def __init__(self, owner):
            self.owner = owner
            self.batch = 0

        async def collect(self, payload, options=None):
            self.owner.calls.append(("timeline.collect", payload, options, {}))
            self.batch += 1
            return {
                "status": "success",
                "data": {
                    "posts": [
                        {
                            "postId": str(self.batch),
                            "author": {"username": "target"},
                            "content": {"text": f"batch {self.batch}"},
                        }
                    ]
                },
            }

        async def browse(self, payload, options=None):
            self.owner.calls.append(("timeline.browse", payload, options, {}))
            return {"status": "success", "data": {"scrolls": 1, "atBoundary": False}}

    actions = Actions()
    actions.timeline = Timeline(actions)
    ctx = context(actions=actions)
    params = browse_match_engage.Params(
        scroll_count=1,
        target_authors={"target"},
        like=True,
        reply_mode="none",
        max_engagements=2,
    )

    output = await browse_match_engage.run(ctx, params)

    assert [call[0] for call in actions.calls] == [
        "timeline.collect",
        "interaction.like",
        "timeline.browse",
        "timeline.collect",
        "interaction.like",
    ]
    assert output["posts_seen"] == 2
    assert output["liked"] == 2


def test_write_options_match_x_actions_idempotency_contract():
    ctx = context()
    options = write_options(ctx, "like:account-1:123")
    assert options["confirmLive"] is True
    assert options["accountScope"] == "account-1"
    assert options["cancellation"] is ctx.cancellation
    assert "timeoutMs" not in options

    overridden = write_options(ctx, "like:account-1:123", timeout_ms=30_000)
    assert overridden["timeoutMs"] == 30_000


def test_uncertain_idempotency_reuse_is_not_treated_as_normal_skip():
    with pytest.raises(TaskUncertainError):
        require_certain(
            {
                "status": "skipped",
                "data": {"previous": {"state": "uncertain"}},
            }
        )


async def test_timeline_navigation_is_retried_before_reporting_completion():
    class Timeline:
        def __init__(self):
            self.calls = []

        async def collect(self, payload, options=None):
            self.calls.append((payload, options))
            if len(self.calls) == 1:
                return {"status": "navigating", "data": {"requiresRetry": True}}
            return {
                "status": "success",
                "data": {
                    "scrolls": 1,
                    "posts": [{"postId": "123", "content": {"text": "hello"}}],
                },
            }

    timeline = Timeline()
    ctx = context(actions=SimpleNamespace(timeline=timeline))
    result = await run_timeline(
        ctx,
        feed="for_you",
        scroll_count=1,
        interval_seconds=1.5,
        collect=True,
    )

    assert len(timeline.calls) == 2
    assert timeline.calls[0][1]["timeoutMs"] > timeline.calls[0][0]["durationMs"]
    assert timeline.calls[0][1]["cancellation"] is ctx.cancellation
    assert result["scrolls"] == 1
    assert result["posts"][0]["postId"] == "123"
