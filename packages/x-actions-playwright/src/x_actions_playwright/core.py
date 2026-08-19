from __future__ import annotations

import asyncio
import hashlib
import re
from datetime import datetime
from typing import Any, Awaitable, Callable, TypeVar
from urllib.parse import urljoin, urlparse

from .errors import ActionError

T = TypeVar("T")


def clamp_number(value: Any, fallback: float, minimum: float, maximum: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        number = fallback
    return min(max(number, minimum), maximum)


def normalize_username(value: Any = "") -> str:
    return str(value).strip().removeprefix("@").lower()


def is_safe_username(value: Any = "") -> bool:
    return bool(re.fullmatch(r"[A-Za-z0-9_]{1,15}", str(value).removeprefix("@")))


def parse_tweet_identity(href: str = "") -> dict[str, str] | None:
    path = urlparse(urljoin("https://x.com", str(href))).path
    match = re.match(r"^/?([^/?#]+)/status/(\d+)", path, re.I)
    if not match:
        return None
    username, tweet_id = match.groups()
    return {
        "username": username,
        "tweetId": tweet_id,
        "path": f"/{username}/status/{tweet_id}",
        "url": f"https://x.com/{username}/status/{tweet_id}",
    }


def parse_media_viewer_position(url: str) -> dict[str, Any] | None:
    path = urlparse(urljoin("https://x.com", url)).path
    match = re.match(r"^/([^/]+)/status/(\d+)/photo/(\d+)/?$", path, re.I)
    if not match:
        return None
    username, tweet_id, raw_index = match.groups()
    index = int(raw_index)
    return {"username": username, "tweetId": tweet_id, "index": index}


def classify_page(url: str) -> str:
    parsed = urlparse(urljoin("https://x.com", url))
    if parsed.hostname not in {"x.com", "www.x.com"}:
        return "unknown"
    path = parsed.path.rstrip("/") or "/"
    if path == "/home":
        return "home"
    if path == "/compose/post":
        return "compose"
    if path == "/notifications":
        return "notifications"
    if path.startswith("/search"):
        return "search-people" if "f=user" in parsed.query else "search"
    if path.startswith("/i/chat"):
        return "messages" if path == "/i/chat" else "conversation"
    if re.search(r"/status/\d+/photo/\d+$", path, re.I):
        return "media-viewer"
    if re.search(r"/status/\d+$", path, re.I):
        return "tweet-detail"
    if re.fullmatch(r"/[A-Za-z0-9_]{1,15}", path):
        return "profile"
    return "unknown"


def hash_text(value: str = "") -> str:
    return hashlib.sha256(value.encode()).hexdigest()[:16]


def summarize_text(value: str = "", maximum: int = 120) -> str:
    text = " ".join(str(value).split())
    return text if len(text) <= maximum else f"{text[: maximum - 1]}…"


def parse_compact_number(value: str = "") -> dict[str, Any]:
    display = " ".join(str(value).split()).strip()
    if not display:
        return {"value": None, "displayText": None}
    match = re.search(r"(-?\d+(?:\.\d+)?)\s*(K|M|B|万|億|亿)?", display.replace(",", ""), re.I)
    if not match:
        return {"value": None, "displayText": display}
    multiplier = {"K": 1_000, "M": 1_000_000, "B": 1_000_000_000, "万": 10_000, "億": 100_000_000, "亿": 100_000_000}.get((match.group(2) or "").upper(), 1)
    return {"value": round(float(match.group(1)) * multiplier), "displayText": display}


def parse_metric_group(value: str = "") -> dict[str, int]:
    result = {"replyCount": 0, "repostCount": 0, "likeCount": 0, "viewCount": 0, "bookmarkCount": 0}
    labels = {
        "replyCount": ("reply", "replies", "回复", "评论"),
        "repostCount": ("repost", "retweet", "转推", "转帖"),
        "likeCount": ("like", "likes", "点赞"),
        "viewCount": ("view", "views", "查看", "浏览"),
        "bookmarkCount": ("bookmark", "bookmarks", "书签", "收藏"),
    }
    for part in re.split(r"[,，]", value):
        for key, names in labels.items():
            if any(name.lower() in part.lower() for name in names):
                result[key] = parse_compact_number(part)["value"] or 0
    return result


def relationship_state(test_id: str = "", aria: str = "") -> str:
    combined = f"{test_id} {aria}".lower()
    if "unfollow" in combined or "following" in combined:
        return "following"
    if "requested" in combined or "pending" in combined:
        return "requested"
    if "follow" in combined:
        return "not-following"
    return "unknown"


async def cancellable_sleep(milliseconds: int, cancellation: asyncio.Event | None = None) -> None:
    if cancellation and cancellation.is_set():
        raise ActionError("USER_CANCELLED", "Execution was cancelled.")
    sleep_task = asyncio.create_task(asyncio.sleep(max(0, milliseconds) / 1000))
    if not cancellation:
        await sleep_task
        return
    cancel_task = asyncio.create_task(cancellation.wait())
    done, pending = await asyncio.wait({sleep_task, cancel_task}, return_when=asyncio.FIRST_COMPLETED)
    for task in pending:
        task.cancel()
    if cancel_task in done and cancellation.is_set():
        raise ActionError("USER_CANCELLED", "Execution was cancelled.")


async def wait_for(
    check: Callable[[], Awaitable[T | None]],
    *,
    timeout_ms: int = 10_000,
    interval_ms: int = 120,
    description: str = "condition",
    cancellation: asyncio.Event | None = None,
) -> T:
    deadline = asyncio.get_running_loop().time() + timeout_ms / 1000
    last_error: BaseException | None = None
    while asyncio.get_running_loop().time() < deadline:
        if cancellation and cancellation.is_set():
            raise ActionError("USER_CANCELLED", f"Cancelled while waiting for {description}.")
        try:
            result = await check()
            if result is not None and result is not False:
                return result
        except Exception as error:  # polling tolerates transient detached nodes
            last_error = error
        await cancellable_sleep(interval_ms, cancellation)
    raise ActionError("TIMEOUT", f"Timed out waiting for {description}.", {"cause": str(last_error) if last_error else None})


def parse_schedule(value: Any) -> datetime:
    try:
        result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as error:
        raise ActionError("INVALID_SCHEDULE_TIME", "The schedule time is invalid.") from error
    return result
