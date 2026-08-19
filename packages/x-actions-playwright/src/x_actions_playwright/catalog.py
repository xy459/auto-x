from __future__ import annotations

from typing import Any

from .models import ActionDefinition, FailureMode

ACTION_CATEGORIES = (
    "context", "timeline", "post", "image", "video", "comment",
    "interaction", "browse", "account", "publish", "message",
)

_FAILURE_DESCRIPTIONS = {
    "UNEXPECTED_ERROR": "Unclassified runtime or X layout error.",
    "USER_CANCELLED": "Execution was cancelled before or during the action.",
    "CONTENT_MISMATCH": "Input is invalid or X did not accept the requested content.",
    "CONFIRMATION_REQUIRED": "A live write requires confirmLive=true.",
    "IDEMPOTENCY_IN_PROGRESS": "Another execution currently owns the idempotency key.",
    "IDEMPOTENCY_STATE_CONFLICT": "The idempotency key has an unsupported terminal state.",
    "ACTION_UNSUPPORTED": "The current X layout or target does not expose this action.",
    "PAGE_UNSUPPORTED": "The current page does not satisfy the action precondition.",
    "TARGET_NOT_FOUND": "The target post, comment, control, or editor is not in the current DOM.",
    "TARGET_UNSAFE": "The selected target is an ad, another user's post, or otherwise unsafe for this action.",
    "STATE_UNKNOWN": "The page or target state could not be identified or verified.",
    "TIMEOUT": "A locator or postcondition did not become ready before the timeout.",
    "ELEMENT_NOT_VISIBLE": "The target control exists but is not visible.",
    "ELEMENT_DISABLED": "The target control is disabled.",
    "ELEMENT_BLOCKED": "The target control is covered or cannot receive input.",
    "ACCOUNT_NOT_FOUND": "The requested account does not exist.",
    "ACCOUNT_SUSPENDED": "The requested account is suspended.",
    "ACCOUNT_TEMPORARILY_RESTRICTED": "The requested account is temporarily restricted.",
    "ACCOUNT_MISMATCH": "The loaded profile does not match the requested account.",
    "PROFILE_LOAD_FAILED": "X explicitly reported that the profile failed to load.",
    "PROFILE_LOADING_TIMEOUT": "The profile remained in a loading/skeleton state.",
    "PROFILE_STATE_UNKNOWN": "The profile loaded without a recognizable account state.",
    "INVALID_SCHEDULE_TIME": "The requested schedule time is invalid or too soon.",
    "DRAFT_CONFLICT": "A non-empty draft would be overwritten.",
    "MEDIA_TOO_LARGE": "The selected media exceeds the local upload safety limit.",
    "SUBMISSION_REJECTED": "X explicitly rejected the submission.",
    "SUBMISSION_RESULT_UNKNOWN": "A live write was triggered but its final state could not be confirmed.",
    "BROWSER_CLOSED_DURING_RUN": "The caller-owned browser or page closed during the action.",
}
_RETRYABLE = {"IDEMPOTENCY_IN_PROGRESS", "STATE_UNKNOWN", "TIMEOUT", "ELEMENT_NOT_VISIBLE", "ELEMENT_BLOCKED", "PROFILE_LOAD_FAILED", "PROFILE_LOADING_TIMEOUT"}

BASE = ("UNEXPECTED_ERROR", "USER_CANCELLED", "BROWSER_CLOSED_DURING_RUN", "TIMEOUT")
TARGET = BASE + ("TARGET_NOT_FOUND", "TARGET_UNSAFE", "STATE_UNKNOWN")
CLICK = TARGET + ("ELEMENT_NOT_VISIBLE", "ELEMENT_DISABLED", "ELEMENT_BLOCKED", "TIMEOUT")
WRITE = BASE + ("CONFIRMATION_REQUIRED", "IDEMPOTENCY_IN_PROGRESS", "IDEMPOTENCY_STATE_CONFLICT") + CLICK[len(BASE) :]
COMPOSER = BASE + (
    "CONFIRMATION_REQUIRED", "IDEMPOTENCY_IN_PROGRESS", "IDEMPOTENCY_STATE_CONFLICT",
    "CONTENT_MISMATCH", "DRAFT_CONFLICT", "SUBMISSION_REJECTED",
    "TARGET_NOT_FOUND", "ELEMENT_NOT_VISIBLE", "ELEMENT_DISABLED", "ELEMENT_BLOCKED", "TIMEOUT",
)

# id, label, handler, access, retry, target, idempotent, requires_tweet,
# requires_comment, deprecated, replaced_by, failure codes, edge-case summary
_SPECS: tuple[tuple[Any, ...], ...] = (
    ("context.inspect", "Inspect context", "inspect", "read", "safe", "none", False, False, False, False, None, BASE + ("STATE_UNKNOWN",), "non-X page; loading skeleton; signed-out or virtualized timeline"),
    ("context.selectPost", "Select post", "select_post", "read", "safe", "tweet", False, True, False, False, None, BASE + ("TARGET_NOT_FOUND",), "target scrolled out of the virtual list; quoted status links must not win"),
    ("timeline.open", "Open timeline", "timeline_open", "read", "safe", "none", False, False, False, False, None, BASE + ("CONTENT_MISMATCH", "TARGET_NOT_FOUND", "ELEMENT_NOT_VISIBLE", "ELEMENT_DISABLED", "ELEMENT_BLOCKED", "TIMEOUT"), "invalid feed; Home navigation is two-phase; tab selection must be verified"),
    ("timeline.browse", "Browse timeline", "timeline_browse", "read", "safe", "none", False, False, False, False, None, BASE + ("CONTENT_MISMATCH",) + CLICK[2:], "bounded duration/scroll count; cancellation; changing virtual-list height"),
    ("timeline.collect", "Browse and collect", "timeline_collect", "read", "safe", "none", False, False, False, False, None, BASE + ("CONTENT_MISMATCH",) + CLICK[2:], "deduplicate virtualized posts; ad filtering; media/quote ownership; bounded Show more"),
    ("post.openDetails", "Open post details", "post_open_details", "read", "safe", "tweet", False, True, False, False, None, TARGET, "canonical main-post URL only; navigation is reported, not disguised as completion"),
    ("post.getDetails", "Get post details", "post_get_details", "read", "safe", "tweet", False, True, False, False, None, TARGET, "localized metrics; quote/media ownership; content-only posts; ads excluded by default"),
    ("post.getType", "Classify direct/quote", "post_get_type", "read", "safe", "tweet", False, True, False, False, None, TARGET, "reply and repost banners are not quote posts; missing quoted identity is retained"),
    ("post.delete", "Delete post", "post_delete", "write", "never", "tweet", False, True, False, False, None, WRITE + ("SUBMISSION_REJECTED",), "only the signed-in owner's post; portal menus; clicked-but-unverified is uncertain"),
    ("post.exitDetails", "Exit post details", "post_exit_details", "read", "safe", "tweet-detail", False, False, False, False, None, BASE + ("ELEMENT_NOT_VISIBLE", "ELEMENT_DISABLED", "ELEMENT_BLOCKED", "TIMEOUT"), "modal versus route detail; do not navigate outside x.com"),
    ("post.expand", "Expand Show more", "post_expand", "read", "safe", "tweet", False, True, False, False, None, CLICK, "only the main tweet text; quoted/card Show more excluded; verify text growth"),
    ("post.getUrl", "Get canonical URL", "post_get_url", "read", "safe", "tweet", False, True, False, False, None, TARGET, "pure read; quoted status links excluded; returns canonical x.com URL"),
    ("post.copyLink", "Copy link through X", "post_copy_link", "read", "safe", "tweet", False, True, False, False, None, CLICK, "re-locate portal menu after delay; clipboard acceptance may be unverifiable"),
    ("image.open", "Open image", "image_open", "read", "safe", "tweet", False, True, False, False, None, CLICK, "main-post photo routes only; link previews, article covers and quoted photos excluded"),
    ("image.previous", "Previous image", "image_previous", "read", "safe", "media-viewer", False, False, False, False, None, BASE + ("STATE_UNKNOWN", "CONTENT_MISMATCH", "ELEMENT_NOT_VISIBLE", "ELEMENT_DISABLED", "ELEMENT_BLOCKED", "TIMEOUT"), "first image is skipped; viewer URL/index must move backward"),
    ("image.next", "Next image", "image_next", "read", "safe", "media-viewer", False, False, False, False, None, BASE + ("STATE_UNKNOWN", "CONTENT_MISMATCH", "ELEMENT_NOT_VISIBLE", "ELEMENT_DISABLED", "ELEMENT_BLOCKED", "TIMEOUT"), "last image is skipped; never cross into another post's media"),
    ("image.close", "Close image", "image_close", "read", "safe", "media-viewer", False, False, False, False, None, BASE + ("ELEMENT_NOT_VISIBLE", "ELEMENT_DISABLED", "ELEMENT_BLOCKED", "TIMEOUT"), "close modal first and preserve the underlying post/detail context"),
    ("video.play", "Play video", "video_play", "read", "safe", "tweet", False, True, False, False, None, CLICK, "main-post video only; browser autoplay policy; GIF/live variants"),
    ("video.pause", "Pause video", "video_pause", "read", "safe", "tweet", False, True, False, False, None, CLICK, "main-post video only; node may be rebuilt; verify paused state"),
    ("video.unmute", "Unmute video", "video_unmute", "read", "safe", "tweet", False, True, False, False, None, CLICK, "no-audio media; autoplay policy; do not affect other players"),
    ("video.mute", "Mute video", "video_mute", "read", "safe", "tweet", False, True, False, False, None, CLICK, "main video only; player/global volume differences; verify muted state"),
    ("comment.listVisible", "List visible comments", "comment_list_visible", "read", "safe", "comments", False, False, False, False, None, BASE + ("PAGE_UNSUPPORTED", "STATE_UNKNOWN"), "exclude root and Discover more; visible region only; localized reply context"),
    ("comment.collect", "Collect comments", "comment_collect", "read", "safe", "comments", False, False, False, False, None, BASE + ("PAGE_UNSUPPORTED", "STATE_UNKNOWN", "TIMEOUT"), "virtual-list deduplication; Discover more boundary; bounded cancellation-aware scrolling"),
    ("comment.get", "Get comment", "comment_get", "read", "safe", "comment", False, False, True, False, None, BASE + ("PAGE_UNSUPPORTED", "TARGET_NOT_FOUND", "STATE_UNKNOWN"), "commentId cannot be the root; must be before Discover more and currently visible"),
    ("comment.like", "Like comment", "comment_like", "write", "never", "comment", True, False, True, False, None, WRITE + ("PAGE_UNSUPPORTED", "ACTION_UNSUPPORTED"), "comment-scoped target; already liked skips; unverified postcondition is uncertain"),
    ("comment.unlike", "Unlike comment", "comment_unlike", "write", "never", "comment", True, False, True, False, None, WRITE + ("PAGE_UNSUPPORTED", "ACTION_UNSUPPORTED"), "comment-scoped target; already unliked skips; never toggle blindly"),
    ("comment.reply", "Reply to comment", "comment_reply", "write", "never", "comment", False, False, True, False, None, COMPOSER + ("PAGE_UNSUPPORTED", "ACTION_UNSUPPORTED", "TARGET_UNSAFE", "STATE_UNKNOWN"), "bind the newly opened reply composer; verify replying-to target; uncertain after submit"),
    ("comment.quote", "Quote comment", "comment_quote", "write", "never", "comment", False, False, True, False, None, COMPOSER + ("PAGE_UNSUPPORTED", "ACTION_UNSUPPORTED", "TARGET_UNSAFE", "STATE_UNKNOWN"), "open the selected comment's repost menu; never quote the root by mistake"),
    ("comment.deleteReply", "Delete own reply", "comment_delete_reply", "write", "never", "reply", False, False, False, False, None, WRITE + ("SUBMISSION_REJECTED",), "replyId is its own post ID; ownership required; clicked-but-unverified is uncertain"),
    ("interaction.reply", "Reply to post", "interaction_reply", "write", "never", "tweet", False, True, False, False, None, COMPOSER + ("TARGET_UNSAFE", "STATE_UNKNOWN"), "new overlay composer must not be confused with background inline composer"),
    ("interaction.quote", "Quote post", "interaction_quote", "write", "never", "tweet", False, True, False, False, None, COMPOSER + ("TARGET_UNSAFE", "STATE_UNKNOWN"), "quote menu and newly mounted composer; residual draft rejected; uncertain after submit"),
    ("interaction.like", "Like", "interaction_like", "write", "never", "tweet", True, True, False, False, None, WRITE, "already liked skips; quoted controls excluded; verify unlike target state"),
    ("interaction.unlike", "Unlike", "interaction_unlike", "write", "never", "tweet", True, True, False, False, None, WRITE, "already unliked skips; target-state semantics rather than toggle semantics"),
    ("interaction.bookmark", "Bookmark", "interaction_bookmark", "write", "never", "tweet", True, True, False, False, None, WRITE, "already bookmarked skips; layout may expose bookmark only in detail"),
    ("interaction.repost", "Repost", "interaction_repost", "write", "never", "tweet", True, True, False, False, None, WRITE, "re-locate confirmation menu; do not choose Quote; verify unretweet state"),
    ("interaction.undoRepost", "Undo repost", "interaction_undo_repost", "write", "never", "tweet", True, True, False, False, None, WRITE, "ordinary repost only; re-locate undo menu; verify retweet state"),
    ("interaction.sendViaChat", "Send via chat", "interaction_send_via_chat", "read", "safe", "tweet", False, True, False, False, None, CLICK, "opens X recipient dialog only; does not silently complete a final send"),
    ("browse.scrollTimeline", "Scroll timeline", "browse_scroll_timeline", "read", "safe", "none", False, False, False, False, None, BASE + ("PAGE_UNSUPPORTED", "STATE_UNKNOWN"), "Home only; bounded distance; top/bottom is a normal skip"),
    ("browse.openForYou", "Open For you", "browse_open_for_you", "read", "safe", "none", False, False, False, True, "timeline.open", BASE + ("TARGET_NOT_FOUND", "ELEMENT_NOT_VISIBLE", "ELEMENT_DISABLED", "ELEMENT_BLOCKED", "TIMEOUT"), "deprecated compatibility wrapper for feed=for-you"),
    ("browse.openFollowing", "Open Following", "browse_open_following", "read", "safe", "none", False, False, False, True, "timeline.open", BASE + ("TARGET_NOT_FOUND", "ELEMENT_NOT_VISIBLE", "ELEMENT_DISABLED", "ELEMENT_BLOCKED", "TIMEOUT"), "deprecated compatibility wrapper for feed=following"),
    ("browse.browseForYou", "Browse For you", "browse_browse_for_you", "read", "safe", "none", False, False, False, True, "timeline.browse", CLICK, "deprecated compatibility wrapper; bounded scrolling and cancellation"),
    ("browse.browseFollowing", "Browse Following", "browse_browse_following", "read", "safe", "none", False, False, False, True, "timeline.browse", CLICK, "deprecated compatibility wrapper; bounded scrolling and cancellation"),
    ("browse.browseAndCollectForYou", "Collect For you", "browse_collect_for_you", "read", "safe", "none", False, False, False, True, "timeline.collect", CLICK, "deprecated wrapper; virtual-list deduplication and media ownership"),
    ("browse.browseAndCollectFollowing", "Collect Following", "browse_collect_following", "read", "safe", "none", False, False, False, True, "timeline.collect", CLICK, "deprecated wrapper; virtual-list deduplication and media ownership"),
    ("browse.scrollComments", "Scroll comments", "browse_scroll_comments", "read", "safe", "none", False, False, False, False, None, BASE + ("PAGE_UNSUPPORTED", "STATE_UNKNOWN"), "detail page only; bounded scroll; Discover more and lazy loading"),
    ("browse.wait", "Wait", "browse_wait", "read", "safe", "none", False, False, False, False, None, BASE, "duration clamped; cancellation-aware; waiting does not assert element readiness"),
    ("account.search", "Search account", "account_search", "read", "safe", "none", False, False, False, False, None, BASE + ("CONTENT_MISMATCH",), "safe handle navigates directly; names use People search; two-phase navigation"),
    ("account.getSession", "Get signed-in session", "account_get_session", "read", "safe", "none", False, False, False, False, None, BASE + ("STATE_UNKNOWN",), "explicit signed-out versus unknown; account switcher/profile link evidence"),
    ("account.getDetails", "Get account details", "account_get_details", "read", "safe", "profile", False, False, False, False, None, BASE + ("CONTENT_MISMATCH", "PAGE_UNSUPPORTED", "ACCOUNT_NOT_FOUND", "ACCOUNT_SUSPENDED", "ACCOUNT_TEMPORARILY_RESTRICTED", "ACCOUNT_MISMATCH", "PROFILE_LOAD_FAILED", "PROFILE_LOADING_TIMEOUT", "PROFILE_STATE_UNKNOWN"), "renamed handle redirects; private/suspended/missing/loading states; scoped profile header"),
    ("account.listCandidates", "List account candidates", "account_list_candidates", "read", "safe", "none", False, False, False, False, None, BASE + ("PAGE_UNSUPPORTED", "STATE_UNKNOWN"), "People results only; virtualized cards; deduplicate handles; exclude side recommendations"),
    ("account.follow", "Follow profile", "account_follow", "write", "never", "profile", True, False, False, False, None, WRITE + ("PAGE_UNSUPPORTED",), "current profile only; self target rejected; following/requested skips; verify relationship"),
    ("account.unfollow", "Unfollow profile", "account_unfollow", "write", "never", "profile", True, False, False, False, None, WRITE + ("PAGE_UNSUPPORTED",), "current profile only; confirm dialog re-located; verify relationship"),
    ("publish.post", "Publish post", "publish_post", "write", "never", "composer", False, False, False, False, None, COMPOSER + ("MEDIA_TOO_LARGE",), "empty content; media constraints; existing draft; editor state sync; uncertain after final click"),
    ("publish.schedule", "Schedule post", "publish_schedule", "write", "never", "composer", False, False, False, False, None, COMPOSER + ("INVALID_SCHEDULE_TIME", "MEDIA_TOO_LARGE"), "future local time; labeled schedule controls; media/draft/editor sync; uncertain after final click"),
    ("message.replyConversation", "Reply to conversation", "message_reply_conversation", "write", "never", "conversation", False, False, False, False, None, COMPOSER + ("PAGE_UNSUPPORTED",), "specific chat only; bind local editor/send button; cleared-without-proof is uncertain"),
)


def _schema(action_id: str, target: str) -> tuple[dict[str, Any], dict[str, Any]]:
    properties: dict[str, Any] = {}
    required: list[str] = []
    if target == "tweet":
        properties["tweetId"] = {"type": "string", "pattern": r"^\d+$"}
        required.append("tweetId")
    if target == "comment":
        properties["commentId"] = {"type": "string", "pattern": r"^\d+$"}
        required.append("commentId")
    if target == "reply":
        properties["replyId"] = {"type": "string", "pattern": r"^\d+$"}
        required.append("replyId")
    if any(part in action_id for part in ("reply", "quote")) and action_id != "interaction.sendViaChat":
        properties["text"] = {"type": "string", "minLength": 1}
        if action_id not in {"comment.deleteReply"}:
            required.append("text")
    if action_id.startswith("timeline."):
        properties.update({"feed": {"enum": ["for-you", "following"]}, "maxScrolls": {"type": "integer", "minimum": 0, "maximum": 100}, "durationMs": {"type": "integer", "minimum": 0, "maximum": 60000}})
        required.append("feed")
    if action_id == "account.search":
        properties["query"] = {"type": "string", "minLength": 1}
        required.append("query")
    if action_id == "account.getDetails":
        properties["handle"] = {"type": "string"}
    if action_id == "publish.post":
        properties.update({"text": {"type": "string"}, "media": {"type": "array"}})
    if action_id == "publish.schedule":
        properties.update({"text": {"type": "string"}, "media": {"type": "array"}, "scheduleAt": {"type": "string", "format": "date-time"}})
        required.append("scheduleAt")
    input_schema = {"type": "object", "properties": properties, "required": sorted(set(required)), "additionalProperties": True}
    output_schema = {"type": "object", "properties": {"status": {"enum": ["success", "skipped", "navigating", "uncertain", "cancelled", "failed"]}, "data": {"type": "object"}}, "required": ["status", "data"]}
    return input_schema, output_schema


def _definition(spec: tuple[Any, ...]) -> ActionDefinition:
    action_id, label, handler, access, retry, target, idem, req_tweet, req_comment, deprecated, replaced, codes, edge = spec
    input_schema, output_schema = _schema(action_id, target)
    failures = tuple(
        FailureMode(
            code,
            _FAILURE_DESCRIPTIONS[code],
            code in _RETRYABLE and retry == "safe",
            access == "write" and code in {"BROWSER_CLOSED_DURING_RUN", "TIMEOUT", "UNEXPECTED_ERROR"},
        )
        for code in dict.fromkeys(codes)
    )
    return ActionDefinition(
        id=action_id,
        category=action_id.split(".", 1)[0],
        method=action_id.split(".", 1)[1],
        label=label,
        handler=handler,
        access=access,
        retry_policy=retry,
        target_type=target,
        idempotent=idem,
        enabled=True,
        confirmation="required" if access == "write" else "none",
        requires_tweet=req_tweet,
        requires_comment=req_comment,
        deprecated=deprecated,
        replaced_by=replaced,
        failure_modes=failures,
        edge_cases=tuple(part.strip() for part in edge.split(";") if part.strip()),
        input_schema=input_schema,
        output_schema=output_schema,
    )


ACTION_DEFINITIONS = {definition.id: definition for definition in map(_definition, _SPECS)}


def get_action_definition(action_id: str) -> ActionDefinition | None:
    return ACTION_DEFINITIONS.get(action_id)


def list_actions(category: str | None = None) -> list[ActionDefinition]:
    definitions = list(ACTION_DEFINITIONS.values())
    return definitions if category is None else [item for item in definitions if item.category == category]
