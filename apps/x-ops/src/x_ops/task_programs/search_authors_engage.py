from __future__ import annotations

import re
from typing import Any, Literal, Self

from pydantic import BaseModel, Field, field_validator, model_validator
from x_actions_playwright.errors import ActionError

from ..task_sdk import TaskContext
from ._common import (
    action_options,
    matches_post,
    post_id,
    post_text,
    posts_from,
    require_certain,
    result_data,
    result_status,
    write_options,
)
from .spec import ProgramSpec

SPEC = ProgramSpec(
    name="search_authors_engage",
    version="1.0.0",
    title="指定作者关注并互动",
    description="逐个打开指定作者主页，关注作者并对其帖子点赞或回复。",
)

USERNAME_RE = re.compile(r"^[A-Za-z0-9_]{1,15}$")
SKIPPABLE_PROFILE_ERRORS = {
    "ACCOUNT_NOT_FOUND",
    "ACCOUNT_SUSPENDED",
    "ACCOUNT_TEMPORARILY_RESTRICTED",
    "ACCOUNT_MISMATCH",
    "PROFILE_LOAD_FAILED",
    "PROFILE_LOADING_TIMEOUT",
    "PROFILE_STATE_UNKNOWN",
}


class Params(BaseModel):
    authors: list[str] = Field(min_length=1, max_length=100)
    follow_authors: bool = True
    like_posts: bool = True
    reply_mode: Literal["none", "fixed", "ai"] = "none"
    fixed_reply: str | None = Field(default=None, max_length=280)
    ai_template: str = "reply_to_post"
    keywords: set[str] = Field(default_factory=set)
    max_posts_per_author: int = Field(default=2, ge=1, le=20)
    max_profile_scrolls: int = Field(default=3, ge=0, le=20)
    profile_scroll_interval_seconds: float = Field(default=2, ge=0.25, le=10)
    profile_scroll_distance: int = Field(default=650, ge=200, le=3000)
    author_interval_seconds: float = Field(default=5, ge=0, le=60)
    include_replies: bool = False
    include_pinned: bool = False
    profile_timeout_seconds: float = Field(default=30, ge=5, le=120)
    interaction_timeout_seconds: float = Field(default=30, ge=10, le=120)

    @field_validator("authors")
    @classmethod
    def valid_authors(cls, values: list[str]) -> list[str]:
        normalized: list[str] = []
        seen: set[str] = set()
        for value in values:
            username = str(value).strip().removeprefix("@")
            if not USERNAME_RE.fullmatch(username):
                raise ValueError(f"invalid X username: {value}")
            key = username.casefold()
            if key not in seen:
                seen.add(key)
                normalized.append(username)
        if not normalized:
            raise ValueError("at least one author is required")
        return normalized

    @model_validator(mode="after")
    def actions_are_valid(self) -> Self:
        if self.reply_mode == "fixed" and not (self.fixed_reply or "").strip():
            raise ValueError("fixed_reply is required when reply_mode=fixed")
        if not self.follow_authors and not self.like_posts and self.reply_mode == "none":
            raise ValueError("at least one follow or engagement action must be enabled")
        return self


async def _open_author(context: TaskContext, username: str, timeout_ms: int) -> dict[str, Any]:
    options = action_options(context, timeout_ms)
    search = await context.actions.account.search({"query": username}, options=options)
    if result_status(search) not in {"success", "skipped", "navigating"}:
        require_certain(search, task_run_id=context.cancellation.task_run_id)
    await context.cancellation.sleep(0.5)
    details = await context.actions.account.getDetails({"handle": username}, options=options)
    if result_status(details) == "navigating":
        await context.cancellation.sleep(0.5)
        details = await context.actions.account.getDetails({"handle": username}, options=options)
    require_certain(details, task_run_id=context.cancellation.task_run_id)
    return result_data(details)


async def run(context: TaskContext, params: Params) -> dict[str, Any]:
    authors_processed = authors_skipped = followed = follow_skipped = 0
    posts_seen = posts_matched = liked = like_skipped = replied = reply_skipped = 0
    own_username = str(getattr(context.account, "username", None) or "").lstrip("@").casefold()
    timeout_ms = int(params.profile_timeout_seconds * 1_000)
    interaction_timeout_ms = int(params.interaction_timeout_seconds * 1_000)

    for author_index, requested_username in enumerate(params.authors):
        await context.cancellation.raise_if_cancelled()
        context.logger.info("准备处理指定作者", author=requested_username)
        try:
            details = await _open_author(context, requested_username, timeout_ms)
        except ActionError as error:
            if error.code not in SKIPPABLE_PROFILE_ERRORS:
                raise
            authors_skipped += 1
            context.logger.warning(
                "跳过无法使用的作者主页",
                author=requested_username,
                error_code=error.code,
                error_message=error.message,
            )
            continue

        account = details.get("account") or {}
        resolved_username = str(account.get("username") or requested_username).lstrip("@")
        authors_processed += 1

        if params.follow_authors:
            if resolved_username.casefold() == own_username:
                follow_skipped += 1
                context.logger.info("跳过关注当前登录账号", author=resolved_username)
            else:
                try:
                    follow_result = require_certain(
                        await context.actions.account.follow(
                            {},
                            options=write_options(
                                context,
                                f"follow:{context.account.account_id}:{resolved_username.casefold()}",
                                timeout_ms=interaction_timeout_ms,
                            ),
                        ),
                        task_run_id=context.cancellation.task_run_id,
                    )
                except ActionError as error:
                    if error.code != "TARGET_UNSAFE":
                        raise
                    follow_skipped += 1
                    context.logger.info("跳过关注当前登录账号", author=resolved_username)
                else:
                    if result_status(follow_result) == "success":
                        followed += 1
                    else:
                        follow_skipped += 1

        if params.like_posts or params.reply_mode != "none":
            handled_posts: set[str] = set()
            processed_for_author = 0
            for scroll_index in range(params.max_profile_scrolls + 1):
                await context.cancellation.raise_if_cancelled()
                collected = require_certain(
                    await context.actions.account.listPosts(
                        {
                            "maxPosts": 50,
                            "includeReplies": params.include_replies,
                            "includePinned": params.include_pinned,
                        },
                        options=action_options(context, timeout_ms),
                    ),
                    task_run_id=context.cancellation.task_run_id,
                )
                for post in posts_from(collected):
                    tweet_id = post_id(post)
                    if not tweet_id or tweet_id in handled_posts:
                        continue
                    handled_posts.add(tweet_id)
                    posts_seen += 1
                    if not matches_post(post, (), params.keywords):
                        continue
                    posts_matched += 1
                    processed_for_author += 1
                    if params.like_posts:
                        like_result = require_certain(
                            await context.actions.interaction.like(
                                {"tweetId": tweet_id},
                                options=write_options(
                                    context,
                                    f"author-like:{context.account.account_id}:{tweet_id}",
                                    timeout_ms=interaction_timeout_ms,
                                ),
                            ),
                            task_run_id=context.cancellation.task_run_id,
                        )
                        if result_status(like_result) == "success":
                            liked += 1
                        else:
                            like_skipped += 1
                    if params.reply_mode != "none":
                        await context.cancellation.raise_if_cancelled()
                        if params.reply_mode == "ai":
                            reply_text = await context.ai.generate(
                                template=params.ai_template,
                                variables={"post_text": post_text(post), "author": post.get("author")},
                            )
                        else:
                            reply_text = params.fixed_reply or ""
                        reply_result = require_certain(
                            await context.actions.interaction.reply(
                                {"tweetId": tweet_id, "text": reply_text},
                                options=write_options(
                                    context,
                                    f"author-reply:{context.account.account_id}:{tweet_id}",
                                    timeout_ms=interaction_timeout_ms,
                                ),
                            ),
                            task_run_id=context.cancellation.task_run_id,
                        )
                        if result_status(reply_result) == "success":
                            replied += 1
                        else:
                            reply_skipped += 1
                    if processed_for_author >= params.max_posts_per_author:
                        break
                if processed_for_author >= params.max_posts_per_author or scroll_index >= params.max_profile_scrolls:
                    break
                scroll_result = require_certain(
                    await context.actions.account.scrollPosts(
                        {"distance": params.profile_scroll_distance},
                        options=action_options(context, timeout_ms),
                    ),
                    task_run_id=context.cancellation.task_run_id,
                )
                scroll_data = result_data(scroll_result)
                if int(scroll_data.get("scrolls", 0)) == 0 or scroll_data.get("atBoundary") is True:
                    break
                await context.cancellation.sleep(params.profile_scroll_interval_seconds)

        if author_index + 1 < len(params.authors) and params.author_interval_seconds:
            await context.cancellation.sleep(params.author_interval_seconds)

    return {
        "authors_requested": len(params.authors),
        "authors_processed": authors_processed,
        "authors_skipped": authors_skipped,
        "followed": followed,
        "follow_skipped": follow_skipped,
        "posts_seen": posts_seen,
        "posts_matched": posts_matched,
        "liked": liked,
        "like_skipped": like_skipped,
        "replied": replied,
        "reply_skipped": reply_skipped,
    }
