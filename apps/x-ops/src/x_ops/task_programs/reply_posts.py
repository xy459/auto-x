from typing import Any, Literal, Self

from pydantic import BaseModel, Field, model_validator

from ..task_sdk import TaskContext
from ._common import (
    matches_post,
    post_id,
    post_text,
    require_certain,
    result_status,
    run_timeline_batches,
    write_options,
)
from .spec import ProgramSpec

SPEC = ProgramSpec(
    name="reply_posts",
    version="1.0.0",
    title="匹配并回复",
    description="浏览和匹配帖子，使用固定文本或 AI 生成文本回复。",
)


class Params(BaseModel):
    feed: Literal["for_you", "following"] = "for_you"
    scroll_count: int = Field(default=10, ge=1, le=100)
    scroll_interval_seconds: float = Field(default=1.5, ge=0.25, le=10)
    scroll_distance: int = Field(default=650, ge=200, le=3000)
    target_authors: set[str] = Field(default_factory=set)
    keywords: set[str] = Field(default_factory=set)
    reply_mode: Literal["fixed", "ai"] = "fixed"
    fixed_reply: str | None = Field(default=None, max_length=280)
    ai_template: str = "reply_to_post"
    max_replies: int = Field(default=3, ge=1, le=50)

    @model_validator(mode="after")
    def fixed_mode_has_text(self) -> Self:
        if self.reply_mode == "fixed" and not (self.fixed_reply or "").strip():
            raise ValueError("fixed_reply is required when reply_mode=fixed")
        return self


async def run(context: TaskContext, params: Params) -> dict[str, Any]:
    await context.cancellation.raise_if_cancelled()
    matched = replied = skipped = 0

    async def handle_batch(posts: list[dict[str, Any]]) -> bool:
        nonlocal matched, replied, skipped
        for post in posts:
            await context.cancellation.raise_if_cancelled()
            if not matches_post(post, params.target_authors, params.keywords):
                continue
            tweet_id = post_id(post)
            if not tweet_id:
                continue
            matched += 1
            if replied >= params.max_replies:
                return True
            if params.reply_mode == "ai":
                await context.cancellation.raise_if_cancelled()
                text = await context.ai.generate(
                    template=params.ai_template,
                    variables={"post_text": post_text(post), "author": post.get("author")},
                )
            else:
                text = params.fixed_reply or ""
            await context.cancellation.raise_if_cancelled()
            result = require_certain(
                await context.actions.interaction.reply(
                    {"tweetId": tweet_id, "text": text},
                    options=write_options(
                        context, f"reply:{context.account.account_id}:{tweet_id}"
                    ),
                )
            )
            if result_status(result) == "success":
                replied += 1
            else:
                skipped += 1
            if replied >= params.max_replies:
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
        "replied": replied,
        "skipped": skipped,
    }
