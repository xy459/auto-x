from __future__ import annotations

import asyncio
import random
from typing import Any, Literal, Self

from pydantic import BaseModel, Field, model_validator
from x_actions_playwright.errors import ActionError

from ..task_sdk import TaskContext
from ._common import (
    action_options,
    feed_value,
    jitter_int,
    jitter_number,
    post_id,
    posts_from,
    require_certain,
    result_data,
    result_status,
    write_options,
)
from .spec import ProgramSpec

SPEC = ProgramSpec(
    name="browse_view_posts",
    version="1.0.0",
    title="浏览并查看帖子",
    description="浏览时间线，低频穿插打开帖子详情、返回，并按策略刷新新帖子。",
)


class Params(BaseModel):
    feed: Literal["for_you", "following"] = "for_you"
    scroll_count: int = Field(default=30, ge=1, le=300)
    scroll_interval_seconds: float = Field(default=5, ge=0.25, le=60)
    scroll_distance: int = Field(default=650, ge=200, le=3000)
    collect_visible_posts: int = Field(default=12, ge=1, le=50)
    target_authors: set[str] = Field(default_factory=set)
    follow_target_authors: bool = True
    max_target_author_follows: int = Field(default=10, ge=0, le=100)
    open_post_enabled: bool = True
    open_post_probability: float = Field(default=0.18, ge=0, le=1)
    min_scrolls_between_open: int = Field(default=3, ge=0, le=100)
    max_posts_opened: int = Field(default=6, ge=0, le=100)
    post_dwell_seconds_min: float = Field(default=8, ge=0, le=300)
    post_dwell_seconds_max: float = Field(default=25, ge=0, le=600)
    detail_scroll_probability: float = Field(default=0.45, ge=0, le=1)
    detail_scroll_count_max: int = Field(default=2, ge=0, le=10)
    click_show_new_posts: bool = True
    refresh_strategy: Literal["tab_first", "home_show", "none"] = "tab_first"
    refresh_top_every_scrolls: int = Field(default=12, ge=0, le=300)
    refresh_top_every_seconds: float = Field(default=240, ge=0, le=7200)
    home_show_every_refreshes: int = Field(default=3, ge=0, le=50)
    action_timeout_ms: int = Field(default=30000, ge=1000, le=120000)
    follow_timeout_seconds: float = Field(default=30, ge=10, le=120)
    scroll_interval_jitter_ratio: float = Field(
        default=0.2,
        ge=0,
        le=1,
        json_schema_extra={"advanced": True},
    )
    scroll_distance_jitter_ratio: float = Field(
        default=0.15,
        ge=0,
        le=1,
        json_schema_extra={"advanced": True},
    )
    refresh_scroll_jitter_ratio: float = Field(
        default=0.25,
        ge=0,
        le=1,
        json_schema_extra={"advanced": True},
    )
    refresh_time_jitter_ratio: float = Field(
        default=0.25,
        ge=0,
        le=1,
        json_schema_extra={"advanced": True},
    )
    home_show_refresh_jitter: int = Field(
        default=1,
        ge=0,
        le=10,
        json_schema_extra={"advanced": True},
    )

    @model_validator(mode="before")
    @classmethod
    def migrate_legacy_ranges(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        values = dict(data)
        if "scroll_interval_seconds" not in values:
            low = values.get("scroll_interval_seconds_min")
            high = values.get("scroll_interval_seconds_max")
            if low is not None and high is not None:
                base = (float(low) + float(high)) / 2
                values["scroll_interval_seconds"] = base
                if base > 0 and "scroll_interval_jitter_ratio" not in values:
                    values["scroll_interval_jitter_ratio"] = abs(float(high) - float(low)) / 2 / base
        if "scroll_distance" not in values:
            low = values.get("scroll_distance_min")
            high = values.get("scroll_distance_max")
            if low is not None and high is not None:
                base = round((int(low) + int(high)) / 2)
                values["scroll_distance"] = base
                if base > 0 and "scroll_distance_jitter_ratio" not in values:
                    values["scroll_distance_jitter_ratio"] = abs(int(high) - int(low)) / 2 / base
        return values

    @model_validator(mode="after")
    def clamp_migrated_jitter(self) -> Self:
        self.scroll_interval_jitter_ratio = min(1.0, max(0.0, self.scroll_interval_jitter_ratio))
        self.scroll_distance_jitter_ratio = min(1.0, max(0.0, self.scroll_distance_jitter_ratio))
        return self


def _range_number(low: float, high: float) -> float:
    start = min(low, high)
    end = max(low, high)
    return random.uniform(start, end)


def _range_int(low: int, high: int) -> int:
    start = min(low, high)
    end = max(low, high)
    return random.randint(start, end)


def _next_refresh_scroll_target(params: Params) -> int:
    if params.refresh_top_every_scrolls <= 0:
        return 0
    return jitter_int(
        params.refresh_top_every_scrolls,
        params.refresh_scroll_jitter_ratio,
        minimum=1,
        maximum=300,
    )


def _next_refresh_time_target(params: Params) -> float:
    if params.refresh_top_every_seconds <= 0:
        return 0
    return jitter_number(
        params.refresh_top_every_seconds,
        params.refresh_time_jitter_ratio,
        minimum=1,
        maximum=7200,
    )


def _next_home_show_target(params: Params) -> int:
    if params.home_show_every_refreshes <= 0:
        return 0
    low = max(1, params.home_show_every_refreshes - params.home_show_refresh_jitter)
    high = min(50, params.home_show_every_refreshes + params.home_show_refresh_jitter)
    return random.randint(low, high)


def _candidate_posts(posts: list[dict[str, Any]], opened: set[str]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for post in posts:
        identity = post_id(post)
        if not identity or identity in opened or post.get("isAd"):
            continue
        result.append(post)
    return result


def _author_username(post: dict[str, Any]) -> str:
    author = post.get("author") or {}
    if isinstance(author, dict):
        value = author.get("username") or author.get("handle") or author.get("id") or ""
    else:
        value = author
    return str(value).lstrip("@").strip()


def _target_author_posts(
    posts: list[dict[str, Any]],
    opened: set[str],
    target_authors: set[str],
) -> list[dict[str, Any]]:
    wanted = {author.lstrip("@").casefold() for author in target_authors if author}
    if not wanted:
        return []
    return [
        post
        for post in _candidate_posts(posts, opened)
        if _author_username(post).casefold() in wanted
    ]


async def _collect_visible(context: TaskContext, params: Params) -> list[dict[str, Any]]:
    result = await context.actions.timeline.collect(
        {
            "feed": feed_value(params.feed),
            "maxScrolls": 0,
            "durationMs": 0,
            "maxPosts": params.collect_visible_posts,
            "includeAds": False,
        },
        options=action_options(context, params.action_timeout_ms),
    )
    if result_status(result) == "navigating":
        await context.cancellation.sleep(0.75)
        result = await context.actions.timeline.collect(
            {
                "feed": feed_value(params.feed),
                "maxScrolls": 0,
                "durationMs": 0,
                "maxPosts": params.collect_visible_posts,
                "includeAds": False,
            },
            options=action_options(context, params.action_timeout_ms),
        )
    require_certain(result, task_run_id=context.cancellation.task_run_id)
    return posts_from(result)


async def _open_and_view_post(
    context: TaskContext,
    params: Params,
    *,
    tweet_id: str,
) -> bool:
    try:
        result = await context.actions.post.openDetails(
            {"tweetId": tweet_id},
            options=action_options(context, params.action_timeout_ms),
        )
        if result_status(result) not in {"success", "skipped", "navigating"}:
            require_certain(result, task_run_id=context.cancellation.task_run_id)
    except ActionError as error:
        if error.code != "TARGET_NOT_FOUND":
            raise
        context.logger.warning(
            "跳过已不在当前页面的帖子",
            tweet_id=tweet_id,
            error_code=error.code,
            error_message=error.message,
        )
        return False
    dwell = _range_number(params.post_dwell_seconds_min, params.post_dwell_seconds_max)
    context.logger.info("查看帖子详情", tweet_id=tweet_id, dwell_seconds=round(dwell, 1))
    await context.cancellation.sleep(dwell)

    if params.detail_scroll_count_max and random.random() < params.detail_scroll_probability:
        for _ in range(random.randint(1, params.detail_scroll_count_max)):
            await context.cancellation.raise_if_cancelled()
            distance = _range_int(250, 850)
            try:
                scroll = await context.actions.browse.scrollComments(
                    {"distance": distance},
                    options=action_options(context, 10_000),
                )
                require_certain(scroll, task_run_id=context.cancellation.task_run_id)
            except RuntimeError:
                break
            await context.cancellation.sleep(_range_number(1.0, 3.5))

    exit_result = await context.actions.post.exitDetails(
        {},
        options=action_options(context, params.action_timeout_ms),
    )
    require_certain(exit_result, task_run_id=context.cancellation.task_run_id)
    return True


async def _follow_target_author(
    context: TaskContext,
    params: Params,
    *,
    username: str,
) -> str:
    normalized_author = username.casefold()
    own_username = str(getattr(context.account, "username", None) or "").lstrip("@").casefold()
    if not normalized_author or normalized_author == own_username:
        context.logger.info("跳过关注当前登录账号", author=username)
        return "skipped"
    try:
        result = require_certain(
            await context.actions.account.followHandle(
                {"handle": username},
                options=write_options(
                    context,
                    f"view-post-follow:{context.account.account_id}:{normalized_author}",
                    timeout_ms=int(params.follow_timeout_seconds * 1_000),
                ),
            ),
            task_run_id=context.cancellation.task_run_id,
        )
    except ActionError as error:
        if error.code not in {
            "TARGET_UNSAFE",
            "ACCOUNT_NOT_FOUND",
            "ACCOUNT_SUSPENDED",
            "ACCOUNT_TEMPORARILY_RESTRICTED",
        }:
            raise
        context.logger.warning(
            "跳过无法关注的目标作者",
            author=username,
            error_code=error.code,
            error_message=error.message,
        )
        return "skipped"
    return "followed" if result_status(result) == "success" else "skipped"


async def _maybe_refresh(
    context: TaskContext,
    params: Params,
    *,
    scrolls_completed: int,
    last_refresh_scroll: int,
    last_refresh_at: float,
    next_refresh_scrolls: int,
    next_refresh_seconds: float,
    next_home_show_refresh: int,
    refresh_attempts: int,
    loop_time: float,
) -> tuple[bool, int, float, int, int, int, float, bool]:
    if not params.click_show_new_posts or params.refresh_strategy == "none":
        return (
            False,
            last_refresh_scroll,
            last_refresh_at,
            refresh_attempts,
            next_home_show_refresh,
            next_refresh_scrolls,
            next_refresh_seconds,
            False,
        )
    due_by_scroll = (
        next_refresh_scrolls > 0
        and scrolls_completed - last_refresh_scroll >= next_refresh_scrolls
    )
    due_by_time = (
        next_refresh_seconds > 0
        and loop_time - last_refresh_at >= next_refresh_seconds
    )
    if not due_by_scroll and not due_by_time:
        return (
            False,
            last_refresh_scroll,
            last_refresh_at,
            refresh_attempts,
            next_home_show_refresh,
            next_refresh_scrolls,
            next_refresh_seconds,
            False,
        )

    refresh_attempts += 1
    home_fallback = bool(next_home_show_refresh and refresh_attempts >= next_home_show_refresh)
    result = await context.actions.timeline.refreshNew(
        {
            "feed": feed_value(params.feed),
            "strategy": params.refresh_strategy,
            "homeFallback": home_fallback,
            "settleMs": _range_int(2000, 5000),
        },
        options=action_options(context, params.action_timeout_ms),
    )
    if result_status(result) == "navigating":
        await context.cancellation.sleep(1.0)
        result = await context.actions.timeline.refreshNew(
            {
                "feed": feed_value(params.feed),
                "strategy": params.refresh_strategy,
                "homeFallback": home_fallback,
                "settleMs": _range_int(2000, 5000),
            },
            options=action_options(context, params.action_timeout_ms),
        )
    require_certain(result, task_run_id=context.cancellation.task_run_id)
    data = result_data(result)
    clicked = bool(data.get("clickedShowNewPosts"))
    context.logger.info(
        "刷新新帖子尝试完成",
        strategy=params.refresh_strategy,
        home_fallback=home_fallback,
        clicked_show=clicked,
    )
    next_home_show_refresh = (
        refresh_attempts + _next_home_show_target(params)
        if home_fallback
        else next_home_show_refresh
    )
    return (
        True,
        scrolls_completed,
        loop_time,
        refresh_attempts,
        next_home_show_refresh,
        _next_refresh_scroll_target(params),
        _next_refresh_time_target(params),
        clicked,
    )


async def run(context: TaskContext, params: Params) -> dict[str, Any]:
    await context.cancellation.raise_if_cancelled()
    context.logger.info(
        "开始浏览并查看帖子",
        feed=params.feed,
        scroll_count=params.scroll_count,
        open_probability=params.open_post_probability,
        refresh_strategy=params.refresh_strategy,
    )

    opened_posts: set[str] = set()
    posts_seen: set[str] = set()
    scrolls_completed = 0
    posts_opened = 0
    target_author_posts_opened = 0
    target_authors_followed = 0
    target_authors_follow_skipped = 0
    refresh_attempts = 0
    show_new_posts_clicked = 0
    last_open_scroll = -params.min_scrolls_between_open
    followed_authors: set[str] = set()
    loop = asyncio.get_running_loop()
    last_refresh_at = loop.time()
    last_refresh_scroll = 0
    next_refresh_scrolls = _next_refresh_scroll_target(params)
    next_refresh_seconds = _next_refresh_time_target(params)
    next_home_show_refresh = _next_home_show_target(params)
    stop_reason = "scroll_limit"

    while scrolls_completed < params.scroll_count:
        await context.cancellation.raise_if_cancelled()
        visible_posts = await _collect_visible(context, params)
        for post in visible_posts:
            identity = post_id(post)
            if identity:
                posts_seen.add(identity)

        can_open = (
            params.open_post_enabled
            and posts_opened < params.max_posts_opened
            and scrolls_completed - last_open_scroll >= params.min_scrolls_between_open
        )
        target_candidates = _target_author_posts(visible_posts, opened_posts, params.target_authors)
        selected_target = bool(can_open and target_candidates)
        if selected_target:
            post = target_candidates[0]
        else:
            candidates = _candidate_posts(visible_posts, opened_posts)
            post = (
                random.choice(candidates)
                if can_open and candidates and random.random() < params.open_post_probability
                else None
            )
        if post:
            identity = post_id(post)
            if identity:
                opened_posts.add(identity)
                if await _open_and_view_post(context, params, tweet_id=identity):
                    posts_opened += 1
                    last_open_scroll = scrolls_completed
                    if selected_target:
                        target_author_posts_opened += 1
                        username = _author_username(post)
                        normalized = username.casefold()
                        if (
                            params.follow_target_authors
                            and target_authors_followed < params.max_target_author_follows
                            and normalized
                            and normalized not in followed_authors
                        ):
                            followed_authors.add(normalized)
                            follow_status = await _follow_target_author(
                                context,
                                params,
                                username=username,
                            )
                            if follow_status == "followed":
                                target_authors_followed += 1
                            else:
                                target_authors_follow_skipped += 1

        (
            refreshed,
            last_refresh_scroll,
            last_refresh_at,
            refresh_attempts,
            next_home_show_refresh,
            next_refresh_scrolls,
            next_refresh_seconds,
            clicked,
        ) = await _maybe_refresh(
            context,
            params,
            scrolls_completed=scrolls_completed,
            last_refresh_scroll=last_refresh_scroll,
            last_refresh_at=last_refresh_at,
            next_refresh_scrolls=next_refresh_scrolls,
            next_refresh_seconds=next_refresh_seconds,
            next_home_show_refresh=next_home_show_refresh,
            refresh_attempts=refresh_attempts,
            loop_time=loop.time(),
        )
        if refreshed and clicked:
            show_new_posts_clicked += 1

        distance = jitter_int(
            params.scroll_distance,
            params.scroll_distance_jitter_ratio,
            minimum=200,
            maximum=3000,
        )
        interval = jitter_number(
            params.scroll_interval_seconds,
            params.scroll_interval_jitter_ratio,
            minimum=0.25,
            maximum=60,
        )
        browse = await context.actions.timeline.browse(
            {
                "feed": feed_value(params.feed),
                "maxScrolls": 1,
                "durationMs": int(interval * 1000 + 800),
                "intervalMs": int(interval * 1000),
                "distance": distance,
            },
            options=action_options(context, max(params.action_timeout_ms, int(interval * 1000 + 5000))),
        )
        if result_status(browse) == "navigating":
            await context.cancellation.sleep(0.75)
            continue
        require_certain(browse, task_run_id=context.cancellation.task_run_id)
        data = result_data(browse)
        actual_scrolls = int(data.get("scrolls") or 0)
        if actual_scrolls <= 0:
            stop_reason = "timeline_boundary"
            break
        scrolls_completed += actual_scrolls
        if data.get("atBoundary"):
            stop_reason = "timeline_boundary"
            break

    context.logger.info(
        "浏览并查看帖子完成",
        scrolls_completed=scrolls_completed,
        posts_seen=len(posts_seen),
        posts_opened=posts_opened,
        target_author_posts_opened=target_author_posts_opened,
        target_authors_followed=target_authors_followed,
        refresh_attempts=refresh_attempts,
    )
    return {
        "feed": params.feed,
        "scrolls_completed": scrolls_completed,
        "posts_seen": len(posts_seen),
        "posts_opened": posts_opened,
        "target_author_posts_opened": target_author_posts_opened,
        "target_authors_followed": target_authors_followed,
        "target_authors_follow_skipped": target_authors_follow_skipped,
        "refresh_attempts": refresh_attempts,
        "show_new_posts_clicked": show_new_posts_clicked,
        "stop_reason": stop_reason,
    }
