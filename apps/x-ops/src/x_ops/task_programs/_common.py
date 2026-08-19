from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable, Mapping
from typing import Any

from ..task_sdk.context import TaskContext
from ..task_sdk.errors import TaskCancelledError, TaskUncertainError


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


async def run_timeline(
    context: TaskContext,
    *,
    feed: str,
    scroll_count: int,
    interval_seconds: float,
    distance: int = 650,
    collect: bool = False,
    max_posts: int = 500,
) -> dict[str, Any]:
    """Run a long timeline operation as cancellable, bounded action chunks."""
    interval_ms = max(250, int(interval_seconds * 1000))
    remaining_scrolls = scroll_count
    completed_scrolls = 0
    collected: dict[str, dict[str, Any]] = {}
    at_boundary = False
    method = context.actions.timeline.collect if collect else context.actions.timeline.browse

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
        options = action_options(context, duration_ms + 4_000)
        result = await method(payload, options=options)
        if result_status(result) == "navigating":
            await context.cancellation.raise_if_cancelled()
            result = await method(payload, options=options)
        result = require_certain(
            result, task_run_id=context.cancellation.task_run_id
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
        if actual_scrolls == 0 or len(collected) >= max_posts:
            break

    return {
        "status": "success",
        "scrolls": completed_scrolls,
        "posts": list(collected.values()),
        "atBoundary": at_boundary,
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

    async def call_with_navigation_retry(method: Any, payload: dict[str, Any], timeout_ms: int) -> Any:
        options = action_options(context, timeout_ms)
        result = await method(payload, options=options)
        if result_status(result) == "navigating":
            # page.goto(..., wait_until="domcontentloaded") may return before
            # the React timeline tabs mount. Give the page a cancellable turn
            # to hydrate instead of immediately counting a missing tab.
            await context.cancellation.sleep(0.5)
            result = await method(payload, options=options)
        return require_certain(
            result, task_run_id=context.cancellation.task_run_id
        )

    while True:
        await context.cancellation.raise_if_cancelled()
        visible = await call_with_navigation_retry(
            context.actions.timeline.collect,
            {
                "feed": feed_value(feed),
                "maxScrolls": 0,
                "durationMs": 0,
                "maxPosts": max_posts_per_batch,
                "includeAds": include_ads,
            },
            10_000,
        )
        batch: list[dict[str, Any]] = []
        for post in posts_from(visible):
            identity = post_id(post)
            if identity and identity not in seen:
                seen.add(identity)
                batch.append(post)

        if batch and await handle_batch(batch):
            break
        if remaining_scrolls <= 0 or at_boundary:
            break

        duration_ms = min(15_000, interval_ms + 2_000)
        browsed = await call_with_navigation_retry(
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
        completed_scrolls += actual_scrolls
        remaining_scrolls -= actual_scrolls
        at_boundary = bool(data.get("atBoundary"))
        if actual_scrolls == 0:
            break

    return {
        "status": "success",
        "scrolls": completed_scrolls,
        "postsSeen": len(seen),
        "atBoundary": at_boundary,
    }
