from typing import Any, Literal

from pydantic import BaseModel, Field

from ..task_sdk import TaskContext
from ._common import (
    matches_post,
    post_id,
    require_certain,
    result_status,
    run_timeline_batches,
    write_options,
)
from .spec import ProgramSpec

SPEC = ProgramSpec(
    name="like_posts",
    version="1.0.0",
    title="匹配并点赞",
    description="浏览时间线，按作者和关键词匹配帖子后点赞。",
)


class Params(BaseModel):
    feed: Literal["for_you", "following"] = "for_you"
    scroll_count: int = Field(default=10, ge=1, le=100)
    scroll_interval_seconds: float = Field(default=1.5, ge=0.25, le=10)
    scroll_distance: int = Field(default=650, ge=200, le=3000)
    target_authors: set[str] = Field(default_factory=set)
    keywords: set[str] = Field(default_factory=set)
    max_likes: int = Field(default=10, ge=1, le=100)


async def run(context: TaskContext, params: Params) -> dict[str, Any]:
    await context.cancellation.raise_if_cancelled()
    matched = liked = skipped = 0

    async def handle_batch(posts: list[dict[str, Any]]) -> bool:
        nonlocal matched, liked, skipped
        for post in posts:
            await context.cancellation.raise_if_cancelled()
            if not matches_post(post, params.target_authors, params.keywords):
                continue
            tweet_id = post_id(post)
            if not tweet_id:
                continue
            matched += 1
            if liked >= params.max_likes:
                return True
            context.logger.info("匹配到待点赞帖子", post_id=tweet_id)
            result = require_certain(
                await context.actions.interaction.like(
                    {"tweetId": tweet_id},
                    options=write_options(
                        context, f"like:{context.account.account_id}:{tweet_id}"
                    ),
                )
            )
            if result_status(result) == "success":
                liked += 1
            else:
                skipped += 1
            if liked >= params.max_likes:
                return True
        return False

    summary = await run_timeline_batches(
        context,
        feed=params.feed,
        scroll_count=params.scroll_count,
        interval_seconds=params.scroll_interval_seconds,
        distance=params.scroll_distance,
        handle_batch=handle_batch,
    )
    return {
        "posts_seen": summary["postsSeen"],
        "matched": matched,
        "liked": liked,
        "skipped": skipped,
    }
