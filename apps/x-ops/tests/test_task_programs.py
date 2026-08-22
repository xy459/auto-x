from __future__ import annotations

from types import SimpleNamespace

import pytest
from x_actions_playwright.errors import ActionError

from x_ops.task_programs import (
    browse_match_engage,
    browse_view_posts,
    login_accounts,
    search_authors_engage,
)
from x_ops.task_programs._common import (
    require_certain,
    run_timeline,
    run_timeline_batches,
    write_options,
)
from x_ops.task_sdk import TaskCancelledError, TaskUncertainError


class Cancellation:
    def __init__(self, cancel_after: int | None = None):
        self.task_run_id = "run"
        self.checks = 0
        self.cancel_after = cancel_after

    async def raise_if_cancelled(self):
        self.checks += 1
        if self.cancel_after is not None and self.checks >= self.cancel_after:
            raise TaskCancelledError("run")

    async def sleep(self, _seconds):
        await self.raise_if_cancelled()


class Logger:
    def info(self, *_args, **_kwargs):
        pass

    def warning(self, *_args, **_kwargs):
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
        self.account = Namespace(self, "account")


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
        target_authors={"target"},
        keywords={"python"},
        like=True,
        reply_mode="fixed",
        fixed_reply="hi",
        max_engagements=2,
    )
    output = await browse_match_engage.run(ctx, params)
    assert [call[0] for call in ctx.actions.calls] == [
        "timeline.collect",
        "interaction.like",
        "interaction.reply",
    ]
    assert output == {
        "posts_seen": 2,
        "matched": 1,
        "liked": 1,
        "replied": 1,
        "actions_completed": 2,
        "followed": 0,
        "follow_skipped": 0,
        "scrolls_completed": 0,
        "stop_reason": "program_limit",
    }


async def test_engage_program_can_follow_matched_author_without_changing_default_order():
    ctx = context()
    params = browse_match_engage.Params(
        target_authors={"target"},
        like=False,
        reply_mode="none",
        follow_authors=True,
        max_follows=1,
    )

    output = await browse_match_engage.run(ctx, params)

    assert [call[0] for call in ctx.actions.calls] == [
        "timeline.collect",
        "account.followHandle",
    ]
    assert output["followed"] == 1
    assert output["actions_completed"] == 0
    assert output["stop_reason"] == "program_limit"


async def test_uncertain_write_is_propagated_to_runner_boundary():
    ctx = context(actions=Actions(write_status="uncertain"))
    params = browse_match_engage.Params(target_authors={"target"}, like=True)
    with pytest.raises(TaskUncertainError):
        await browse_match_engage.run(ctx, params)


async def test_cancel_between_actions_prevents_next_write():
    cancellation = Cancellation(cancel_after=5)
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


async def test_browse_view_posts_opens_details_returns_and_refreshes():
    class Timeline:
        def __init__(self, owner):
            self.owner = owner

        async def collect(self, payload, options=None):
            self.owner.calls.append(("timeline.collect", payload, options, {}))
            return {
                "status": "success",
                "data": {
                    "posts": [
                        {
                            "postId": "100",
                            "author": {"username": "target"},
                            "content": {"text": "interesting post"},
                            "isAd": False,
                        }
                    ]
                },
            }

        async def browse(self, payload, options=None):
            self.owner.calls.append(("timeline.browse", payload, options, {}))
            return {"status": "success", "data": {"scrolls": 1, "atBoundary": False}}

        async def refreshNew(self, payload, options=None):
            self.owner.calls.append(("timeline.refreshNew", payload, options, {}))
            return {
                "status": "success",
                "data": {
                    "clickedShowNewPosts": True,
                    "usedHomeKey": bool(payload.get("homeFallback")),
                },
            }

    class Post:
        def __init__(self, owner):
            self.owner = owner

        async def openDetails(self, payload, options=None):
            self.owner.calls.append(("post.openDetails", payload, options, {}))
            return {"status": "navigating", "data": {"url": "https://x.com/target/status/100"}}

        async def exitDetails(self, payload, options=None):
            self.owner.calls.append(("post.exitDetails", payload, options, {}))
            return {"status": "success", "data": {"returned": True}}

    actions = Actions()
    actions.timeline = Timeline(actions)
    actions.post = Post(actions)
    ctx = context(actions=actions)
    params = browse_view_posts.Params(
        scroll_count=2,
        open_post_probability=1,
        min_scrolls_between_open=0,
        max_posts_opened=1,
        post_dwell_seconds_min=0,
        post_dwell_seconds_max=0,
        detail_scroll_probability=0,
        refresh_top_every_scrolls=1,
        refresh_top_every_seconds=0,
        home_show_every_refreshes=1,
        home_show_refresh_jitter=0,
    )

    output = await browse_view_posts.run(ctx, params)

    assert [call[0] for call in actions.calls] == [
        "timeline.collect",
        "post.openDetails",
        "post.exitDetails",
        "timeline.browse",
        "timeline.collect",
        "timeline.refreshNew",
        "timeline.browse",
    ]
    assert actions.calls[5][1]["homeFallback"] is True
    assert output["scrolls_completed"] == 2
    assert output["posts_seen"] == 1
    assert output["posts_opened"] == 1
    assert output["refresh_attempts"] == 1
    assert output["show_new_posts_clicked"] == 1


def test_browse_view_posts_migrates_legacy_scroll_ranges_and_marks_advanced_schema():
    params = browse_view_posts.Params.model_validate(
        {
            "scroll_interval_seconds_min": 3,
            "scroll_interval_seconds_max": 7,
            "scroll_distance_min": 400,
            "scroll_distance_max": 900,
        }
    )

    assert params.scroll_interval_seconds == 5
    assert params.scroll_interval_jitter_ratio == 0.4
    assert params.scroll_distance == 650
    assert round(params.scroll_distance_jitter_ratio, 3) == 0.385

    schema = browse_view_posts.Params.model_json_schema()
    assert "scroll_interval_seconds_min" not in schema["properties"]
    assert schema["properties"]["scroll_interval_jitter_ratio"]["advanced"] is True
    assert schema["properties"]["refresh_scroll_jitter_ratio"]["advanced"] is True


async def test_browse_view_posts_prioritizes_target_author_and_follows():
    class Timeline:
        def __init__(self, owner):
            self.owner = owner

        async def collect(self, payload, options=None):
            self.owner.calls.append(("timeline.collect", payload, options, {}))
            return {
                "status": "success",
                "data": {
                    "posts": [
                        {
                            "postId": "200",
                            "author": {"username": "target"},
                            "content": {"text": "target post"},
                            "isAd": False,
                        },
                        {
                            "postId": "201",
                            "author": {"username": "other"},
                            "content": {"text": "other post"},
                            "isAd": False,
                        },
                    ]
                },
            }

        async def browse(self, payload, options=None):
            self.owner.calls.append(("timeline.browse", payload, options, {}))
            return {"status": "success", "data": {"scrolls": 1, "atBoundary": False}}

    class Post:
        def __init__(self, owner):
            self.owner = owner

        async def openDetails(self, payload, options=None):
            self.owner.calls.append(("post.openDetails", payload, options, {}))
            return {"status": "navigating", "data": {"url": "https://x.com/target/status/200"}}

        async def exitDetails(self, payload, options=None):
            self.owner.calls.append(("post.exitDetails", payload, options, {}))
            return {"status": "success", "data": {"returned": True}}

    class Account:
        def __init__(self, owner):
            self.owner = owner

        async def followHandle(self, payload, options=None):
            self.owner.calls.append(("account.followHandle", payload, options, {}))
            return {"status": "success", "data": {"relationship": "following"}}

    actions = Actions()
    actions.timeline = Timeline(actions)
    actions.post = Post(actions)
    actions.account = Account(actions)
    ctx = context(actions=actions)
    params = browse_view_posts.Params(
        scroll_count=1,
        target_authors={"@target"},
        open_post_probability=0,
        min_scrolls_between_open=0,
        post_dwell_seconds_min=0,
        post_dwell_seconds_max=0,
        detail_scroll_probability=0,
        click_show_new_posts=False,
    )

    output = await browse_view_posts.run(ctx, params)

    assert [call[0] for call in actions.calls] == [
        "timeline.collect",
        "post.openDetails",
        "post.exitDetails",
        "account.followHandle",
        "timeline.browse",
    ]
    assert actions.calls[1][1] == {"tweetId": "200"}
    assert actions.calls[3][1] == {"handle": "target"}
    assert actions.calls[3][2]["confirmLive"] is True
    assert output["posts_opened"] == 1
    assert output["target_author_posts_opened"] == 1
    assert output["target_authors_followed"] == 1
    assert output["target_authors_follow_skipped"] == 0


async def test_search_authors_engage_follows_and_likes_visible_profile_posts():
    class AccountActions:
        def __init__(self, owner):
            self.owner = owner

        async def search(self, payload, options=None):
            self.owner.calls.append(("account.search", payload, options, {}))
            return {"status": "navigating", "data": {"url": "https://x.com/target"}}

        async def getDetails(self, payload, options=None):
            self.owner.calls.append(("account.getDetails", payload, options, {}))
            return {"status": "success", "data": {"account": {"username": "target"}}}

        async def follow(self, payload, options=None):
            self.owner.calls.append(("account.follow", payload, options, {}))
            return {"status": "success", "data": {"relationship": "following"}}

        async def listPosts(self, payload, options=None):
            self.owner.calls.append(("account.listPosts", payload, options, {}))
            return {
                "status": "success",
                "data": {
                    "posts": [
                        {"postId": "10", "author": {"username": "target"}, "content": {"text": "Python one"}},
                        {"postId": "11", "author": {"username": "target"}, "content": {"text": "Python two"}},
                    ]
                },
            }

    actions = Actions()
    actions.account = AccountActions(actions)
    ctx = context(actions=actions)
    params = search_authors_engage.Params(
        authors=["@target"],
        follow_authors=True,
        like_posts=True,
        keywords={"python"},
        max_posts_per_author=2,
        max_profile_scrolls=0,
        author_interval_seconds=0,
    )

    output = await search_authors_engage.run(ctx, params)

    assert [call[0] for call in actions.calls] == [
        "account.search",
        "account.getDetails",
        "account.follow",
        "account.listPosts",
        "interaction.like",
        "interaction.like",
    ]
    assert output == {
        "authors_requested": 1,
        "authors_processed": 1,
        "authors_skipped": 0,
        "followed": 1,
        "follow_skipped": 0,
        "posts_seen": 2,
        "posts_matched": 2,
        "liked": 2,
        "like_skipped": 0,
        "replied": 0,
        "reply_skipped": 0,
    }


def test_search_authors_params_normalize_and_deduplicate_handles():
    params = search_authors_engage.Params(authors=["@Target", "target"], follow_authors=True)
    assert params.authors == ["Target"]


async def test_timeline_collect_timeout_retries_before_processing_batch():
    class Timeline:
        def __init__(self):
            self.collect_calls = 0

        async def collect(self, _payload, options=None):
            self.collect_calls += 1
            if self.collect_calls == 1:
                raise ActionError("TIMEOUT", "slow timeline", retryable=True)
            return {
                "status": "success",
                "data": {"posts": [{"postId": "1", "content": {"text": "ready"}}]},
            }

    timeline = Timeline()
    ctx = context(actions=SimpleNamespace(timeline=timeline))

    async def stop_after_ready(posts):
        assert [post["postId"] for post in posts] == ["1"]
        return True

    result = await run_timeline_batches(
        ctx,
        feed="for_you",
        scroll_count=1,
        interval_seconds=1,
        distance=650,
        handle_batch=stop_after_ready,
        collect_timeout_seconds=30,
        collect_retry_count=1,
    )

    assert timeline.collect_calls == 2
    assert result["stopReason"] == "program_limit"


async def test_timeline_scroll_stall_retries_then_recovers():
    class Timeline:
        def __init__(self):
            self.collect_calls = 0
            self.browse_calls = 0

        async def collect(self, _payload, options=None):
            self.collect_calls += 1
            return {
                "status": "success",
                "data": {
                    "posts": [
                        {"postId": str(self.collect_calls), "content": {"text": "ready"}}
                    ]
                },
            }

        async def browse(self, _payload, options=None):
            self.browse_calls += 1
            if self.browse_calls < 3:
                return {
                    "status": "success",
                    "data": {"scrolls": 0, "atBoundary": False, "startY": 0, "endY": 0},
                }
            return {
                "status": "success",
                "data": {"scrolls": 1, "atBoundary": False, "startY": 0, "endY": 650},
            }

    timeline = Timeline()
    ctx = context(actions=SimpleNamespace(timeline=timeline))
    batches = 0

    async def stop_on_second_batch(_posts):
        nonlocal batches
        batches += 1
        return batches == 2

    result = await run_timeline_batches(
        ctx,
        feed="for_you",
        scroll_count=1,
        interval_seconds=1,
        distance=650,
        handle_batch=stop_on_second_batch,
        stalled_scroll_retry_count=3,
    )

    assert timeline.browse_calls == 3
    assert result["scrolls"] == 1
    assert result["stopReason"] == "program_limit"


async def test_timeline_scroll_stall_is_not_reported_as_success():
    class Timeline:
        async def collect(self, _payload, options=None):
            return {
                "status": "success",
                "data": {"posts": [{"postId": "1", "content": {"text": "ready"}}]},
            }

        async def browse(self, _payload, options=None):
            return {
                "status": "success",
                "data": {"scrolls": 0, "atBoundary": False, "startY": 0, "endY": 0},
            }

    ctx = context(actions=SimpleNamespace(timeline=Timeline()))

    async def continue_task(_posts):
        return False

    with pytest.raises(ActionError) as caught:
        await run_timeline_batches(
            ctx,
            feed="for_you",
            scroll_count=1,
            interval_seconds=1,
            distance=650,
            handle_batch=continue_task,
            stalled_scroll_retry_count=2,
        )

    assert caught.value.code == "TIMELINE_SCROLL_STALLED"


def test_existing_engage_params_receive_backward_compatible_defaults():
    params = browse_match_engage.Params.model_validate(
        {
            "feed": "for_you",
            "scroll_count": 10,
            "scroll_interval_seconds": 5.25,
            "scroll_distance": 650,
            "target_authors": [],
            "keywords": ["AI"],
            "like": True,
            "reply_mode": "none",
            "max_engagements": 3,
        }
    )

    assert params.timeline_ready_timeout_seconds == 30
    assert params.collect_timeout_seconds == 30
    assert params.collect_retry_count == 2
    assert params.stalled_scroll_retry_count == 3
    assert params.interaction_timeout_seconds == 30
    assert params.follow_authors is False
    assert params.max_follows == 10


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
            },
            task_run_id="run",
        )


def test_cancelled_action_preserves_real_task_run_id():
    with pytest.raises(TaskCancelledError) as caught:
        require_certain({"status": "cancelled"}, task_run_id="run-123")

    assert caught.value.task_run_id == "run-123"
    assert "run-123" in str(caught.value)


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


async def test_browse_view_posts_skips_posts_that_leave_dom():
    class Timeline:
        async def collect(self, _payload, options=None):
            return {
                "status": "success",
                "data": {
                    "posts": [
                        {"postId": "123", "author": {"username": "target"}, "content": {"text": "hello"}},
                    ],
                },
            }

        async def browse(self, _payload, options=None):
            return {
                "status": "success",
                "data": {"scrolls": 1, "atBoundary": False},
            }

    class Post:
        async def openDetails(self, _payload, options=None):
            raise ActionError("TARGET_NOT_FOUND", "Post 123 is not in the current DOM.")

    ctx = context(actions=SimpleNamespace(timeline=Timeline(), post=Post()))
    params = browse_view_posts.Params(
        scroll_count=1,
        open_post_probability=1,
        min_scrolls_between_open=0,
        scroll_interval_seconds=0.25,
        scroll_interval_jitter_ratio=0,
        click_show_new_posts=False,
    )

    result = await browse_view_posts.run(ctx, params)

    assert result["scrolls_completed"] == 1
    assert result["posts_seen"] == 1
    assert result["posts_opened"] == 0
    assert result["stop_reason"] == "scroll_limit"


async def test_login_accounts_uses_secret_refs_and_deletes_on_success(monkeypatch):
    class Store:
        def __init__(self):
            self.values = {"password-ref": "pw", "totp-ref": "SECRET"}
            self.deleted = []

        def get(self, reference):
            return self.values.get(reference)

        def delete(self, reference):
            self.deleted.append(reference)

    store = Store()
    monkeypatch.setattr(login_accounts.login_secrets, "store", lambda: store)

    class Account:
        async def login(self, payload=None, options=None, **kwargs):
            actions.calls.append(("account.login", payload, options, kwargs))
            return {
                "status": "success",
                "data": {"session": {"loggedIn": True, "username": "target"}},
            }

    actions = Actions()
    actions.account = Account()
    ctx = context(actions=actions)
    params = login_accounts.Params(
        credentials_by_account={
            "account-1": {
                "username": "target",
                "password_ref": "password-ref",
                "totp_secret_ref": "totp-ref",
            }
        }
    )

    output = await login_accounts.run(ctx, params)

    assert actions.calls[0][0] == "account.login"
    payload = actions.calls[0][1]
    assert payload["password"] == "pw"
    assert payload["totpSecret"] == "SECRET"
    assert "password" not in output
    assert output["logged_in"] is True
    assert store.deleted == ["password-ref", "totp-ref"]
