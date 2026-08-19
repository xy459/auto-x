from __future__ import annotations

import asyncio

import pytest

from x_actions_playwright.core import (
    cancellable_sleep,
    classify_page,
    normalize_username,
    parse_compact_number,
    parse_media_viewer_position,
    parse_metric_group,
    parse_tweet_identity,
    relationship_state,
)
from x_actions_playwright.errors import ActionError


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://x.com/home", "home"),
        ("https://x.com/user/status/123", "tweet-detail"),
        ("https://x.com/user/status/123/photo/2", "media-viewer"),
        ("https://x.com/search?q=a&f=user", "search-people"),
        ("https://x.com/i/chat/abc", "conversation"),
        ("https://example.com/home", "unknown"),
    ],
)
def test_classify_page(url, expected):
    assert classify_page(url) == expected


def test_identity_and_media_parsing():
    assert parse_tweet_identity("/@bad/no") is None
    assert parse_tweet_identity("/alice/status/123/photo/2") == {"username": "alice", "tweetId": "123", "path": "/alice/status/123", "url": "https://x.com/alice/status/123"}
    assert parse_media_viewer_position("https://x.com/alice/status/123/photo/2") == {"username": "alice", "tweetId": "123", "index": 2}


def test_numbers_metrics_and_relationships():
    assert parse_compact_number("1.2K Likes")["value"] == 1200
    assert parse_compact_number("2万 点赞")["value"] == 20000
    assert parse_metric_group("2 Replies, 3 Reposts, 1.2K Likes")["likeCount"] == 1200
    assert relationship_state("alice-unfollow", "Following @alice") == "following"
    assert relationship_state("alice-follow", "Follow @alice") == "not-following"
    assert normalize_username(" @Alice ") == "alice"


@pytest.mark.asyncio
async def test_cancellation_is_structured():
    event = asyncio.Event()
    event.set()
    with pytest.raises(ActionError) as caught:
        await cancellable_sleep(10, event)
    assert caught.value.code == "USER_CANCELLED"
