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
    timeline_ready_timeout_seconds: float = Field(default=30, ge=5, le=120)
    collect_timeout_seconds: float = Field(default=30, ge=5, le=120)
    collect_retry_count: int = Field(default=2, ge=0, le=5)
    stalled_scroll_retry_count: int = Field(default=3, ge=0, le=10)
    interaction_timeout_seconds: float = Field(default=30, ge=10, le=120)


# 完整执行流程：
# Runner 校验参数并准备浏览器
#   → like_posts.run()
#   → 检查取消状态并初始化统计值
#   → run_timeline_batches()
#   → 收集当前可见帖子，排除广告并按帖子 ID 去重
#   → 按目标作者和关键词匹配帖子
#   → 获取匹配帖子的 ID 并累计 matched
#   → interaction.like()
#   → 已点赞则跳过，否则点击点赞并确认按钮变为已点赞状态
#   → 根据结果累计 liked 或 skipped
#   → 未达到 max_likes 时滚动一次并处理下一批帖子
#   → 达到点赞上限、滚动上限、页面底部或取消状态时结束
#   → 返回 posts_seen、matched、liked 和 skipped
#   → Runner 保存成功、失败、不确定或取消状态
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
                        context,
                        f"like:{context.account.account_id}:{tweet_id}",
                        timeout_ms=int(params.interaction_timeout_seconds * 1_000),
                    ),
                ),
                task_run_id=context.cancellation.task_run_id,
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
        timeline_ready_timeout_seconds=params.timeline_ready_timeout_seconds,
        collect_timeout_seconds=params.collect_timeout_seconds,
        collect_retry_count=params.collect_retry_count,
        stalled_scroll_retry_count=params.stalled_scroll_retry_count,
    )
    return {
        "posts_seen": summary["postsSeen"],
        "matched": matched,
        "liked": liked,
        "skipped": skipped,
        "scrolls_completed": summary["scrolls"],
        "stop_reason": summary["stopReason"],
    }
