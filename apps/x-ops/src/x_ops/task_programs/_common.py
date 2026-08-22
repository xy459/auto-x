from __future__ import annotations

import asyncio
import random
from collections.abc import Awaitable, Callable, Iterable, Mapping
from typing import Any

from x_actions_playwright.errors import ActionError

from ..task_sdk.context import TaskContext
from ..task_sdk.errors import TaskCancelledError, TaskUncertainError

DEFAULT_TIMELINE_READY_TIMEOUT_SECONDS = 30.0
DEFAULT_COLLECT_TIMEOUT_SECONDS = 30.0
DEFAULT_COLLECT_RETRY_COUNT = 2
DEFAULT_COLLECT_RETRY_INTERVAL_SECONDS = 1.5
DEFAULT_STALLED_SCROLL_RETRY_COUNT = 3
DEFAULT_STALLED_SCROLL_RETRY_INTERVAL_SECONDS = 2.0


def feed_value(value: str) -> str:
    return value.replace("_", "-")


def result_status(result: Any) -> str:
    if isinstance(result, Mapping):
        return str(result.get("status", "success"))
    return str(getattr(result, "status", "success"))


def result_data(result: Any) -> dict[str, Any]:
    if isinstance(result, Mapping):
        data = result.get("data", result)
    else:
        data = getattr(result, "data", {})
    return dict(data) if isinstance(data, Mapping) else {}


def require_certain(result: Any, *, task_run_id: str) -> Any:
    status = result_status(result)
    data = result_data(result)
    previous = data.get("previous")
    previous_state = previous.get("state") if isinstance(previous, Mapping) else None
    if status == "uncertain" or (status == "skipped" and previous_state in {"pending", "uncertain"}):
        action = getattr(result, "action", None)
        details = result.to_dict() if hasattr(result, "to_dict") else data
        raise TaskUncertainError(action_id=action, details=details)
    if status == "cancelled":
        raise TaskCancelledError(task_run_id)
    if status in {"failed", "navigating"}:
        raise RuntimeError(f"Action did not complete: {status}")
    return result


def posts_from(result: Any) -> list[dict[str, Any]]:
    posts = result_data(result).get("posts", [])
    return [dict(post) for post in posts if isinstance(post, Mapping)]


def matches_post(post: Mapping[str, Any], authors: Iterable[str], keywords: Iterable[str]) -> bool:
    wanted_authors = {value.lstrip("@").casefold() for value in authors if value}
    author = post.get("author") or {}
    if isinstance(author, Mapping):
        actual_author = str(author.get("id") or author.get("username") or "").lstrip("@").casefold()
    else:
        actual_author = str(author).lstrip("@").casefold()
    content = post.get("content") or {}
    text = str(content.get("text", "") if isinstance(content, Mapping) else content).casefold()
    wanted_keywords = {value.casefold() for value in keywords if value}
    author_ok = not wanted_authors or actual_author in wanted_authors
    keyword_ok = not wanted_keywords or any(keyword in text for keyword in wanted_keywords)
    return author_ok and keyword_ok


def post_id(post: Mapping[str, Any]) -> str | None:
    value = post.get("postId") or post.get("tweetId") or post.get("id")
    return str(value) if value is not None else None


def post_text(post: Mapping[str, Any]) -> str:
    content = post.get("content") or {}
    return str(content.get("text", "") if isinstance(content, Mapping) else content)


def jitter_number(base: float, ratio: float, *, minimum: float, maximum: float) -> float:
    spread = max(0.0, base * ratio)
    low = max(minimum, base - spread)
    high = min(maximum, base + spread)
    return random.uniform(min(low, high), max(low, high))


def jitter_int(base: int, ratio: float, *, minimum: int, maximum: int) -> int:
    return int(round(jitter_number(float(base), ratio, minimum=minimum, maximum=maximum)))


def action_options(context: TaskContext, timeout_ms: int) -> dict[str, Any]:
    return {
        "timeoutMs": timeout_ms,
        "cancellation": context.cancellation,
    }


def write_options(
    context: TaskContext,
    run_key: str,
    timeout_ms: int | None = None,
) -> dict[str, Any]:
    options: dict[str, Any] = {
        "confirmLive": True,
        "idempotencyKey": run_key,
        "accountScope": context.account.account_id,
        "cancellation": context.cancellation,
    }
    if timeout_ms is not None:
        options["timeoutMs"] = timeout_ms
    return options


async def _call_with_navigation_retry(
    context: TaskContext,
    method: Any,
    payload: dict[str, Any],
    timeout_ms: int,
) -> Any:
    options = action_options(context, timeout_ms)
    result = await method(payload, options=options)
    if result_status(result) == "navigating":
        # A domcontentloaded navigation can complete before X mounts the home
        # tabs and virtualized timeline. Give the React application one turn to
        # hydrate, then repeat the same safe read/browse action.
        await context.cancellation.sleep(0.5)
        result = await method(payload, options=options)
    return require_certain(result, task_run_id=context.cancellation.task_run_id)


async def _collect_with_retries(
    context: TaskContext,
    *,
    feed: str,
    max_posts: int,
    include_ads: bool,
    timeout_seconds: float,
    retry_count: int,
    retry_interval_seconds: float = DEFAULT_COLLECT_RETRY_INTERVAL_SECONDS,
) -> Any:
    payload = {
        "feed": feed_value(feed),
        "maxScrolls": 0,
        "durationMs": 0,
        "maxPosts": max_posts,
        "includeAds": include_ads,
    }
    timeout_ms = max(1_000, int(timeout_seconds * 1_000))
    for attempt in range(retry_count + 1):
        try:
            return await _call_with_navigation_retry(
                context,
                context.actions.timeline.collect,
                payload,
                timeout_ms,
            )
        except ActionError as exc:
            if exc.code != "TIMEOUT" or attempt >= retry_count:
                raise
            context.logger.warning(
                "时间线收集超时，准备重试",
                attempt=attempt + 1,
                max_retries=retry_count,
                timeout_seconds=timeout_seconds,
            )
            await context.cancellation.sleep(retry_interval_seconds)
    raise AssertionError("unreachable")


async def _wait_for_timeline_ready(
    context: TaskContext,
    *,
    feed: str,
    timeout_seconds: float,
    collect_timeout_seconds: float,
    collect_retry_count: int,
    max_posts: int,
    include_ads: bool,
) -> Any:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_seconds
    attempts = 0
    last_result: Any | None = None
    while True:
        await context.cancellation.raise_if_cancelled()
        remaining = deadline - loop.time()
        if remaining <= 0:
            raise ActionError(
                "TIMELINE_NOT_READY",
                f"X timeline did not expose any posts within {timeout_seconds:g} seconds.",
                {"feed": feed, "attempts": attempts},
                retryable=True,
            )
        attempts += 1
        try:
            per_attempt_timeout = min(
                collect_timeout_seconds,
                max(1.0, remaining / (collect_retry_count + 1)),
            )
            last_result = await _collect_with_retries(
                context,
                feed=feed,
                max_posts=max_posts,
                include_ads=include_ads,
                timeout_seconds=per_attempt_timeout,
                retry_count=collect_retry_count,
            )
        except ActionError as exc:
            if exc.code != "TIMEOUT":
                raise
            if loop.time() >= deadline:
                raise ActionError(
                    "TIMELINE_NOT_READY",
                    f"X timeline was not ready within {timeout_seconds:g} seconds.",
                    {"feed": feed, "attempts": attempts, "lastError": exc.to_dict()},
                    retryable=True,
                ) from exc
            context.logger.warning(
                "时间线尚未就绪，继续等待",
                feed=feed,
                attempt=attempts,
                reason=exc.code,
            )
        else:
            posts = posts_from(last_result)
            if posts:
                context.logger.info(
                    "时间线已就绪",
                    feed=feed,
                    visible_posts=len(posts),
                    attempts=attempts,
                )
                return last_result
            context.logger.warning(
                "时间线暂无可识别帖子，继续等待",
                feed=feed,
                attempt=attempts,
                url=result_data(last_result).get("url"),
            )
        remaining = deadline - loop.time()
        if remaining <= 0:
            continue
        await context.cancellation.sleep(min(DEFAULT_COLLECT_RETRY_INTERVAL_SECONDS, remaining))


async def run_timeline(
    context: TaskContext,
    *,
    feed: str,
    scroll_count: int,
    interval_seconds: float,
    distance: int = 650,
    collect: bool = False,
    max_posts: int = 500,
    timeline_ready_timeout_seconds: float = DEFAULT_TIMELINE_READY_TIMEOUT_SECONDS,
    stalled_scroll_retry_count: int = DEFAULT_STALLED_SCROLL_RETRY_COUNT,
) -> dict[str, Any]:
    """Run a long timeline operation as cancellable, bounded action chunks."""
    interval_ms = max(250, int(interval_seconds * 1000))
    remaining_scrolls = scroll_count
    completed_scrolls = 0
    collected: dict[str, dict[str, Any]] = {}
    at_boundary = False
    stop_reason = "scroll_limit"
    stalled_attempts = 0
    method = context.actions.timeline.collect if collect else context.actions.timeline.browse

    if not collect:
        await _wait_for_timeline_ready(
            context,
            feed=feed,
            timeout_seconds=timeline_ready_timeout_seconds,
            collect_timeout_seconds=timeline_ready_timeout_seconds,
            collect_retry_count=0,
            max_posts=1,
            include_ads=True,
        )

    while remaining_scrolls > 0 and not at_boundary:
        await context.cancellation.raise_if_cancelled()
        # Keep one atomic action below the x-actions 60-second browsing bound.
        chunk_limit = max(1, 45_000 // (interval_ms + 500))
        chunk_scrolls = min(remaining_scrolls, chunk_limit)
        duration_ms = min(55_000, chunk_scrolls * (interval_ms + 500) + 1_000)
        payload = {
            "feed": feed_value(feed),
            "maxScrolls": chunk_scrolls,
            "durationMs": duration_ms,
            "intervalMs": interval_ms,
            "distance": distance,
        }
        if collect:
            payload.update({"maxPosts": max_posts, "includeAds": False})
        result = await _call_with_navigation_retry(
            context,
            method,
            payload,
            duration_ms + 4_000,
        )
        data = result_data(result)

        if collect:
            for post in data.get("posts", []):
                if not isinstance(post, Mapping):
                    continue
                identity = post_id(post)
                if identity and identity not in collected:
                    collected[identity] = dict(post)
                    if len(collected) >= max_posts:
                        break

        actual_scrolls = max(0, int(data.get("scrolls", 0)))
        completed_scrolls += actual_scrolls
        remaining_scrolls -= actual_scrolls
        at_boundary = bool(data.get("atBoundary"))
        context.logger.info(
            "时间线滚动完成",
            requested_scrolls=chunk_scrolls,
            actual_scrolls=actual_scrolls,
            completed_scrolls=completed_scrolls,
            remaining_scrolls=remaining_scrolls,
            start_y=data.get("startY"),
            end_y=data.get("endY"),
            at_boundary=at_boundary,
        )
        if len(collected) >= max_posts:
            stop_reason = "post_limit"
            break
        if at_boundary:
            stop_reason = "timeline_boundary"
            break
        if actual_scrolls == 0:
            stalled_attempts += 1
            if stalled_attempts > stalled_scroll_retry_count:
                raise ActionError(
                    "TIMELINE_SCROLL_STALLED",
                    "X timeline did not move after repeated scroll attempts.",
                    {
                        "feed": feed,
                        "attempts": stalled_attempts,
                        "startY": data.get("startY"),
                        "endY": data.get("endY"),
                    },
                    retryable=True,
                )
            context.logger.warning(
                "时间线未产生有效滚动，准备重试",
                attempt=stalled_attempts,
                max_retries=stalled_scroll_retry_count,
            )
            await context.cancellation.sleep(DEFAULT_STALLED_SCROLL_RETRY_INTERVAL_SECONDS)
            continue
        stalled_attempts = 0

    return {
        "status": "success",
        "scrolls": completed_scrolls,
        "posts": list(collected.values()),
        "atBoundary": at_boundary,
        "stopReason": stop_reason,
    }


async def run_timeline_batches(
    context: TaskContext,
    *,
    feed: str,
    scroll_count: int,
    interval_seconds: float,
    distance: int,
    handle_batch: Callable[[list[dict[str, Any]]], Awaitable[bool]],
    max_posts_per_batch: int = 100,
    include_ads: bool = False,
    timeline_ready_timeout_seconds: float = DEFAULT_TIMELINE_READY_TIMEOUT_SECONDS,
    collect_timeout_seconds: float = DEFAULT_COLLECT_TIMEOUT_SECONDS,
    collect_retry_count: int = DEFAULT_COLLECT_RETRY_COUNT,
    stalled_scroll_retry_count: int = DEFAULT_STALLED_SCROLL_RETRY_COUNT,
) -> dict[str, Any]:
    """Collect the currently rendered batch, handle it, then scroll once.

    X virtualizes its timeline. Posts observed near the beginning of a long
    collect call may no longer exist in the DOM when that call returns. This
    helper deliberately performs interaction while each batch is still
    rendered, before advancing the timeline.

    ``handle_batch`` returns ``True`` when the task has reached its own stop
    condition. The helper still owns cancellation, navigation recovery,
    de-duplication and exact scroll accounting.
    """

    interval_ms = max(250, int(interval_seconds * 1000))
    remaining_scrolls = scroll_count
    completed_scrolls = 0
    seen: set[str] = set()
    at_boundary = False
    stop_reason = "scroll_limit"
    visible = await _wait_for_timeline_ready(
        context,
        feed=feed,
        timeout_seconds=timeline_ready_timeout_seconds,
        collect_timeout_seconds=collect_timeout_seconds,
        collect_retry_count=collect_retry_count,
        max_posts=max_posts_per_batch,
        include_ads=include_ads,
    )

    while True:
        await context.cancellation.raise_if_cancelled()
        batch: list[dict[str, Any]] = []
        for post in posts_from(visible):
            identity = post_id(post)
            if identity and identity not in seen:
                seen.add(identity)
                batch.append(post)

        context.logger.info(
            "时间线批次已收集",
            visible_posts=len(posts_from(visible)),
            new_posts=len(batch),
            posts_seen=len(seen),
            completed_scrolls=completed_scrolls,
            remaining_scrolls=remaining_scrolls,
            at_boundary=at_boundary,
        )

        if batch and await handle_batch(batch):
            stop_reason = "program_limit"
            break
        if remaining_scrolls <= 0:
            stop_reason = "scroll_limit"
            break
        if at_boundary:
            stop_reason = "timeline_boundary"
            break

        duration_ms = min(15_000, interval_ms + 2_000)
        stalled_attempts = 0
        while True:
            browsed = await _call_with_navigation_retry(
                context,
                context.actions.timeline.browse,
                {
                    "feed": feed_value(feed),
                    "maxScrolls": 1,
                    "durationMs": duration_ms,
                    "intervalMs": interval_ms,
                    "distance": distance,
                },
                duration_ms + 4_000,
            )
            data = result_data(browsed)
            actual_scrolls = max(0, int(data.get("scrolls", 0)))
            at_boundary = bool(data.get("atBoundary"))
            context.logger.info(
                "时间线滚动完成",
                requested_scrolls=1,
                actual_scrolls=actual_scrolls,
                completed_scrolls=completed_scrolls + actual_scrolls,
                remaining_scrolls=remaining_scrolls - actual_scrolls,
                start_y=data.get("startY"),
                end_y=data.get("endY"),
                at_boundary=at_boundary,
            )
            if actual_scrolls > 0 or at_boundary:
                break
            stalled_attempts += 1
            if stalled_attempts > stalled_scroll_retry_count:
                raise ActionError(
                    "TIMELINE_SCROLL_STALLED",
                    "X timeline did not move after repeated scroll attempts.",
                    {
                        "feed": feed,
                        "attempts": stalled_attempts,
                        "startY": data.get("startY"),
                        "endY": data.get("endY"),
                    },
                    retryable=True,
                )
            context.logger.warning(
                "时间线未产生有效滚动，准备重试",
                attempt=stalled_attempts,
                max_retries=stalled_scroll_retry_count,
            )
            await context.cancellation.sleep(DEFAULT_STALLED_SCROLL_RETRY_INTERVAL_SECONDS)

        completed_scrolls += actual_scrolls
        remaining_scrolls -= actual_scrolls
        visible = await _collect_with_retries(
            context,
            feed=feed,
            max_posts=max_posts_per_batch,
            include_ads=include_ads,
            timeout_seconds=collect_timeout_seconds,
            retry_count=collect_retry_count,
        )

    return {
        "status": "success",
        "scrolls": completed_scrolls,
        "postsSeen": len(seen),
        "atBoundary": at_boundary,
        "stopReason": stop_reason,
    }
