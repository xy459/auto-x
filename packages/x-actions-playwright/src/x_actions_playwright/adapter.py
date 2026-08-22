from __future__ import annotations

import asyncio
import os
import random
import re
import sys
import time
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from urllib.parse import quote, urlparse
from weakref import WeakKeyDictionary
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .core import (
    cancellable_sleep,
    clamp_number,
    classify_page,
    hash_text,
    is_safe_username,
    normalize_username,
    parse_media_viewer_position,
    parse_metric_group,
    parse_schedule,
    parse_tweet_identity,
    relationship_state,
    totp_now,
)
from .errors import ActionError, normalize_error
from .models import ExecutionOptions

try:  # The package remains importable for catalog tooling before Playwright is installed.
    from playwright.async_api import Locator, Page
except ImportError:  # pragma: no cover
    Locator = Any  # type: ignore[misc,assignment]
    Page = Any  # type: ignore[misc,assignment]


POST_EXTRACT_JS = r"""
(article, options) => {
  const absolute = (value) => {
    if (!value) return null;
    try { return new URL(value, location.href).href; } catch { return String(value); }
  };
  const parseStatus = (href) => {
    const match = String(href || '').match(/^\/?([^/?#]+)\/status\/(\d+)/i);
    return match ? { username: match[1], postId: match[2], url: `https://x.com/${match[1]}/status/${match[2]}` } : null;
  };
  const owned = (node, postId) => {
    for (let current = node; current && current !== article; current = current.parentElement) {
      if (current.matches?.('[data-testid="quoteTweet"], [data-testid="card.wrapper"], [data-testid^="card.layout"]')) return false;
      if (current.matches?.('a[href*="/status/"]')) {
        const identity = parseStatus(current.getAttribute('href'));
        if (identity && identity.postId !== String(postId)) return false;
      }
    }
    return true;
  };
  const timeLink = article.querySelector('time')?.closest('a[href*="/status/"]');
  let identity = parseStatus(timeLink?.getAttribute('href'));
  if (!identity) {
    for (const link of article.querySelectorAll('a[href*="/status/"]')) {
      if (String(link.getAttribute('href')).includes('/analytics')) continue;
      identity = parseStatus(link.getAttribute('href'));
      if (identity) break;
    }
  }
  if (!identity) return null;
  const postId = identity.postId;
  const textNodes = [...article.querySelectorAll('[data-testid="tweetText"]')].filter(node => !node.closest('[data-testid="quoteTweet"]'));
  const textNode = textNodes[0] || null;
  const authorTexts = [...article.querySelectorAll(`a[href="/${identity.username}"]`)].filter(node => !node.closest('[data-testid="quoteTweet"]')).map(node => (node.textContent || '').trim()).filter(Boolean);
  const group = [...article.querySelectorAll('[role="group"][aria-label]')].find(node => !node.closest('[data-testid="quoteTweet"]') && /repl|like|view|bookmark|回复|点赞|浏览|收藏/i.test(node.getAttribute('aria-label') || ''));
  const photos = [...article.querySelectorAll('a[href*="/photo/"]')].filter(link => {
    if (!owned(link, postId)) return false;
    const parsed = parseStatus(link.getAttribute('href'));
    return parsed?.postId === postId;
  }).map(link => {
    const match = String(link.getAttribute('href')).match(/\/photo\/(\d+)/);
    const image = link.querySelector('img');
    return { index: Number(match?.[1] || 0), mediaPageUrl: absolute(link.getAttribute('href')), imageUrl: absolute(image?.currentSrc || image?.getAttribute('src')), alt: image?.getAttribute('alt') || null };
  }).filter(item => item.index).sort((a,b) => a.index-b.index);
  const videos = [...article.querySelectorAll('video')].filter(video => owned(video, postId)).map((video, index) => {
    const sourceUrls = [...new Set([video.currentSrc, video.getAttribute('src'), ...[...video.querySelectorAll('source')].map(source => source.getAttribute('src'))].filter(Boolean).map(absolute))];
    const persistentUrls = sourceUrls.filter(url => /^https?:/i.test(url));
    return { index:index+1, mediaPageUrl:identity.url, playbackUrl:sourceUrls[0] || null, sourceUrls, persistentUrls, posterUrl:absolute(video.getAttribute('poster')), temporaryPlaybackUrl:Boolean(sourceUrls[0] && /^(blob:|data:)/i.test(sourceUrls[0])), persistentMediaUrlAvailable:persistentUrls.length>0 };
  });
  const quoteNode = article.querySelector('[data-testid="quoteTweet"]');
  let quotedPost = null;
  if (quoteNode) {
    const qtime = quoteNode.querySelector('time')?.closest('a[href*="/status/"]');
    const qidentity = parseStatus(qtime?.getAttribute('href') || quoteNode.querySelector('a[href*="/status/"]')?.getAttribute('href'));
    const qtext = quoteNode.querySelector('[data-testid="tweetText"]');
    quotedPost = qidentity ? { available:true, postId:qidentity.postId, url:qidentity.url, author:{username:qidentity.username,handle:`@${qidentity.username}`,profileUrl:`https://x.com/${qidentity.username}`}, content:{text:qtext?.innerText || '',language:qtext?.getAttribute('lang') || null} } : { available:false, postId:null,url:null,author:null,reason:'quoted-post-identity-unavailable' };
  }
  const isAd = /\bAd\b|广告|推广/i.test(article.innerText || '');
  const contentText = textNode?.innerText || '';
  let emptyReason = null;
  if (!contentText.trim()) {
    emptyReason = photos.length ? 'image-only' : videos.length ? 'video-only' : quoteNode ? 'quote-only' : article.querySelector('[data-testid="poll"],[data-testid="cardPoll"]') ? 'poll-only' : article.querySelector('[data-testid="card.wrapper"]') ? 'link-card-only' : 'unknown-empty';
  }
  return {
    postId, url:identity.url, postType:quotedPost ? 'quote' : 'direct', isQuote:Boolean(quotedPost), isDirect:!quotedPost, isAd,
    author:{handle:authorTexts.find(t=>/^@/.test(t)) || `@${identity.username}`,username:identity.username,displayName:authorTexts.find(t=>t&&!t.startsWith('@')) || null,profileUrl:`https://x.com/${identity.username}`},
    content:{text:contentText,hasText:Boolean(contentText.trim()),emptyReason,createdAt:article.querySelector('time')?.getAttribute('datetime') || null,language:textNode?.getAttribute('lang') || null,hasMore:Boolean([...article.querySelectorAll('[data-testid="tweet-text-show-more-link"],button,[role="button"]')].find(n=>!n.closest('[data-testid="quoteTweet"]') && /show more|显示更多|展开/i.test((n.innerText||n.textContent||'').trim()))),complete:true},
    metricsLabel:group?.getAttribute('aria-label') || '',
    viewerInteraction:{liked:Boolean(article.querySelector(':scope [data-testid="unlike"]:not([data-testid="quoteTweet"] *)')),reposted:Boolean(article.querySelector(':scope [data-testid="unretweet"]:not([data-testid="quoteTweet"] *)')),bookmarked:Boolean(article.querySelector(':scope [data-testid="removeBookmark"]:not([data-testid="quoteTweet"] *)'))},
    media:{images:photos,videos,imageCount:photos.length,videoCount:videos.length},quotedPost,
    links:{postUrl:identity.url,imagePageUrls:photos.map(x=>x.mediaPageUrl),imageUrls:photos.map(x=>x.imageUrl),videoPageUrls:videos.map(x=>x.mediaPageUrl),videoPlaybackUrls:videos.flatMap(x=>x.sourceUrls),persistentVideoUrls:videos.flatMap(x=>x.persistentUrls),quotedPostUrl:quotedPost?.url || null}
  };
}
"""


def _normalized_media_path(value: object) -> str:
    return os.path.abspath(os.path.expanduser(str(value)))


class XAdapter:
    def __init__(self) -> None:
        self._selected_tweet_ids: WeakKeyDictionary[Any, str] = WeakKeyDictionary()
        self._fallback_selected_tweet_ids: dict[Any, str] = {}

    def get_selected_tweet_id(self, page: Page) -> str | None:
        try:
            return self._selected_tweet_ids.get(page)
        except TypeError:
            return self._fallback_selected_tweet_ids.get(page)

    def _set_selected_tweet_id(self, page: Page, tweet_id: str) -> None:
        try:
            self._selected_tweet_ids[page] = tweet_id
        except TypeError:
            self._fallback_selected_tweet_ids[page] = tweet_id

    async def dispatch(
        self,
        page: Page,
        handler: str,
        payload: dict[str, Any],
        options: ExecutionOptions,
    ) -> dict[str, Any]:
        method = getattr(self, handler, None)
        if not callable(method):
            raise ActionError("ACTION_UNSUPPORTED", f"No Playwright implementation for handler {handler}.")
        return cast(dict[str, Any], await method(page, payload, options))

    async def _click(
        self,
        locator: Locator,
        description: str,
        options: ExecutionOptions,
        *,
        mutation: bool = False,
    ) -> None:
        try:
            target = locator.first
            if not await target.count():
                raise ActionError("TARGET_NOT_FOUND", f"{description} was not found.")
            if not await target.is_visible():
                raise ActionError("ELEMENT_NOT_VISIBLE", f"{description} is not visible.")
            if not await target.is_enabled():
                raise ActionError("ELEMENT_DISABLED", f"{description} is disabled.")
            await target.scroll_into_view_if_needed(timeout=options.timeout_ms)
            if mutation:
                # Complete Playwright actionability checks without dispatching
                # an input event. From the following point a failed/cancelled
                # call may have triggered the external write.
                await target.click(timeout=options.timeout_ms, trial=True)
                options.trace.mark_mutation_triggered()
                # X frequently replaces write controls between consecutive
                # actionability checks. Re-running the full default click can
                # then wait for the stale transition until the action-wide
                # timeout. The trial above already proved the control is safe
                # to receive input, so dispatch the live click without another
                # long stability wait and leave time for postcondition checks.
                await target.click(
                    timeout=min(options.timeout_ms, 5_000),
                    force=True,
                )
            else:
                await target.click(timeout=options.timeout_ms)
        except ActionError:
            raise
        except Exception as error:
            normalized = normalize_error(error)
            if normalized.code == "UNEXPECTED_ERROR":
                normalized = ActionError("ELEMENT_BLOCKED", f"Could not click {description}: {error}", retryable=True)
            raise normalized from error

    def _tweet_locator(self, page: Page, tweet_id: str | None) -> Locator:
        target = str(tweet_id or self.get_selected_tweet_id(page) or "")
        articles = page.locator('article[data-testid="tweet"]')
        if not target:
            return articles.first
        return articles.filter(has=page.locator(f'a[href*="/status/{target}"] time')).first

    async def _require_tweet(self, page: Page, tweet_id: Any) -> Locator:
        article = self._tweet_locator(page, str(tweet_id or ""))
        if not await article.count():
            raise ActionError(
                "TARGET_NOT_FOUND",
                f"Post {tweet_id or self.get_selected_tweet_id(page) or ''} is not in the current DOM.",
            )
        return article

    async def _owned_control(self, article: Locator, test_ids: tuple[str, ...]) -> Locator:
        candidates = article.locator(",".join(f'[data-testid="{test_id}"]' for test_id in test_ids))
        for index in range(await candidates.count()):
            candidate = candidates.nth(index)
            owned = await candidate.evaluate("el => !el.closest('[data-testid=\"quoteTweet\"]')")
            if owned:
                return candidate
        return candidates.nth(10_000)

    async def _post(self, article: Locator, *, include_ads: bool = False) -> dict[str, Any]:
        raw = cast(dict[str, Any] | None, await article.evaluate(POST_EXTRACT_JS, {}))
        if not raw:
            raise ActionError("STATE_UNKNOWN", "Could not identify the selected post.")
        if raw.get("isAd") and not include_ads:
            raise ActionError("TARGET_UNSAFE", "Advertising posts are excluded by default.")
        raw["metrics"] = parse_metric_group(raw.pop("metricsLabel", ""))
        raw["content"]["complete"] = not raw["content"].get("hasMore")
        return raw

    async def _main_post_id(self, page: Page) -> str:
        identity = parse_tweet_identity(urlparse(page.url).path)
        if not identity:
            raise ActionError("PAGE_UNSUPPORTED", "Comment actions require a post detail page.")
        return identity["tweetId"]

    async def _comment_locators(self, page: Page) -> list[Locator]:
        root_id = await self._main_post_id(page)
        articles = page.locator('[data-testid="primaryColumn"] article[data-testid="tweet"]')
        result: list[Locator] = []
        for index in range(await articles.count()):
            article = articles.nth(index)
            post = await self._post(article, include_ads=True)
            if post["postId"] == root_id or post["isAd"]:
                continue
            text = await article.inner_text()
            if re.search(r"Discover more|Sourced from across X|发现更多|来自 X", text, re.I):
                break
            result.append(article)
        return result

    async def inspect(self, page: Page, payload: dict[str, Any], options: ExecutionOptions) -> dict[str, Any]:
        account = await self._account_session_data(page)
        tweets: list[dict[str, Any]] = []
        articles = page.locator('article[data-testid="tweet"]')
        for index in range(min(await articles.count(), 50)):
            try:
                post = await self._post(articles.nth(index), include_ads=True)
                tweets.append({"tweetId": post["postId"], "url": post["url"], "author": post["author"]["username"], "isAd": post["isAd"]})
            except ActionError:
                continue
        return {"status": "success", "context": {"pageType": classify_page(page.url), "url": page.url, "title": await page.title(), "account": account, "tweets": tweets, "selectedTweetId": self.get_selected_tweet_id(page)}}

    async def select_post(self, page: Page, payload: dict[str, Any], options: ExecutionOptions) -> dict[str, Any]:
        article = await self._require_tweet(page, payload.get("tweetId"))
        post = await self._post(article, include_ads=True)
        self._set_selected_tweet_id(page, post["postId"])
        await article.scroll_into_view_if_needed(timeout=options.timeout_ms)
        await article.evaluate("el => { el.dataset.xActionsSelected='true'; el.style.outline='3px solid #1d9bf0'; el.style.outlineOffset='3px'; }")
        return {"status": "success", "tweet": post}

    def _feed(self, payload: dict[str, Any], default: str | None = None) -> str:
        value = str(payload.get("feed") or default or "").strip().lower().replace("_", "-")
        if value == "foryou":
            value = "for-you"
        if value not in {"for-you", "following"}:
            raise ActionError("CONTENT_MISMATCH", "feed must be 'for-you' or 'following'.", {"feed": value})
        return value

    def _timeline_tab(self, page: Page, feed: str) -> Locator:
        pattern = re.compile(r"^(For you|为你推荐|推荐)$", re.I) if feed == "for-you" else re.compile(r"^(Following|正在关注|关注)$", re.I)
        return page.get_by_role("tab", name=pattern).first

    async def timeline_open(self, page: Page, payload: dict[str, Any], options: ExecutionOptions) -> dict[str, Any]:
        feed = self._feed(payload)
        if classify_page(page.url) != "home":
            await page.goto("https://x.com/home", wait_until="domcontentloaded", timeout=options.timeout_ms)
            return {"status": "navigating", "url": "https://x.com/home", "requiresRetry": True, "requestedTimeline": feed}
        tab = self._timeline_tab(page, feed)
        try:
            # X renders the Home tabs client-side after DOMContentLoaded. A task
            # page can therefore already be at /home while the requested tab is
            # still absent from the DOM. Wait for it instead of failing the
            # immediate retry performed by the task program.
            await tab.wait_for(state="visible", timeout=options.timeout_ms)
        except Exception as error:
            raise ActionError(
                "TARGET_NOT_FOUND",
                f"Could not find the {feed} Home tab.",
            ) from error
        if await tab.get_attribute("aria-selected") == "true":
            return {"status": "skipped", "reason": f"{feed} is already selected.", "timeline": feed, "evidence": ["aria-selected=true"]}
        await self._click(tab, f"{feed} Home tab", options)
        try:
            names = ["For you", "为你推荐", "推荐"] if feed == "for-you" else ["Following", "正在关注", "关注"]
            await page.wait_for_function("names => [...document.querySelectorAll('[role=tab]')].some(t => t.getAttribute('aria-selected') === 'true' && names.includes((t.innerText||t.textContent||'').trim()))", arg=names, timeout=options.timeout_ms)
        except Exception as error:
            current = self._timeline_tab(page, feed)
            if not await current.count() or await current.get_attribute("aria-selected") != "true":
                raise ActionError("TIMEOUT", f"Timed out verifying {feed} tab selection.", retryable=True) from error
        return {"status": "success", "timeline": feed, "evidence": ["aria-selected:false->true"]}

    async def timeline_browse(self, page: Page, payload: dict[str, Any], options: ExecutionOptions) -> dict[str, Any]:
        return await self._browse_timeline(page, payload, options, collect=False)

    async def timeline_collect(self, page: Page, payload: dict[str, Any], options: ExecutionOptions) -> dict[str, Any]:
        return await self._browse_timeline(page, payload, options, collect=True)

    def _show_new_posts_locator(self, page: Page) -> Locator:
        pattern = re.compile(r"^(Show|显示|查看).*(post|posts|帖子|条)", re.I)
        return page.get_by_role("button", name=pattern).or_(page.get_by_text(pattern)).first

    async def _click_show_new_posts_if_visible(self, page: Page, options: ExecutionOptions) -> bool:
        show = self._show_new_posts_locator(page)
        if not await show.count() or not await show.is_visible():
            return False
        await self._click(show, "Show new posts", options)
        return True

    async def timeline_refresh_new(self, page: Page, payload: dict[str, Any], options: ExecutionOptions) -> dict[str, Any]:
        feed = self._feed(payload)
        strategy = str(payload.get("strategy") or "tab_first").strip().lower().replace("-", "_")
        if strategy not in {"tab_first", "home_show", "none"}:
            raise ActionError("CONTENT_MISMATCH", "strategy must be tab_first, home_show, or none.")
        if classify_page(page.url) != "home":
            await page.goto("https://x.com/home", wait_until="domcontentloaded", timeout=options.timeout_ms)
            return {"status": "navigating", "url": "https://x.com/home", "requiresRetry": True, "strategy": strategy}
        settle_ms = int(clamp_number(payload.get("settleMs"), 2500, 500, 30000))
        evidence: list[str] = []
        clicked_show = False
        used_home = False
        clicked_tab = False

        if strategy == "none":
            return {"status": "skipped", "reason": "refresh-disabled", "strategy": strategy}

        if await self._click_show_new_posts_if_visible(page, options):
            clicked_show = True
            evidence.append("show-new-posts-visible-clicked")
        elif strategy == "tab_first":
            tab = self._timeline_tab(page, feed)
            await self._click(tab, f"{feed} Home tab refresh", options)
            clicked_tab = True
            evidence.append("timeline-tab-clicked")
            await cancellable_sleep(settle_ms, options.cancellation)
            if await self._click_show_new_posts_if_visible(page, options):
                clicked_show = True
                evidence.append("show-new-posts-after-tab-clicked")

        if not clicked_show and (strategy == "home_show" or payload.get("homeFallback")):
            before = int(await page.evaluate("window.scrollY"))
            await page.keyboard.press("Home")
            used_home = True
            evidence.append(f"home-key:{before}->top")
            await cancellable_sleep(settle_ms, options.cancellation)
            if await self._click_show_new_posts_if_visible(page, options):
                clicked_show = True
                evidence.append("show-new-posts-after-home-clicked")

        return {
            "status": "success" if clicked_show or clicked_tab or used_home else "skipped",
            "strategy": strategy,
            "feed": feed,
            "clickedShowNewPosts": clicked_show,
            "clickedTimelineTab": clicked_tab,
            "usedHomeKey": used_home,
            "evidence": evidence or ["no-visible-new-posts-control"],
        }

    async def _browse_timeline(self, page: Page, payload: dict[str, Any], options: ExecutionOptions, *, collect: bool) -> dict[str, Any]:
        feed = self._feed(payload)
        opened = await self.timeline_open(page, {"feed": feed}, options)
        if opened["status"] == "navigating":
            return {**opened, "phase": "open-home"}
        duration_ms = int(clamp_number(payload.get("durationMs"), 6000, 0, 60000))
        interval_ms = int(clamp_number(payload.get("intervalMs"), 1500, 250, 10000))
        distance = int(clamp_number(payload.get("distance"), 650, 200, 3000))
        max_scrolls = int(clamp_number(payload.get("maxScrolls"), 20, 0, 100))
        max_posts = int(clamp_number(payload.get("maxPosts"), 200, 1, 1000))
        include_ads = payload.get("includeAds", True) is not False
        if payload.get("resetToTop"):
            await page.evaluate("window.scrollTo({top:0,behavior:'auto'})")
        start_y = int(await page.evaluate("window.scrollY"))
        positions = [start_y]
        posts: dict[str, dict[str, Any]] = {}
        duplicates = filtered_ads = unidentified = 0

        async def sample() -> None:
            nonlocal duplicates, filtered_ads, unidentified
            articles = page.locator('[data-testid="primaryColumn"] article[data-testid="tweet"]')
            for index in range(await articles.count()):
                if len(posts) >= max_posts:
                    break
                try:
                    post = await self._post(articles.nth(index), include_ads=True)
                except ActionError:
                    unidentified += 1
                    continue
                if post["postId"] in posts:
                    duplicates += 1
                    continue
                if post["isAd"] and not include_ads:
                    filtered_ads += 1
                    continue
                post["collection"] = {"order": len(posts) + 1, "timeline": feed, "scrollY": int(await page.evaluate("window.scrollY")), "firstSeenAt": datetime.now(UTC).isoformat()}
                posts[post["postId"]] = post

        if collect:
            await sample()
        loop = asyncio.get_running_loop()
        started = loop.time()
        scrolls = 0
        at_boundary = False
        while (loop.time() - started) * 1000 < duration_ms and scrolls < max_scrolls and len(posts) < max_posts:
            await cancellable_sleep(min(interval_ms, max(0, duration_ms - int((loop.time() - started) * 1000))), options.cancellation)
            if (loop.time() - started) * 1000 >= duration_ms:
                break
            before = int(await page.evaluate("window.scrollY"))
            await page.mouse.wheel(0, distance)
            await cancellable_sleep(150 if payload.get("behavior") != "smooth" else 450, options.cancellation)
            after = int(await page.evaluate("window.scrollY"))
            maximum = int(await page.evaluate("Math.max(0, document.documentElement.scrollHeight-innerHeight)"))
            positions.append(after)
            at_boundary = after >= maximum
            if collect:
                await sample()
            if after == before:
                break
            scrolls += 1
            if at_boundary:
                break
        current = self._timeline_tab(page, feed)
        if not await current.count() or await current.get_attribute("aria-selected") != "true":
            raise ActionError("STATE_UNKNOWN", f"The {feed} timeline stopped being selected.", retryable=True)
        data: dict[str, Any] = {"status": "success", "timeline": feed, "requestedDurationMs": duration_ms, "actualDurationMs": round((loop.time() - started) * 1000), "intervalMs": interval_ms, "distance": distance, "scrolls": scrolls, "startY": start_y, "endY": int(await page.evaluate("window.scrollY")), "atBoundary": at_boundary, "positions": positions, "evidence": ["timeline-selected", f"scroll-count:{scrolls}"]}
        if collect:
            data.update({"posts": list(posts.values()), "collectedCount": len(posts), "duplicateCount": duplicates, "unidentifiedCount": unidentified, "filteredAdCount": filtered_ads, "includeAds": include_ads, "maxPosts": max_posts, "limitReached": len(posts) >= max_posts, "checkpoint": {"feed": feed, "scrollY": data["endY"], "lastPostId": next(reversed(posts), None), "collectedCount": len(posts), "createdAt": datetime.now(UTC).isoformat()}})
        return data

    async def post_open_details(self, page: Page, payload: dict[str, Any], options: ExecutionOptions) -> dict[str, Any]:
        article = await self._require_tweet(page, payload.get("tweetId"))
        post = await self._post(article)
        if page.url.rstrip("/") == post["url"].rstrip("/"):
            return {"status": "skipped", "reason": "Post detail is already open.", "url": post["url"]}
        await page.goto(post["url"], wait_until="domcontentloaded", timeout=options.timeout_ms)
        return {"status": "navigating", "url": post["url"], "requiresRetry": False}

    async def post_get_details(self, page: Page, payload: dict[str, Any], options: ExecutionOptions) -> dict[str, Any]:
        article = await self._require_tweet(page, payload.get("tweetId"))
        return {"status": "success", "post": await self._post(article, include_ads=bool(payload.get("includeAds")))}

    async def post_get_type(self, page: Page, payload: dict[str, Any], options: ExecutionOptions) -> dict[str, Any]:
        post = (await self.post_get_details(page, payload, options))["post"]
        return {"status": "success", "postId": post["postId"], "url": post["url"], "type": post["postType"], "isQuote": post["isQuote"], "isDirect": post["isDirect"], "quotedPost": post["quotedPost"]}

    async def post_get_url(self, page: Page, payload: dict[str, Any], options: ExecutionOptions) -> dict[str, Any]:
        post = (await self.post_get_details(page, payload, options))["post"]
        return {"status": "success", "url": post["url"], "tweet": post}

    async def post_expand(self, page: Page, payload: dict[str, Any], options: ExecutionOptions) -> dict[str, Any]:
        article = await self._require_tweet(page, payload.get("tweetId"))
        before = await self._post(article, include_ads=True)
        candidates = article.locator('[data-testid="tweet-text-show-more-link"], button, [role="button"]')
        target: Locator | None = None
        for index in range(await candidates.count()):
            item = candidates.nth(index)
            text = (await item.inner_text()).strip()
            owned = await item.evaluate("el => !el.closest('[data-testid=\"quoteTweet\"], [data-testid=\"card.wrapper\"]')")
            if owned and re.search(r"show more|显示更多|展开", text, re.I):
                target = item
                break
        if target is None:
            return {"status": "skipped", "reason": "Post has no main-text Show more control.", "target": before}
        await self._click(target, "Show more", options)
        try:
            await page.wait_for_function("([id,before]) => { const a=[...document.querySelectorAll('article[data-testid=\"tweet\"]')].find(x=>x.querySelector(`a[href*=\"/status/${id}\"] time`)); const t=[...a?.querySelectorAll('[data-testid=\"tweetText\"]')||[]].find(n=>!n.closest('[data-testid=\"quoteTweet\"]')); return (t?.innerText||'').length > before; }", arg=[before["postId"], len(before["content"]["text"])], timeout=options.timeout_ms)
        except Exception as error:
            raise ActionError("TIMEOUT", "Show more was clicked but the post text did not expand.", retryable=True) from error
        return {"status": "success", "post": await self._post(article, include_ads=True), "evidence": ["tweet-text-expanded"]}

    async def post_copy_link(self, page: Page, payload: dict[str, Any], options: ExecutionOptions) -> dict[str, Any]:
        article = await self._require_tweet(page, payload.get("tweetId"))
        share = await self._owned_control(article, ("share",))
        await self._click(share, "post share button", options)
        await cancellable_sleep(int(clamp_number(payload.get("menuDelayMs"), 1500, 0, 5000)), options.cancellation)
        menu = page.get_by_role("menuitem", name=re.compile(r"^(Copy link|复制链接|複製連結)$", re.I))
        if not await menu.count():
            menu = page.locator('[data-testid="copyLink"], [data-testid="copy-link"]')
        await self._click(menu, "Copy link menu item", options)
        return {"status": "success", "evidence": ["native-copy-link-clicked"]}

    async def post_delete(self, page: Page, payload: dict[str, Any], options: ExecutionOptions) -> dict[str, Any]:
        article = await self._require_tweet(page, payload.get("tweetId"))
        post = await self._post(article)
        tweet_id = str(post["postId"])
        session = await self._account_session_data(page)
        if not session.get("username"):
            raise ActionError("STATE_UNKNOWN", "Could not identify the signed-in account before deleting.")
        if normalize_username(session["username"]) != normalize_username(post["author"]["username"]):
            raise ActionError("TARGET_UNSAFE", "Only posts owned by the signed-in account can be deleted.")
        if options.dry_run:
            return {"status": "success", "dryRun": True, "wouldExecute": "post.delete", "target": post}
        more = article.locator('[data-testid="caret"]')
        await self._click(more, "post More menu", options)
        delete_item = page.get_by_role("menuitem", name=re.compile(r"^(Delete|删除|删除帖子|刪除|刪除貼文)$", re.I))
        await self._click(delete_item, "Delete menu item", options)
        confirmation = page.locator('[data-testid="confirmationSheetConfirm"]')
        if not await confirmation.count():
            confirmation = page.get_by_role("dialog").get_by_role("button", name=re.compile(r"^(Delete|删除|刪除)$", re.I))
        await self._click(confirmation, "Delete confirmation", options, mutation=True)
        try:
            await article.wait_for(state="detached", timeout=min(options.timeout_ms, 10_000))
            return {"status": "success", "tweetId": str(tweet_id), "deleted": True, "evidence": ["post-removed"]}
        except Exception as error:
            toast = page.locator('[data-testid="toast"]')
            text = await toast.inner_text() if await toast.count() else ""
            if re.search(r"failed|error|try again|出错|失败", text, re.I):
                raise ActionError("SUBMISSION_REJECTED", "X rejected the post deletion.", {"toast": text}) from error
            return {"status": "uncertain", "tweetId": str(tweet_id), "deleted": None, "reason": "Delete confirmation was clicked but the final state could not be proven. Do not retry automatically."}

    async def post_exit_details(self, page: Page, payload: dict[str, Any], options: ExecutionOptions) -> dict[str, Any]:
        if classify_page(page.url) != "tweet-detail":
            return {"status": "skipped", "reason": "Current page is not a post detail route."}
        back = page.locator('[data-testid="app-bar-back"]')
        if await back.count():
            await self._click(back, "Back", options)
        else:
            await page.go_back(wait_until="domcontentloaded", timeout=options.timeout_ms)
        if urlparse(page.url).hostname not in {"x.com", "www.x.com"}:
            raise ActionError("PAGE_UNSUPPORTED", "Exiting details would leave x.com.")
        return {"status": "success", "url": page.url}

    async def image_open(self, page: Page, payload: dict[str, Any], options: ExecutionOptions) -> dict[str, Any]:
        article = await self._require_tweet(page, payload.get("tweetId"))
        tweet_id = str((await self._post(article, include_ads=True))["postId"])
        links = article.locator(f'a[href*="/status/{tweet_id}/photo/"]')
        owned: list[Locator] = []
        for index in range(await links.count()):
            link = links.nth(index)
            if await link.evaluate("el => !el.closest('[data-testid=\"quoteTweet\"], [data-testid=\"card.wrapper\"]')"):
                owned.append(link)
        requested = int(clamp_number(payload.get("index"), 1, 1, max(1, len(owned))))
        if not owned:
            raise ActionError("TARGET_NOT_FOUND", "The selected post has no owned image.")
        if requested > len(owned):
            raise ActionError("CONTENT_MISMATCH", "Requested image index is out of range.")
        await self._click(owned[requested - 1], f"image {requested}", options)
        try:
            await page.wait_for_url(re.compile(r"/status/\d+/photo/\d+"), timeout=options.timeout_ms)
        except Exception as error:
            raise ActionError("TIMEOUT", "Image click did not open X's media viewer.", retryable=True) from error
        return {"status": "success", "viewer": parse_media_viewer_position(page.url)}

    async def _navigate_image(self, page: Page, options: ExecutionOptions, direction: str) -> dict[str, Any]:
        before = parse_media_viewer_position(page.url)
        if not before:
            raise ActionError("STATE_UNKNOWN", "Current page is not an X image viewer.")
        name = re.compile(r"(Previous|上一张|前一张)", re.I) if direction == "previous" else re.compile(r"(Next|下一张|后一张)", re.I)
        button = page.get_by_role("button", name=name)
        if not await button.count() or not await button.first.is_visible() or not await button.first.is_enabled():
            return {"status": "skipped", "reason": f"Already at the {direction} image boundary.", "viewer": before}
        await self._click(button, f"{direction} image", options)
        try:
            await page.wait_for_function(r"([old,direction]) => { const m=location.pathname.match(/\/photo\/(\d+)/); if(!m)return false; const n=Number(m[1]); return direction==='previous'?n<old:n>old; }", arg=[before["index"], direction], timeout=options.timeout_ms)
        except Exception as error:
            raise ActionError("TIMEOUT", f"The viewer did not move {direction}.", retryable=True) from error
        return {"status": "success", "viewer": parse_media_viewer_position(page.url)}

    async def image_previous(self, page: Page, payload: dict[str, Any], options: ExecutionOptions) -> dict[str, Any]:
        return await self._navigate_image(page, options, "previous")

    async def image_next(self, page: Page, payload: dict[str, Any], options: ExecutionOptions) -> dict[str, Any]:
        return await self._navigate_image(page, options, "next")

    async def image_close(self, page: Page, payload: dict[str, Any], options: ExecutionOptions) -> dict[str, Any]:
        if classify_page(page.url) != "media-viewer":
            return {"status": "skipped", "reason": "Current page is not a media viewer."}
        close = page.locator('[data-testid="app-bar-close"], [data-testid="close"]')
        if not await close.count():
            close = page.get_by_role("button", name=re.compile(r"^(Close|关闭|關閉)$", re.I))
        await self._click(close, "media viewer close", options)
        return {"status": "success", "url": page.url}

    async def _video(self, page: Page, payload: dict[str, Any]) -> tuple[Locator, Locator, dict[str, Any]]:
        article = await self._require_tweet(page, payload.get("tweetId"))
        post = await self._post(article)
        videos = article.locator("video")
        for index in range(await videos.count()):
            video = videos.nth(index)
            if await video.evaluate("el => !el.closest('[data-testid=\"quoteTweet\"], [data-testid^=\"card.\"]')"):
                return article, video, post
        raise ActionError("TARGET_NOT_FOUND", "The selected post has no owned video.")

    async def _set_video_property(self, page: Page, payload: dict[str, Any], options: ExecutionOptions, prop: str, desired: bool) -> dict[str, Any]:
        _, video, post = await self._video(page, payload)
        current = bool(await video.evaluate(f"el => Boolean(el.{prop})"))
        if current == desired:
            return {"status": "skipped", "reason": f"Video {prop} is already {desired}.", "target": post}
        try:
            if prop == "paused":
                await video.evaluate("(el, play) => play ? el.play() : el.pause()", not desired)
            else:
                await video.evaluate("(el, muted) => { el.muted = muted; }", desired)
            await page.wait_for_function("([video,prop,desired]) => Boolean(video[prop]) === desired", arg=[await video.element_handle(), prop, desired], timeout=options.timeout_ms)
        except Exception as error:
            raise ActionError("TIMEOUT", f"Video {prop} did not reach {desired}.", retryable=True) from error
        return {"status": "success", "target": post, "evidence": [f"{prop}={str(desired).lower()}"]}

    async def video_play(self, page: Page, payload: dict[str, Any], options: ExecutionOptions) -> dict[str, Any]:
        return await self._set_video_property(page, payload, options, "paused", False)

    async def video_pause(self, page: Page, payload: dict[str, Any], options: ExecutionOptions) -> dict[str, Any]:
        return await self._set_video_property(page, payload, options, "paused", True)

    async def video_unmute(self, page: Page, payload: dict[str, Any], options: ExecutionOptions) -> dict[str, Any]:
        return await self._set_video_property(page, payload, options, "muted", False)

    async def video_mute(self, page: Page, payload: dict[str, Any], options: ExecutionOptions) -> dict[str, Any]:
        return await self._set_video_property(page, payload, options, "muted", True)

    async def _comment_data(self, article: Locator, root_id: str) -> dict[str, Any]:
        post = await self._post(article, include_ads=True)
        text = await article.inner_text()
        line = next((line for line in text.splitlines() if re.search(r"Replying to|回复给|正在回复", line, re.I)), "")
        handles = list(dict.fromkeys(f"@{item}" for item in re.findall(r"@([A-Za-z0-9_]{1,15})", line)))
        return {
            "commentId": post["postId"],
            "postId": root_id,
            "parentCommentId": None,
            "isAd": post["isAd"],
            "author": post["author"],
            "content": post["content"] | {"url": post["url"], "media": {"imageCount": post["media"]["imageCount"], "hasVideo": post["media"]["videoCount"] > 0}},
            "metrics": post["metrics"],
            "viewerInteraction": post["viewerInteraction"],
            "replyContext": {"replyingToHandles": handles, "depth": 1, "source": "visible-reply-context" if handles else "conversation-root-inferred"},
            "capabilities": {
                "canLike": bool(await (await self._owned_control(article, ("like", "unlike"))).count()),
                "canUnlike": bool(await (await self._owned_control(article, ("unlike",))).count()),
                "canReply": bool(await (await self._owned_control(article, ("reply",))).count()),
                "canRepost": bool(await (await self._owned_control(article, ("retweet", "unretweet"))).count()),
                "canQuote": bool(await (await self._owned_control(article, ("retweet", "unretweet"))).count()),
            },
        }

    async def comment_list_visible(self, page: Page, payload: dict[str, Any], options: ExecutionOptions) -> dict[str, Any]:
        root_id = await self._main_post_id(page)
        comments = []
        for article in await self._comment_locators(page):
            if await article.is_visible():
                comments.append(await self._comment_data(article, root_id))
        return {"status": "success", "rootPostId": root_id, "comments": comments, "count": len(comments)}

    async def comment_collect(self, page: Page, payload: dict[str, Any], options: ExecutionOptions) -> dict[str, Any]:
        root_id = await self._main_post_id(page)
        max_scrolls = int(clamp_number(payload.get("maxScrolls"), 12, 0, 50))
        max_comments = int(clamp_number(payload.get("maxComments"), 200, 1, 5000))
        distance = int(clamp_number(payload.get("distance"), 700, 200, 3000))
        settle_ms = int(clamp_number(payload.get("settleMs"), 650, 250, 3000))
        collected: dict[str, dict[str, Any]] = {}
        stagnant = 0
        scrolls = 0
        stop_reason = "max-scrolls"
        for scrolls in range(max_scrolls + 1):
            before = len(collected)
            for article in await self._comment_locators(page):
                comment = await self._comment_data(article, root_id)
                collected.setdefault(comment["commentId"], comment)
                if len(collected) >= max_comments:
                    stop_reason = "max-comments"
                    break
            if len(collected) >= max_comments:
                break
            stagnant = stagnant + 1 if len(collected) == before else 0
            if stagnant >= int(clamp_number(payload.get("maxStagnantRounds"), 4, 2, 10)):
                stop_reason = "stagnant"
                break
            if scrolls == max_scrolls:
                break
            before_y = int(await page.evaluate("window.scrollY"))
            await page.mouse.wheel(0, distance)
            await cancellable_sleep(settle_ms, options.cancellation)
            after_y = int(await page.evaluate("window.scrollY"))
            if after_y == before_y:
                stop_reason = "scroll-boundary"
                break
        return {"status": "success", "rootPostId": root_id, "comments": list(collected.values()), "collectedCount": len(collected), "scrolls": scrolls, "stopReason": stop_reason}

    async def comment_get(self, page: Page, payload: dict[str, Any], options: ExecutionOptions) -> dict[str, Any]:
        comment_id = str(payload.get("commentId") or "")
        root_id = await self._main_post_id(page)
        if not comment_id or comment_id == root_id:
            raise ActionError("TARGET_NOT_FOUND", "commentId must identify a visible comment, not the root post.")
        for article in await self._comment_locators(page):
            post = await self._post(article, include_ads=True)
            if post["postId"] == comment_id:
                return {"status": "success", "comment": await self._comment_data(article, root_id)}
        raise ActionError("TARGET_NOT_FOUND", f"Comment {comment_id} is not visible before Discover more.")

    async def _comment_article(self, page: Page, comment_id: Any, options: ExecutionOptions) -> Locator:
        await self.comment_get(page, {"commentId": comment_id}, options)
        return await self._require_tweet(page, comment_id)

    async def _state_action(
        self,
        page: Page,
        article: Locator,
        options: ExecutionOptions,
        *,
        action: str,
        current_test_id: str,
        desired_test_id: str,
        click_test_ids: tuple[str, ...],
        menu_confirm: Locator | None = None,
    ) -> dict[str, Any]:
        post = await self._post(article, include_ads=True)
        if await (await self._owned_control(article, (desired_test_id,))).count():
            return {"status": "skipped", "reason": f"Target already satisfies {action}.", "target": post}
        if options.dry_run:
            return {"status": "success", "dryRun": True, "wouldExecute": action, "target": post}
        control = await self._owned_control(article, click_test_ids)
        click_error: ActionError | None = None
        try:
            await self._click(
                control,
                f"{action} button",
                options,
                mutation=menu_confirm is None,
            )
            if menu_confirm is not None:
                await self._click(menu_confirm, f"{action} confirmation", options, mutation=True)
        except ActionError as error:
            if not options.trace.mutation_triggered:
                raise
            click_error = error
        try:
            await (await self._owned_control(article, (desired_test_id,))).wait_for(state="visible", timeout=min(options.timeout_ms, 7000))
            evidence = [f"state:{current_test_id}->{desired_test_id}"]
            if click_error is not None:
                evidence.append(f"click-error-recovered:{click_error.code}")
            return {"status": "success", "target": await self._post(article, include_ads=True), "evidence": evidence}
        except Exception:
            result: dict[str, Any] = {
                "status": "uncertain",
                "target": post,
                "reason": f"{action} was clicked but the final target state was not observed. Do not retry automatically.",
            }
            if click_error is not None:
                result["clickError"] = click_error.to_dict()
            return result

    async def comment_like(self, page: Page, payload: dict[str, Any], options: ExecutionOptions) -> dict[str, Any]:
        article = await self._comment_article(page, payload.get("commentId"), options)
        return await self._state_action(page, article, options, action="comment.like", current_test_id="like", desired_test_id="unlike", click_test_ids=("like",))

    async def comment_unlike(self, page: Page, payload: dict[str, Any], options: ExecutionOptions) -> dict[str, Any]:
        article = await self._comment_article(page, payload.get("commentId"), options)
        return await self._state_action(page, article, options, action="comment.unlike", current_test_id="unlike", desired_test_id="like", click_test_ids=("unlike",))

    async def _open_fresh_composer(self, page: Page, trigger: Locator, options: ExecutionOptions, *, kind: str) -> Locator:
        selector = '[data-testid="tweetTextarea_0"][contenteditable="true"]'
        snapshot_key = f"{id(self)}:{time.monotonic_ns()}"
        await page.evaluate(
            """
            ({ selector, key }) => {
              const snapshots = globalThis.__autoXComposerSnapshots
                ?? (globalThis.__autoXComposerSnapshots = new Map());
              const state = new WeakMap();
              for (const editor of document.querySelectorAll(selector)) {
                const style = getComputedStyle(editor);
                state.set(editor, {
                  visible: style.visibility !== 'hidden'
                    && style.display !== 'none'
                    && editor.getClientRects().length > 0,
                  inDialog: Boolean(editor.closest('[role="dialog"], [aria-modal="true"]')),
                });
              }
              snapshots.set(key, state);
            }
            """,
            {"selector": selector, "key": snapshot_key},
        )
        try:
            await self._click(trigger, f"{kind} trigger", options)
            deadline = asyncio.get_running_loop().time() + options.timeout_ms / 1000
            while asyncio.get_running_loop().time() < deadline:
                modal_editors = page.locator(
                    f'[role="dialog"] {selector}, [aria-modal="true"] {selector}'
                )
                all_editors = page.locator(selector)
                for candidates in (modal_editors, all_editors):
                    for index in range(await candidates.count() - 1, -1, -1):
                        editor = candidates.nth(index)
                        if not await editor.is_visible():
                            continue
                        is_fresh = await editor.evaluate(
                            """
                            (element, key) => {
                              const previous = globalThis.__autoXComposerSnapshots
                                ?.get(key)?.get(element);
                              const nowInDialog = Boolean(
                                element.closest('[role="dialog"], [aria-modal="true"]')
                              );
                              return !previous?.visible
                                || (nowInDialog && !previous?.inDialog);
                            }
                            """,
                            snapshot_key,
                        )
                        if is_fresh:
                            return editor
                await cancellable_sleep(100, options.cancellation)
        except ActionError:
            raise
        except Exception as error:
            raise ActionError(
                "TARGET_NOT_FOUND", f"A visible {kind} composer did not open."
            ) from error
        finally:
            try:
                await page.evaluate(
                    "key => globalThis.__autoXComposerSnapshots?.delete(key)",
                    snapshot_key,
                )
            except Exception:
                pass
        raise ActionError("TARGET_NOT_FOUND", f"A visible {kind} composer did not open.")

    async def _prepare_composer(self, page: Page, editor: Locator, text: str, options: ExecutionOptions) -> tuple[Locator, Locator]:
        if not text.strip():
            raise ActionError("CONTENT_MISMATCH", "Composer text is empty.")
        try:
            await editor.wait_for(state="visible", timeout=options.timeout_ms)
        except Exception as error:
            raise ActionError(
                "ELEMENT_NOT_VISIBLE", "The selected composer editor is not visible."
            ) from error
        existing = (await editor.inner_text()).strip()
        if existing:
            raise ActionError("DRAFT_CONFLICT", "Refusing to overwrite an existing draft.", {"existingHash": hash_text(existing), "existingLength": len(existing)})
        await editor.click(timeout=options.timeout_ms)
        await editor.press_sequentially(text, delay=0, timeout=options.timeout_ms)
        actual = (await editor.inner_text()).replace("\r\n", "\n").strip()
        expected = text.replace("\r\n", "\n").strip()
        if not actual and expected:
            await editor.fill(text, timeout=options.timeout_ms)
            actual = (await editor.inner_text()).replace("\r\n", "\n").strip()
        if actual != expected:
            raise ActionError("CONTENT_MISMATCH", "Editor content does not match requested text.", {"expectedHash": hash_text(text), "actualHash": hash_text(actual)})
        surface = editor.locator("xpath=ancestor::*[@role='dialog' or @aria-modal='true'][1]")
        if not await surface.count():
            surface = editor.locator("xpath=ancestor::*[.//*[@data-testid='tweetButton' or @data-testid='tweetButtonInline']][1]")
        if not await surface.count():
            raise ActionError("STATE_UNKNOWN", "Could not bind the composer to its local surface.")
        button = surface.locator(
            '[data-testid="tweetButton"]:visible, [data-testid="tweetButtonInline"]:visible'
        )
        try:
            await button.first.wait_for(state="visible", timeout=options.timeout_ms)
            if not await button.first.is_enabled():
                raise ActionError("CONTENT_MISMATCH", "X displayed the text but did not enable the local submit button.")
        except ActionError:
            raise
        except Exception as error:
            raise ActionError("TARGET_NOT_FOUND", "Composer submit button was not found in the selected surface.") from error
        return surface, button.first

    async def _submit_composer(self, page: Page, editor: Locator, button: Locator, text: str, options: ExecutionOptions, action: str) -> dict[str, Any]:
        if options.dry_run:
            return {"status": "success", "dryRun": True, "wouldExecute": action, "contentHash": hash_text(text)}
        await self._click(button, f"{action} submit", options, mutation=True)
        toast = page.locator('[data-testid="toast"]')
        try:
            await asyncio.sleep(0.2)
            if await toast.count() and await toast.first.is_visible():
                toast_text = (await toast.first.inner_text()).strip()
                if re.search(r"failed|error|try again|出错|失败", toast_text, re.I):
                    raise ActionError("SUBMISSION_REJECTED", f"X rejected {action}.", {"toast": toast_text})
                if re.search(r"posted|sent|已发布|已发送", toast_text, re.I):
                    return {"status": "success", "contentHash": hash_text(text), "evidence": ["success-toast"]}
            if not await editor.count() or not (await editor.inner_text()).strip():
                return {"status": "uncertain", "contentHash": hash_text(text), "reason": "The editor cleared without a reliable X success signal. Do not retry automatically."}
        except ActionError:
            raise
        except Exception:
            pass
        return {"status": "uncertain", "contentHash": hash_text(text), "reason": "The final submit control was clicked but X did not expose a reliable final state. Do not retry automatically."}

    async def comment_reply(self, page: Page, payload: dict[str, Any], options: ExecutionOptions) -> dict[str, Any]:
        article = await self._comment_article(page, payload.get("commentId"), options)
        text = str(payload.get("text") or "")
        trigger = await self._owned_control(article, ("reply",))
        if options.dry_run:
            if not text.strip():
                raise ActionError("CONTENT_MISMATCH", "Composer text is empty.")
            if not await trigger.count():
                raise ActionError("TARGET_NOT_FOUND", "Comment reply control was not found.")
            return {"status": "success", "dryRun": True, "wouldExecute": "comment.reply", "contentHash": hash_text(text)}
        editor = await self._open_fresh_composer(page, trigger, options, kind="comment reply")
        _, button = await self._prepare_composer(page, editor, text, options)
        return await self._submit_composer(page, editor, button, text, options, "comment.reply")

    async def _quote(self, page: Page, article: Locator, text: str, options: ExecutionOptions, action: str) -> dict[str, Any]:
        repost = await self._owned_control(article, ("retweet", "unretweet"))
        if options.dry_run:
            if not text.strip():
                raise ActionError("CONTENT_MISMATCH", "Composer text is empty.")
            if not await repost.count():
                raise ActionError("TARGET_NOT_FOUND", "Quote control was not found.")
            return {"status": "success", "dryRun": True, "wouldExecute": action, "contentHash": hash_text(text)}
        await self._click(repost, f"{action} repost menu", options)
        quote_item = page.get_by_role("menuitem", name=re.compile(r"^(Quote|引用|引用帖子)$", re.I))
        editor = await self._open_fresh_composer(page, quote_item, options, kind=action)
        _, button = await self._prepare_composer(page, editor, text, options)
        return await self._submit_composer(page, editor, button, text, options, action)

    async def comment_quote(self, page: Page, payload: dict[str, Any], options: ExecutionOptions) -> dict[str, Any]:
        return await self._quote(page, await self._comment_article(page, payload.get("commentId"), options), str(payload.get("text") or ""), options, "comment.quote")

    async def comment_delete_reply(self, page: Page, payload: dict[str, Any], options: ExecutionOptions) -> dict[str, Any]:
        if not payload.get("replyId"):
            raise ActionError("TARGET_NOT_FOUND", "comment.deleteReply requires replyId.")
        return await self.post_delete(page, {**payload, "tweetId": payload["replyId"]}, options)

    async def interaction_reply(self, page: Page, payload: dict[str, Any], options: ExecutionOptions) -> dict[str, Any]:
        article = await self._require_tweet(page, payload.get("tweetId"))
        text = str(payload.get("text") or "")
        trigger = await self._owned_control(article, ("reply",))
        if options.dry_run:
            if not text.strip():
                raise ActionError("CONTENT_MISMATCH", "Composer text is empty.")
            if not await trigger.count():
                raise ActionError("TARGET_NOT_FOUND", "Post reply control was not found.")
            return {"status": "success", "dryRun": True, "wouldExecute": "interaction.reply", "contentHash": hash_text(text)}
        editor = await self._open_fresh_composer(page, trigger, options, kind="post reply")
        _, button = await self._prepare_composer(page, editor, text, options)
        return await self._submit_composer(page, editor, button, text, options, "interaction.reply")

    async def interaction_quote(self, page: Page, payload: dict[str, Any], options: ExecutionOptions) -> dict[str, Any]:
        return await self._quote(page, await self._require_tweet(page, payload.get("tweetId")), str(payload.get("text") or ""), options, "interaction.quote")

    async def interaction_like(self, page: Page, payload: dict[str, Any], options: ExecutionOptions) -> dict[str, Any]:
        return await self._state_action(page, await self._require_tweet(page, payload.get("tweetId")), options, action="interaction.like", current_test_id="like", desired_test_id="unlike", click_test_ids=("like",))

    async def interaction_unlike(self, page: Page, payload: dict[str, Any], options: ExecutionOptions) -> dict[str, Any]:
        return await self._state_action(page, await self._require_tweet(page, payload.get("tweetId")), options, action="interaction.unlike", current_test_id="unlike", desired_test_id="like", click_test_ids=("unlike",))

    async def interaction_bookmark(self, page: Page, payload: dict[str, Any], options: ExecutionOptions) -> dict[str, Any]:
        return await self._state_action(page, await self._require_tweet(page, payload.get("tweetId")), options, action="interaction.bookmark", current_test_id="bookmark", desired_test_id="removeBookmark", click_test_ids=("bookmark",))

    async def interaction_repost(self, page: Page, payload: dict[str, Any], options: ExecutionOptions) -> dict[str, Any]:
        article = await self._require_tweet(page, payload.get("tweetId"))
        confirm = page.locator('[data-testid="retweetConfirm"]')
        if not await confirm.count():
            confirm = page.get_by_role("menuitem", name=re.compile(r"^(Repost|转推|转帖)$", re.I))
        return await self._state_action(page, article, options, action="interaction.repost", current_test_id="retweet", desired_test_id="unretweet", click_test_ids=("retweet",), menu_confirm=confirm)

    async def interaction_undo_repost(self, page: Page, payload: dict[str, Any], options: ExecutionOptions) -> dict[str, Any]:
        article = await self._require_tweet(page, payload.get("tweetId"))
        confirm = page.locator('[data-testid="unretweetConfirm"]')
        if not await confirm.count():
            confirm = page.get_by_role("menuitem", name=re.compile(r"^(Undo repost|撤销转推|取消转推|撤销转帖|取消转帖)$", re.I))
        return await self._state_action(page, article, options, action="interaction.undoRepost", current_test_id="unretweet", desired_test_id="retweet", click_test_ids=("unretweet",), menu_confirm=confirm)

    async def interaction_send_via_chat(self, page: Page, payload: dict[str, Any], options: ExecutionOptions) -> dict[str, Any]:
        article = await self._require_tweet(page, payload.get("tweetId"))
        await self._click(await self._owned_control(article, ("share",)), "post share button", options)
        item = page.get_by_role("menuitem", name=re.compile(r"send via (direct message|chat)|通过.*(私信|聊天).*发送|发送.*私信", re.I))
        await self._click(item, "Send via chat", options)
        dialog = page.get_by_role("dialog")
        try:
            await dialog.wait_for(state="visible", timeout=options.timeout_ms)
        except Exception as error:
            raise ActionError("TIMEOUT", "Send via chat did not open a recipient dialog.", retryable=True) from error
        return {"status": "success", "dialogOpened": True, "finalSendPerformed": False, "evidence": ["recipient-dialog-visible"]}

    async def _scroll(self, page: Page, payload: dict[str, Any], options: ExecutionOptions, expected_page: str) -> dict[str, Any]:
        if classify_page(page.url) != expected_page:
            raise ActionError("PAGE_UNSUPPORTED", f"Scrolling requires page type {expected_page}.")
        distance = int(clamp_number(payload.get("distance"), 600, -5000, 5000))
        before = int(await page.evaluate("window.scrollY"))
        maximum = int(await page.evaluate("Math.max(0, document.documentElement.scrollHeight-innerHeight)"))
        await page.mouse.wheel(0, distance)
        await cancellable_sleep(450 if payload.get("behavior") == "smooth" else 100, options.cancellation)
        after = int(await page.evaluate("window.scrollY"))
        at_boundary = after >= maximum if distance >= 0 else after <= 0
        if after == before and at_boundary:
            return {"status": "skipped", "reason": "Already at the scroll boundary.", "beforeY": before, "afterY": after}
        if after == before:
            raise ActionError("STATE_UNKNOWN", "The page did not scroll as requested.", {"distance": distance, "beforeY": before}, retryable=True)
        return {"status": "success", "distance": distance, "beforeY": before, "afterY": after, "evidence": [f"scrollY:{before}->{after}"]}

    async def browse_scroll_timeline(self, page: Page, payload: dict[str, Any], options: ExecutionOptions) -> dict[str, Any]:
        return await self._scroll(page, payload, options, "home")

    async def browse_scroll_comments(self, page: Page, payload: dict[str, Any], options: ExecutionOptions) -> dict[str, Any]:
        return await self._scroll(page, payload, options, "tweet-detail")

    async def browse_wait(self, page: Page, payload: dict[str, Any], options: ExecutionOptions) -> dict[str, Any]:
        duration = int(clamp_number(payload.get("durationMs"), 1000, 0, 60000))
        if duration > options.timeout_ms:
            await cancellable_sleep(options.timeout_ms, options.cancellation)
            raise ActionError("TIMEOUT", "Browsing wait exceeded the action timeout.", {"timeoutMs": options.timeout_ms}, retryable=True)
        await cancellable_sleep(duration, options.cancellation)
        return {"status": "success", "durationMs": duration, "evidence": [f"waited:{duration}ms"]}

    async def browse_open_for_you(self, page: Page, payload: dict[str, Any], options: ExecutionOptions) -> dict[str, Any]:
        return await self.timeline_open(page, {**payload, "feed": "for-you"}, options)

    async def browse_open_following(self, page: Page, payload: dict[str, Any], options: ExecutionOptions) -> dict[str, Any]:
        return await self.timeline_open(page, {**payload, "feed": "following"}, options)

    async def browse_browse_for_you(self, page: Page, payload: dict[str, Any], options: ExecutionOptions) -> dict[str, Any]:
        return await self.timeline_browse(page, {**payload, "feed": "for-you"}, options)

    async def browse_browse_following(self, page: Page, payload: dict[str, Any], options: ExecutionOptions) -> dict[str, Any]:
        return await self.timeline_browse(page, {**payload, "feed": "following"}, options)

    async def browse_collect_for_you(self, page: Page, payload: dict[str, Any], options: ExecutionOptions) -> dict[str, Any]:
        return await self.timeline_collect(page, {**payload, "feed": "for-you"}, options)

    async def browse_collect_following(self, page: Page, payload: dict[str, Any], options: ExecutionOptions) -> dict[str, Any]:
        return await self.timeline_collect(page, {**payload, "feed": "following"}, options)

    async def account_search(self, page: Page, payload: dict[str, Any], options: ExecutionOptions) -> dict[str, Any]:
        query = str(payload.get("query") or "").strip()
        if not query:
            raise ActionError("CONTENT_MISMATCH", "Enter an account name or username.")
        username = query.removeprefix("@")
        if is_safe_username(username) and " " not in query:
            url = f"https://x.com/{username}"
            mode = "exact"
        else:
            url = f"https://x.com/search?q={quote(query)}&src=typed_query&f=user"
            mode = "search"
        await page.goto(url, wait_until="domcontentloaded", timeout=options.timeout_ms)
        return {"status": "navigating", "requiresRetry": True, "mode": mode, "url": url}

    async def _visible_locator(self, locators: list[Locator]) -> Locator | None:
        for locator in locators:
            if await locator.count() and await locator.first.is_visible():
                return locator.first
        return None

    async def _wait_for_visible_locator(
        self,
        locators: list[Locator],
        options: ExecutionOptions,
        *,
        timeout_ms: int = 12_000,
    ) -> Locator | None:
        deadline = asyncio.get_running_loop().time() + min(timeout_ms, options.timeout_ms) / 1000
        while asyncio.get_running_loop().time() < deadline:
            try:
                locator = await self._visible_locator(locators)
            except Exception:  # X may replace the login dialog during navigation.
                locator = None
            if locator is not None:
                return locator
            await cancellable_sleep(250, options.cancellation)
        return None

    async def _type_like_user(
        self,
        locator: Locator,
        value: str,
        options: ExecutionOptions,
        *,
        delay_ms: int,
    ) -> None:
        await locator.scroll_into_view_if_needed(timeout=options.timeout_ms)
        await locator.click(timeout=options.timeout_ms)
        modifier = "Meta" if sys.platform == "darwin" else "Control"
        await locator.press(f"{modifier}+A", timeout=options.timeout_ms)
        await locator.type(value, delay=max(20, min(delay_ms, 500)), timeout=options.timeout_ms)

    async def _click_button_by_name(self, page: Page, names: list[str], options: ExecutionOptions) -> bool:
        for name in names:
            locator = page.get_by_role("button", name=re.compile(f"^{re.escape(name)}$", re.I)).first
            if await locator.count() and await locator.is_visible() and await locator.is_enabled():
                await self._click(locator, name, options)
                return True
        return False

    async def _fill_two_factor_code(
        self,
        page: Page,
        input_locator: Locator,
        code: str,
        options: ExecutionOptions,
        *,
        typing_delay_ms: int,
    ) -> None:
        numeric_inputs = page.locator('input[inputmode="numeric"]')
        visible_inputs = [
            numeric_inputs.nth(index)
            for index in range(await numeric_inputs.count())
            if await numeric_inputs.nth(index).is_visible()
        ]
        if len(visible_inputs) >= len(code) and len(code) > 1:
            for locator, digit in zip(visible_inputs, code, strict=False):
                await self._type_like_user(
                    locator,
                    digit,
                    options,
                    delay_ms=typing_delay_ms,
                )
            return
        await self._type_like_user(
            input_locator,
            code,
            options,
            delay_ms=typing_delay_ms,
        )

    async def _fresh_totp_code(
        self,
        secret: str,
        options: ExecutionOptions,
        *,
        previous_code: str | None = None,
    ) -> str:
        deadline = asyncio.get_running_loop().time() + min(options.timeout_ms / 1000, 35)
        while asyncio.get_running_loop().time() < deadline:
            code = totp_now(secret)
            seconds_remaining = 30 - int(time.time()) % 30
            if code != previous_code and seconds_remaining >= 8:
                return code
            await cancellable_sleep(500, options.cancellation)
        raise ActionError(
            "TOTP_CODE_UNAVAILABLE",
            "A fresh two-factor code could not be generated before the timeout.",
            retryable=True,
        )

    async def _login_challenge_reason(self, page: Page) -> str | None:
        text = await page.locator("body").inner_text(timeout=3_000)
        on_two_factor_page = "two_factor" in page.url.casefold()
        if on_two_factor_page and re.search(
            r"incorrect(?:\.|,)?\s*(?:please\s+try\s+again)?|"
            r"(?:verification\s+)?code.*(?:incorrect|invalid)|"
            r"(?:验证码|验证代码|动态码|代码).*(?:错误|不正确|无效)|"
            r"(?:错误|不正确|无效).*(?:验证码|验证代码|动态码|代码)|"
            r"(?:認証コード|確認コード|コード).*(?:正しくありません|間違|無効)",
            text,
            re.I,
        ):
            return "two_factor_code_rejected"
        if re.search(
            r"wrong password|密码.*(?:错误|不正确)|"
            r"アカウント.*見つかりません|ユーザー名.*見つかりません|"
            r"有効なアカウント.*見つかりません|パスワード.*正しくありません",
            text,
            re.I,
        ):
            return "credentials_rejected"
        if not on_two_factor_page and re.search(r"\bincorrect\b", text, re.I):
            return "credentials_rejected"
        if re.search(
            r"captcha|arkose|verify you are human|证明你是真人|人机验证|安全检查|"
            r"人間であることを確認|ロボットではない|セキュリティチェック",
            text,
            re.I,
        ):
            return "captcha_or_human_verification"
        if re.search(
            r"email|phone|verify your identity|确认.*身份|邮箱|手机|手机号|"
            r"本人確認|メールアドレス|電話番号",
            text,
            re.I,
        ):
            return "extra_identity_verification"
        return None

    async def _wait_after_login_attempt(
        self,
        page: Page,
        options: ExecutionOptions,
        *,
        expected_username: str | None,
    ) -> dict[str, Any]:
        deadline = asyncio.get_running_loop().time() + min(options.timeout_ms / 1000, 60)
        while asyncio.get_running_loop().time() < deadline:
            session = await self._account_session_data(page)
            username = normalize_username(session.get("username"))
            if session.get("loggedIn") is True:
                if expected_username and username and username != normalize_username(expected_username):
                    raise ActionError(
                        "ACCOUNT_MISMATCH",
                        "The browser logged in to a different X account.",
                        {"expectedUsername": normalize_username(expected_username), "actualUsername": username},
                    )
                return {
                    "status": "success",
                    "session": session,
                    "url": page.url,
                    "evidence": ["account-session-authenticated"],
                }
            reason = await self._login_challenge_reason(page)
            if reason:
                return {
                    "status": "failed" if reason == "credentials_rejected" else "uncertain",
                    "reason": reason,
                    "url": page.url,
                    "evidence": [reason],
                }
            await cancellable_sleep(1_000, options.cancellation)
        return {
            "status": "uncertain",
            "reason": "login_result_not_confirmed",
            "url": page.url,
            "session": await self._account_session_data(page),
        }

    async def account_login(self, page: Page, payload: dict[str, Any], options: ExecutionOptions) -> dict[str, Any]:
        username = str(payload.get("username") or "").strip()
        password = str(payload.get("password") or "")
        expected_username = str(payload.get("expectedUsername") or username).strip() or None
        if not username or not password:
            raise ActionError("CONTENT_MISMATCH", "username and password are required.")

        step_delay_ms = int(clamp_number(payload.get("stepDelayMs"), 1800, 500, 30000))
        typing_delay_ms = int(clamp_number(payload.get("typingDelayMs"), 85, 20, 500))
        jitter = random.randint(0, max(250, step_delay_ms // 3))

        session = await self._account_session_data(page)
        current_username = normalize_username(session.get("username"))
        if session.get("loggedIn") is True:
            if expected_username and current_username and current_username != normalize_username(expected_username):
                raise ActionError(
                    "ACCOUNT_MISMATCH",
                    "The browser is already logged in to a different X account.",
                    {"expectedUsername": normalize_username(expected_username), "actualUsername": current_username},
                )
            return {"status": "skipped", "reason": "already_authenticated", "session": session}

        await page.goto("https://x.com/i/flow/login", wait_until="domcontentloaded", timeout=options.timeout_ms)
        await cancellable_sleep(step_delay_ms + jitter, options.cancellation)

        username_input = await self._wait_for_visible_locator([
            page.locator('input[autocomplete="username"]'),
            page.locator('input[name="text"]'),
            page.get_by_label(
                re.compile(
                    "phone|email|username|手机|邮箱|用户名|電話番号|メールアドレス|ユーザー名",
                    re.I,
                )
            ),
            page.locator('input[type="text"]').first,
        ], options)
        if username_input is None:
            raise ActionError("TARGET_NOT_FOUND", "Could not find the X username field.")
        await self._type_like_user(username_input, username, options, delay_ms=typing_delay_ms)
        await cancellable_sleep(step_delay_ms + random.randint(0, 600), options.cancellation)
        if not await self._click_button_by_name(page, ["Next", "下一步", "次へ", "続ける"], options):
            await username_input.press("Enter", timeout=options.timeout_ms)

        await cancellable_sleep(step_delay_ms + random.randint(0, 800), options.cancellation)
        challenge = await self._login_challenge_reason(page)
        if challenge == "credentials_rejected":
            return {
                "status": "failed",
                "reason": challenge,
                "url": page.url,
                "evidence": [challenge],
            }
        password_input = await self._wait_for_visible_locator([
            page.locator('input[name="password"]'),
            page.locator('input[type="password"]'),
            page.get_by_label(re.compile("password|密码|パスワード", re.I)),
        ], options, timeout_ms=8_000)
        if password_input is None:
            return {
                "status": "uncertain",
                "reason": challenge or "password_field_not_available",
                "url": page.url,
                "evidence": [challenge or "password-field-missing"],
            }
        await self._type_like_user(password_input, password, options, delay_ms=typing_delay_ms)
        await cancellable_sleep(step_delay_ms + random.randint(0, 600), options.cancellation)
        options.trace.mark_mutation_triggered()
        if not await self._click_button_by_name(page, ["Log in", "登录", "ログイン", "続ける"], options):
            await password_input.press("Enter", timeout=options.timeout_ms)

        await cancellable_sleep(step_delay_ms + random.randint(0, 800), options.cancellation)
        two_factor_input = await self._wait_for_visible_locator([
            page.locator('input[inputmode="numeric"]'),
            page.locator('input[name="text"]'),
            page.get_by_label(re.compile("code|验证码|verification|認証コード|確認コード", re.I)),
        ], options, timeout_ms=8_000)
        result: dict[str, Any] | None = None
        if two_factor_input is not None:
            fixed_code = str(payload.get("twoFactorCode") or "").strip()
            secret = str(payload.get("totpSecret") or "").strip()
            if not fixed_code and not secret:
                return {
                    "status": "uncertain",
                    "reason": "two_factor_code_required",
                    "url": page.url,
                    "evidence": ["2fa-input-visible"],
                }
            previous_code: str | None = None
            max_attempts = 2 if secret and not fixed_code else 1
            for attempt in range(max_attempts):
                code = fixed_code or await self._fresh_totp_code(
                    secret,
                    options,
                    previous_code=previous_code,
                )
                await self._fill_two_factor_code(
                    page,
                    two_factor_input,
                    code,
                    options,
                    typing_delay_ms=typing_delay_ms,
                )
                await cancellable_sleep(step_delay_ms + random.randint(0, 600), options.cancellation)
                if not await self._click_button_by_name(
                    page,
                    ["Next", "Verify", "Done", "Continue", "下一步", "验证", "完成", "继续", "次へ", "確認", "完了", "続ける"],
                    options,
                ):
                    await two_factor_input.press("Enter", timeout=options.timeout_ms)
                result = await self._wait_after_login_attempt(
                    page, options, expected_username=expected_username
                )
                if result.get("reason") != "two_factor_code_rejected":
                    break
                previous_code = code
                if attempt + 1 >= max_attempts:
                    raise ActionError(
                        "TOTP_CODE_REJECTED",
                        "X rejected the two-factor authentication code.",
                        {"attempts": max_attempts, "url": page.url},
                    )
                two_factor_input = await self._wait_for_visible_locator([
                    page.locator('input[inputmode="numeric"]'),
                    page.locator('input[name="text"]'),
                    page.get_by_label(re.compile("code|验证码|verification|認証コード|確認コード", re.I)),
                ], options, timeout_ms=5_000)
                if two_factor_input is None:
                    raise ActionError(
                        "TOTP_CODE_REJECTED",
                        "X rejected the two-factor code and the input was no longer available for retry.",
                        {"attempts": attempt + 1, "url": page.url},
                    )

        if result is None:
            result = await self._wait_after_login_attempt(
                page, options, expected_username=expected_username
            )
        if result.get("status") == "failed" and result.get("reason") == "credentials_rejected":
            raise ActionError(
                "LOGIN_CREDENTIALS_REJECTED",
                "X rejected the supplied username, account identifier, or password.",
                {"url": result.get("url")},
            )
        if result["status"] == "uncertain" and result.get("reason") in {
            "captcha_or_human_verification",
            "extra_identity_verification",
        }:
            raise ActionError(
                "LOGIN_CHALLENGE_REQUIRED",
                "X requested an additional verification step.",
                {"reason": result["reason"], "url": result.get("url")},
                uncertain=True,
            )
        return result

    async def _account_session_data(self, page: Page) -> dict[str, Any]:
        switcher = page.locator('[data-testid="SideNav_AccountSwitcher_Button"]')
        text = await switcher.inner_text() if await switcher.count() else ""
        profile_link = page.locator('[data-testid="AppTabBar_Profile_Link"]')
        href = await profile_link.get_attribute("href") if await profile_link.count() else None
        username = (re.search(r"@([A-Za-z0-9_]{1,15})", text) or re.match(r"^/([A-Za-z0-9_]{1,15})/?$", href or ""))
        resolved = username.group(1) if username else None
        login = page.locator('a[href="/login"], a[href^="/i/flow/login"], [data-testid="loginButton"]')
        logged_in = True if resolved else False if await login.count() else None
        return {"username": resolved, "handle": f"@{resolved}" if resolved else None, "displayName": next((line.strip() for line in text.splitlines() if line.strip() and "@" not in line), None), "profileUrl": f"https://x.com/{resolved}" if resolved else None, "loggedIn": logged_in, "state": "authenticated" if logged_in is True else "unauthenticated" if logged_in is False else "unknown", "confidence": "high" if resolved and await switcher.count() else "medium" if resolved else "high" if await login.count() else "low"}

    async def account_get_session(self, page: Page, payload: dict[str, Any], options: ExecutionOptions) -> dict[str, Any]:
        return {"status": "success", "session": await self._account_session_data(page), "checkedAt": datetime.now(UTC).isoformat(), "url": page.url}

    async def _profile_state(self, page: Page) -> tuple[str, str]:
        primary = page.locator('[data-testid="primaryColumn"]')
        text = await primary.inner_text() if await primary.count() else await page.locator("body").inner_text()
        if re.search(r"This account doesn.?t exist|账号不存在|找不到此账号", text, re.I):
            return "not-found", text
        if re.search(r"Account suspended|账号.*(封禁|冻结|暂停)", text, re.I):
            return "suspended", text
        if re.search(r"temporarily restricted|临时受限|暂时受限", text, re.I):
            return "restricted", text
        if re.search(r"Something went wrong|出错了|出现错误|请重试", text, re.I):
            return "load-failed", text
        handle = urlparse(page.url).path.strip("/")
        if handle and await page.locator(f'a[href="/{handle}"]').count():
            return "private" if re.search(r"posts are protected|帖子受到保护|私密账号", text, re.I) else "active", text
        if await page.locator('[role="progressbar"], [data-testid*="skeleton" i]').count():
            return "loading", text
        return "unknown", text

    async def account_get_details(self, page: Page, payload: dict[str, Any], options: ExecutionOptions) -> dict[str, Any]:
        requested = str(payload.get("handle") or "").strip().removeprefix("@")
        if requested and not is_safe_username(requested):
            raise ActionError("CONTENT_MISMATCH", "handle must be a valid X username.")
        current = urlparse(page.url).path.strip("/")
        if requested and normalize_username(requested) != normalize_username(current):
            url = f"https://x.com/{requested}"
            await page.goto(url, wait_until="domcontentloaded", timeout=options.timeout_ms)
            return {"status": "navigating", "url": url, "requiresRetry": True, "requestedHandle": f"@{requested}"}
        if classify_page(page.url) != "profile":
            raise ActionError("PAGE_UNSUPPORTED", "Open a user profile or provide handle first.")
        state, text = await self._profile_state(page)
        if state == "loading":
            try:
                await page.wait_for_function("() => !document.querySelector('[role=progressbar], [data-testid*=skeleton i]')", timeout=options.timeout_ms)
                state, text = await self._profile_state(page)
            except Exception as error:
                raise ActionError("PROFILE_LOADING_TIMEOUT", "The profile remained in a loading state.", retryable=True) from error
        errors = {"not-found": ("ACCOUNT_NOT_FOUND", "The requested account does not exist."), "suspended": ("ACCOUNT_SUSPENDED", "The requested account is suspended."), "restricted": ("ACCOUNT_TEMPORARILY_RESTRICTED", "The account is temporarily restricted."), "load-failed": ("PROFILE_LOAD_FAILED", "X reported that the profile failed to load."), "unknown": ("PROFILE_STATE_UNKNOWN", "The profile state is not recognizable.")}
        if state in errors:
            code, message = errors[state]
            raise ActionError(code, message, {"text": text[:240]}, retryable=code == "PROFILE_LOAD_FAILED")
        current = urlparse(page.url).path.strip("/")
        user_name = page.locator('[data-testid="UserName"]')
        header_text = await user_name.inner_text() if await user_name.count() else ""
        visible_handle = re.search(r"@([A-Za-z0-9_]{1,15})", header_text)
        resolved = visible_handle.group(1) if visible_handle else current
        if requested and normalize_username(requested) != normalize_username(resolved):
            raise ActionError("ACCOUNT_MISMATCH", "The loaded profile does not match the requested account.", {"requested": requested, "resolved": resolved})
        bio = page.locator('[data-testid="UserDescription"]')
        relationship = self._profile_relationship_locator(page, resolved)
        test_id = await relationship.get_attribute("data-testid") if await relationship.count() else ""
        aria = await relationship.get_attribute("aria-label") if await relationship.count() else ""
        return {"status": "success", "account": {"username": resolved, "handle": f"@{resolved}", "displayName": next((line for line in header_text.splitlines() if line and not line.startswith("@")), None), "bio": (await bio.inner_text()).strip() if await bio.count() else None, "profileUrl": f"https://x.com/{resolved}", "isPrivate": state == "private", "relationship": relationship_state(test_id or "", aria or ""), "requestedHandle": f"@{requested}" if requested else None, "resolvedHandle": f"@{resolved}", "redirected": bool(requested and normalize_username(requested) != normalize_username(current))}}

    async def account_list_candidates(self, page: Page, payload: dict[str, Any], options: ExecutionOptions) -> dict[str, Any]:
        if classify_page(page.url) != "search-people":
            raise ActionError("PAGE_UNSUPPORTED", "Account candidates require People search results.")
        cells = page.locator('[data-testid="primaryColumn"] [data-testid="UserCell"]')
        results: dict[str, dict[str, Any]] = {}
        for index in range(min(await cells.count(), 50)):
            cell = cells.nth(index)
            text = await cell.inner_text()
            match = re.search(r"@([A-Za-z0-9_]{1,15})", text)
            if not match:
                continue
            username = match.group(1)
            button = cell.locator('button[data-testid$="-follow"], button[data-testid$="-unfollow"], button[aria-label^="Follow @"], button[aria-label^="Following @"], button[aria-label^="Requested @"]')
            results.setdefault(normalize_username(username), {"username": username, "handle": f"@{username}", "displayName": next((line for line in text.splitlines() if line and not line.startswith("@")), None), "profileUrl": f"https://x.com/{username}", "relationship": relationship_state((await button.get_attribute("data-testid") or "") if await button.count() else "", (await button.get_attribute("aria-label") or "") if await button.count() else "")})
        return {"status": "success", "accounts": list(results.values())[:20]}

    async def account_list_posts(self, page: Page, payload: dict[str, Any], options: ExecutionOptions) -> dict[str, Any]:
        if classify_page(page.url) != "profile":
            raise ActionError("PAGE_UNSUPPORTED", "Account posts require a profile page.")
        username = urlparse(page.url).path.strip("/")
        if not is_safe_username(username):
            raise ActionError("PROFILE_STATE_UNKNOWN", "Could not identify the current profile username.")
        max_posts = int(clamp_number(payload.get("maxPosts"), 10, 1, 50))
        include_replies = payload.get("includeReplies") is True
        include_pinned = payload.get("includePinned", True) is not False
        articles = page.locator('[data-testid="primaryColumn"] article[data-testid="tweet"]')
        posts: list[dict[str, Any]] = []
        seen: set[str] = set()
        filtered_replies = filtered_pinned = filtered_other_authors = 0
        for index in range(await articles.count()):
            if len(posts) >= max_posts:
                break
            article = articles.nth(index)
            try:
                post = await self._post(article, include_ads=False)
            except ActionError:
                continue
            post_id = str(post.get("postId") or "")
            author = normalize_username((post.get("author") or {}).get("username"))
            if not post_id or post_id in seen:
                continue
            seen.add(post_id)
            if author != normalize_username(username):
                filtered_other_authors += 1
                continue
            text = await article.inner_text()
            if not include_replies and re.search(r"Replying to|\u6b63\u5728\u56de\u590d|\u56de\u590d @", text, re.I):
                filtered_replies += 1
                continue
            if not include_pinned and re.search(r"(^|\n)Pinned(\n|$)|(^|\n)\u5df2\u7f6e\u9876(\n|$)|(^|\n)\u7f6e\u9876(\n|$)", text, re.I):
                filtered_pinned += 1
                continue
            posts.append(post)
        return {
            "status": "success",
            "username": username,
            "posts": posts,
            "visibleCount": await articles.count(),
            "filteredReplyCount": filtered_replies,
            "filteredPinnedCount": filtered_pinned,
            "filteredOtherAuthorCount": filtered_other_authors,
        }

    async def account_scroll_posts(self, page: Page, payload: dict[str, Any], options: ExecutionOptions) -> dict[str, Any]:
        if classify_page(page.url) != "profile":
            raise ActionError("PAGE_UNSUPPORTED", "Account post scrolling requires a profile page.")
        distance = int(clamp_number(payload.get("distance"), 650, 200, 3000))
        before = int(await page.evaluate("window.scrollY"))
        await page.mouse.wheel(0, distance)
        await cancellable_sleep(250, options.cancellation)
        after = int(await page.evaluate("window.scrollY"))
        maximum = int(await page.evaluate("Math.max(0, document.documentElement.scrollHeight-innerHeight)"))
        at_boundary = after >= maximum
        return {
            "status": "skipped" if after == before else "success",
            "reason": "profile-boundary" if after == before else None,
            "startY": before,
            "endY": after,
            "scrolls": 0 if after == before else 1,
            "atBoundary": at_boundary,
        }

    def _profile_relationship_locator(self, page: Page, username: str) -> Locator:
        primary = page.locator('[data-testid="primaryColumn"]')
        return primary.locator(f'button[data-testid="{username}-follow"], button[data-testid="{username}-unfollow"], button[aria-label="Follow @{username}"], button[aria-label="Following @{username}"], button[aria-label="Requested @{username}"]').first

    async def account_follow(self, page: Page, payload: dict[str, Any], options: ExecutionOptions) -> dict[str, Any]:
        return await self._relationship_action(page, options, follow=True)

    async def account_follow_handle(self, page: Page, payload: dict[str, Any], options: ExecutionOptions) -> dict[str, Any]:
        username = str(payload.get("handle") or "").strip().removeprefix("@")
        if not is_safe_username(username):
            raise ActionError("CONTENT_MISMATCH", "handle must be a valid X username.")
        profile_page = await page.context.new_page()
        try:
            await profile_page.goto(
                f"https://x.com/{username}",
                wait_until="domcontentloaded",
                timeout=options.timeout_ms,
            )
            details = await self.account_get_details(
                profile_page,
                {"handle": username},
                options,
            )
            if details.get("status") == "navigating":
                await cancellable_sleep(500, options.cancellation)
                details = await self.account_get_details(
                    profile_page,
                    {"handle": username},
                    options,
                )
            result = await self._relationship_action(profile_page, options, follow=True)
            return {
                **result,
                "requestedHandle": f"@{username}",
                "account": details.get("account"),
            }
        finally:
            try:
                await profile_page.close()
            except Exception:
                pass

    async def account_unfollow(self, page: Page, payload: dict[str, Any], options: ExecutionOptions) -> dict[str, Any]:
        return await self._relationship_action(page, options, follow=False)

    async def _relationship_action(self, page: Page, options: ExecutionOptions, *, follow: bool) -> dict[str, Any]:
        if classify_page(page.url) != "profile":
            raise ActionError("PAGE_UNSUPPORTED", "Follow actions require a profile page.")
        username = urlparse(page.url).path.strip("/")
        session = await self._account_session_data(page)
        if normalize_username(session.get("username")) == normalize_username(username):
            raise ActionError("TARGET_UNSAFE", "Cannot follow or unfollow the signed-in account itself.")
        control = self._profile_relationship_locator(page, username)
        if not await control.count():
            raise ActionError("TARGET_NOT_FOUND", "Profile relationship control was not found.")
        state = relationship_state(await control.get_attribute("data-testid") or "", await control.get_attribute("aria-label") or "")
        desired = "following" if follow else "not-following"
        if state == desired or (follow and state == "requested"):
            return {"status": "skipped", "reason": f"Profile relationship is already {state}.", "username": username}
        if options.dry_run:
            return {"status": "success", "dryRun": True, "wouldExecute": "follow" if follow else "unfollow", "username": username}
        await self._click(
            control,
            "Follow" if follow else "Unfollow",
            options,
            mutation=follow,
        )
        if not follow:
            confirm = page.locator('[data-testid="confirmationSheetConfirm"]')
            if not await confirm.count():
                confirm = page.get_by_role("dialog").get_by_role("button", name=re.compile(r"^(Unfollow|取消关注)$", re.I))
            await self._click(confirm, "Unfollow confirmation", options, mutation=True)
        try:
            deadline = asyncio.get_running_loop().time() + min(options.timeout_ms, 7000) / 1000
            while asyncio.get_running_loop().time() < deadline:
                fresh = self._profile_relationship_locator(page, username)
                if await fresh.count():
                    observed = relationship_state(await fresh.get_attribute("data-testid") or "", await fresh.get_attribute("aria-label") or "")
                    if observed == desired or (follow and observed == "requested"):
                        return {"status": "success", "username": username, "relationship": observed, "evidence": [f"relationship:{state}->{observed}"]}
                await asyncio.sleep(0.12)
        except Exception:
            pass
        return {"status": "uncertain", "username": username, "reason": "Relationship control was clicked but the final state was not proven. Do not retry automatically."}

    async def _open_post_composer(self, page: Page, options: ExecutionOptions) -> Locator:
        existing = page.locator('[role="dialog"] [data-testid="tweetTextarea_0"][contenteditable="true"]')
        for index in range(await existing.count()):
            if await existing.nth(index).is_visible():
                return existing.nth(index)
        trigger = page.locator('[data-testid="SideNav_NewTweet_Button"]')
        if await trigger.count():
            await self._click(trigger, "post composer trigger", options)
            try:
                await existing.first.wait_for(state="visible", timeout=options.timeout_ms)
                return existing.first
            except Exception as error:
                raise ActionError("ACTION_UNSUPPORTED", "X did not expose a visible dedicated post composer.") from error
        await page.goto("https://x.com/compose/post", wait_until="domcontentloaded", timeout=options.timeout_ms)
        editor = page.locator('[data-testid="tweetTextarea_0"][contenteditable="true"]')
        try:
            await editor.first.wait_for(state="visible", timeout=options.timeout_ms)
        except Exception as error:
            raise ActionError("ACTION_UNSUPPORTED", "X did not expose a supported post composer layout.") from error
        return editor.first

    async def _attach_media(self, editor: Locator, payload: dict[str, Any], options: ExecutionOptions) -> None:
        media = payload.get("media") or []
        if not media:
            return
        paths: list[str] = []
        total = 0
        for item in media:
            raw_path = item.get("path") if isinstance(item, dict) else item
            path = await asyncio.to_thread(_normalized_media_path, raw_path)
            if not await asyncio.to_thread(os.path.isfile, path):
                raise ActionError("CONTENT_MISMATCH", f"Media file does not exist: {path}")
            total += await asyncio.to_thread(os.path.getsize, path)
            paths.append(path)
        if total > 40 * 1024 * 1024:
            raise ActionError("MEDIA_TOO_LARGE", "Media exceeds the 40 MB upload safety limit.", {"totalBytes": total})
        surface = editor.locator("xpath=ancestor::*[.//input[@type='file' and @data-testid='fileInput']][1]")
        file_input = surface.locator('input[type="file"][data-testid="fileInput"]')
        if not await file_input.count():
            raise ActionError("ACTION_UNSUPPORTED", "The selected composer has no supported media input.")
        await file_input.set_input_files(paths, timeout=options.timeout_ms)
        progress = surface.locator('[role="progressbar"], [data-testid="progressBar-bar"]')
        if await progress.count():
            try:
                await progress.first.wait_for(state="hidden", timeout=min(options.timeout_ms, 60_000))
            except Exception as error:
                raise ActionError("TIMEOUT", "Media upload did not finish.", retryable=True) from error

    async def publish_post(self, page: Page, payload: dict[str, Any], options: ExecutionOptions) -> dict[str, Any]:
        text = str(payload.get("text") or "")
        if not text.strip() and not payload.get("media"):
            raise ActionError("CONTENT_MISMATCH", "Post content is empty.")
        if options.dry_run:
            return {"status": "success", "dryRun": True, "wouldExecute": "publish.post", "contentHash": hash_text(text), "mediaCount": len(payload.get("media") or [])}
        editor = await self._open_post_composer(page, options)
        surface, button = await self._prepare_composer(page, editor, text, options) if text else (editor.locator("xpath=ancestor::*[@role='dialog'][1]"), editor.locator("xpath=ancestor::*[@role='dialog'][1]").locator('[data-testid="tweetButton"], [data-testid="tweetButtonInline"]').first)
        await self._attach_media(editor, payload, options)
        if not await button.is_enabled():
            raise ActionError("CONTENT_MISMATCH", "X did not enable the Post button after native Playwright input.")
        return await self._submit_composer(page, editor, button, text, options, "publish.post")

    async def _schedule_wall_time(self, page: Page, scheduled: datetime) -> tuple[datetime, str]:
        timezone_name = str(
            await page.evaluate("Intl.DateTimeFormat().resolvedOptions().timeZone")
            or ""
        )
        try:
            timezone = ZoneInfo(timezone_name)
        except ZoneInfoNotFoundError as error:
            raise ActionError(
                "STATE_UNKNOWN",
                "The browser Profile timezone could not be resolved.",
                {"timezone": timezone_name},
            ) from error

        if scheduled.tzinfo is not None:
            return scheduled.astimezone(timezone), timezone_name

        # A timestamp without an offset is explicitly interpreted as Profile
        # wall time. Reject DST gaps and folds rather than scheduling at a
        # silently shifted or ambiguous instant.
        candidates = (
            scheduled.replace(tzinfo=timezone, fold=0),
            scheduled.replace(tzinfo=timezone, fold=1),
        )
        valid = [
            candidate
            for candidate in candidates
            if candidate.astimezone(UTC).astimezone(timezone).replace(tzinfo=None)
            == scheduled
        ]
        if not valid:
            raise ActionError(
                "INVALID_SCHEDULE_TIME",
                "The requested Profile-local time does not exist because of daylight saving time.",
                {"timezone": timezone_name, "scheduleAt": scheduled.isoformat()},
            )
        if len({candidate.utcoffset() for candidate in valid}) > 1:
            raise ActionError(
                "INVALID_SCHEDULE_TIME",
                "The requested Profile-local time is ambiguous because of daylight saving time.",
                {"timezone": timezone_name, "scheduleAt": scheduled.isoformat()},
            )
        return valid[0], timezone_name

    async def _schedule_control_kind(self, control: Locator) -> str | None:
        metadata = cast(
            dict[str, str],
            await control.evaluate(
                r"""el => {
                  const labelled = (el.getAttribute('aria-labelledby') || '')
                    .split(/\s+/).filter(Boolean)
                    .map(id => document.getElementById(id)?.textContent || '').join(' ');
                  return {
                    text: [el.getAttribute('aria-label') || '', el.name || '', el.id || '',
                      [...(el.labels || [])].map(label => label.textContent || '').join(' '), labelled]
                      .join(' ').toLowerCase()
                  };
                }"""
            ),
        )
        text = metadata.get("text", "")
        patterns = (
            ("meridiem", r"\b(am|pm|meridiem)\b|上午|下午"),
            ("minute", r"\bminute(s)?\b|分钟|分鐘|分$"),
            ("hour", r"\bhour(s)?\b|小时|小時|时$|時$"),
            ("year", r"\byear\b|年份|年$"),
            ("month", r"\bmonth\b|月份|月$"),
            ("day", r"\bday\b|\bdate\b|日期|日$"),
        )
        return next((kind for kind, pattern in patterns if re.search(pattern, text, re.I)), None)

    async def _select_schedule_number(
        self, control: Locator, value: int, field: str
    ) -> None:
        options = cast(
            list[dict[str, str]],
            await control.locator("option").evaluate_all(
                "els => els.map(el => ({value: el.value, text: (el.textContent || '').trim()}))"
            ),
        )
        selected: str | None = None
        for option in options:
            for candidate in (option["value"], option["text"]):
                match = re.fullmatch(r"\s*0*(\d+)\s*", candidate)
                if match and int(match.group(1)) == value:
                    selected = option["value"]
                    break
            if selected is not None:
                break
        if selected is None:
            raise ActionError(
                "ACTION_UNSUPPORTED",
                f"The X schedule {field} control does not contain the requested value.",
                {"field": field, "value": value},
            )
        await control.select_option(value=selected)
        if await control.input_value() != selected:
            raise ActionError("STATE_UNKNOWN", f"X did not retain the selected schedule {field}.")

    async def _select_schedule_meridiem(self, control: Locator, value: str) -> None:
        wanted = "am" if value == "AM" else "pm"
        pattern = r"\bam\b|上午" if wanted == "am" else r"\bpm\b|下午"
        options = cast(
            list[dict[str, str]],
            await control.locator("option").evaluate_all(
                "els => els.map(el => ({value: el.value, text: (el.textContent || '').trim()}))"
            ),
        )
        selected = next(
            (
                option["value"]
                for option in options
                if re.search(pattern, f"{option['value']} {option['text']}", re.I)
            ),
            None,
        )
        if selected is None:
            raise ActionError(
                "ACTION_UNSUPPORTED",
                "The X schedule dialog exposes a 12-hour clock without a recognizable AM/PM option.",
            )
        await control.select_option(value=selected)
        if await control.input_value() != selected:
            raise ActionError("STATE_UNKNOWN", "X did not retain the selected schedule AM/PM value.")

    async def _fill_schedule_controls(self, dialog: Locator, scheduled: datetime) -> None:
        selects = dialog.locator("select")
        count = await selects.count()
        if count not in {5, 6}:
            raise ActionError("ACTION_UNSUPPORTED", "X schedule controls have an unsupported structure.")

        controls: dict[str, Locator] = {}
        for index in range(count):
            control = selects.nth(index)
            kind = await self._schedule_control_kind(control)
            if kind:
                if kind in controls:
                    raise ActionError(
                        "ACTION_UNSUPPORTED",
                        f"X schedule dialog contains multiple {kind} controls.",
                    )
                controls[kind] = control

        if not controls:
            # Compatibility fallback for the stable X layout when labels are
            # temporarily absent. The six-control layout adds AM/PM last.
            order = ["month", "day", "year", "hour", "minute"]
            if count == 6:
                order.append("meridiem")
            controls = {kind: selects.nth(index) for index, kind in enumerate(order)}

        required = {"month", "day", "year", "hour", "minute"}
        missing = sorted(required - controls.keys())
        if missing:
            raise ActionError(
                "ACTION_UNSUPPORTED",
                "X schedule controls could not be identified by their labels.",
                {"missing": missing},
            )

        twelve_hour = "meridiem" in controls
        hour = scheduled.hour % 12 or 12 if twelve_hour else scheduled.hour
        await self._select_schedule_number(controls["month"], scheduled.month, "month")
        await self._select_schedule_number(controls["day"], scheduled.day, "day")
        await self._select_schedule_number(controls["year"], scheduled.year, "year")
        await self._select_schedule_number(controls["hour"], hour, "hour")
        await self._select_schedule_number(controls["minute"], scheduled.minute, "minute")
        if twelve_hour:
            await self._select_schedule_meridiem(
                controls["meridiem"], "AM" if scheduled.hour < 12 else "PM"
            )

    async def publish_schedule(self, page: Page, payload: dict[str, Any], options: ExecutionOptions) -> dict[str, Any]:
        requested = parse_schedule(payload.get("scheduleAt"))
        scheduled, timezone_name = await self._schedule_wall_time(page, requested)
        if scheduled.astimezone(UTC) <= datetime.now(UTC) + timedelta(minutes=1):
            raise ActionError("INVALID_SCHEDULE_TIME", "Schedule time must be at least one minute in the future.")
        text = str(payload.get("text") or "")
        if not text.strip() and not payload.get("media"):
            raise ActionError("CONTENT_MISMATCH", "Post content is empty.")
        if options.dry_run:
            return {
                "status": "success",
                "dryRun": True,
                "wouldExecute": "publish.schedule",
                "contentHash": hash_text(text),
                "scheduleAt": requested.isoformat(),
                "profileScheduleAt": scheduled.isoformat(),
                "profileTimezone": timezone_name,
            }
        editor = await self._open_post_composer(page, options)
        existing = (await editor.inner_text()).strip()
        if existing:
            raise ActionError("DRAFT_CONFLICT", "Refusing to overwrite an existing draft.")
        surface = editor.locator("xpath=ancestor::*[@role='dialog' or @aria-modal='true'][1]")
        if not await surface.count():
            surface = editor.locator("xpath=ancestor::*[.//*[@data-testid='scheduleOption']][1]")
        schedule_button = surface.locator('[data-testid="scheduleOption"]')
        if not await schedule_button.count():
            raise ActionError("ACTION_UNSUPPORTED", "The selected composer does not expose native scheduling.")
        await self._click(schedule_button, "schedule option", options)
        dialog = page.get_by_role("dialog").filter(has=page.locator('[data-testid="scheduledConfirmationPrimaryAction"]')).last
        try:
            await dialog.wait_for(state="visible", timeout=options.timeout_ms)
        except Exception as error:
            raise ActionError("ACTION_UNSUPPORTED", "X schedule dialog did not expose a supported layout.") from error
        await self._fill_schedule_controls(dialog, scheduled)
        confirm = dialog.locator('[data-testid="scheduledConfirmationPrimaryAction"]')
        await self._click(confirm, "schedule confirmation", options)
        _, final_button = await self._prepare_composer(page, editor, text, options) if text else (surface, surface.locator('[data-testid="tweetButton"], [data-testid="tweetButtonInline"]').first)
        await self._attach_media(editor, payload, options)
        return await self._submit_composer(page, editor, final_button, text, options, "publish.schedule")

    async def message_reply_conversation(self, page: Page, payload: dict[str, Any], options: ExecutionOptions) -> dict[str, Any]:
        if classify_page(page.url) != "conversation":
            raise ActionError("PAGE_UNSUPPORTED", "Open a specific X chat conversation first.")
        text = str(payload.get("text") or "")
        if not text.strip():
            raise ActionError("CONTENT_MISMATCH", "Message text is empty.")
        editors = page.locator('[contenteditable="true"][role="textbox"]')
        editor: Locator | None = None
        for index in range(await editors.count()):
            candidate = editors.nth(index)
            if await candidate.is_visible():
                editor = candidate
        if editor is None:
            raise ActionError("TARGET_NOT_FOUND", "Chat message editor was not found.")
        existing = (await editor.inner_text()).strip()
        if existing:
            raise ActionError("DRAFT_CONFLICT", "Refusing to overwrite an existing chat draft.")
        surface = editor.locator(
            "xpath=ancestor::*[.//*[@data-testid='dm-composer-send-button' or "
            "@data-testid='dmComposerSendButton']][1]"
        )
        button = surface.locator('[data-testid="dm-composer-send-button"], [data-testid="dmComposerSendButton"]')
        if not await button.count():
            raise ActionError("ACTION_UNSUPPORTED", "The chat composer did not expose a local send button.")
        if options.dry_run:
            return {"status": "success", "dryRun": True, "wouldExecute": "message.replyConversation", "contentHash": hash_text(text)}
        await editor.press_sequentially(text, delay=0, timeout=options.timeout_ms)
        if not await button.first.is_enabled():
            raise ActionError("ACTION_UNSUPPORTED", "The chat composer did not expose an enabled local send button.")
        await self._click(button, "chat send", options, mutation=True)
        try:
            await page.wait_for_function("el => !(el?.innerText || '').trim()", arg=await editor.element_handle(), timeout=min(options.timeout_ms, 7000))
            return {"status": "uncertain", "evidence": ["chat-editor-cleared"], "reason": "The chat editor cleared, but no delivered outgoing message was proven. Do not retry automatically.", "contentHash": hash_text(text)}
        except Exception:
            return {"status": "uncertain", "reason": "The chat send button was clicked but the final state was not proven. Do not retry automatically.", "contentHash": hash_text(text)}
