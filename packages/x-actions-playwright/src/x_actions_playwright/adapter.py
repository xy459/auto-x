from __future__ import annotations

import asyncio
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Awaitable, Callable
from urllib.parse import quote, urlparse

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


class XAdapter:
    def __init__(self) -> None:
        self.selected_tweet_id: str | None = None

    async def dispatch(
        self,
        page: Page,
        handler: str,
        payload: dict[str, Any],
        options: ExecutionOptions,
    ) -> dict[str, Any]:
        method = getattr(self, handler, None)
        if not method:
            raise ActionError("ACTION_UNSUPPORTED", f"No Playwright implementation for handler {handler}.")
        return await method(page, payload, options)

    async def _count(self, locator: Locator) -> int:
        return int(await locator.count())

    async def _visible(self, locator: Locator) -> bool:
        return bool(await locator.count() and await locator.first.is_visible())

    async def _click(self, locator: Locator, description: str, options: ExecutionOptions) -> None:
        try:
            target = locator.first
            if not await target.count():
                raise ActionError("TARGET_NOT_FOUND", f"{description} was not found.")
            if not await target.is_visible():
                raise ActionError("ELEMENT_NOT_VISIBLE", f"{description} is not visible.")
            if not await target.is_enabled():
                raise ActionError("ELEMENT_DISABLED", f"{description} is disabled.")
            await target.scroll_into_view_if_needed(timeout=options.timeout_ms)
            await target.click(timeout=options.timeout_ms)
        except ActionError:
            raise
        except Exception as error:
            normalized = normalize_error(error)
            if normalized.code == "UNEXPECTED_ERROR":
                normalized = ActionError("ELEMENT_BLOCKED", f"Could not click {description}: {error}", retryable=True)
            raise normalized from error

    def _tweet_locator(self, page: Page, tweet_id: str | None) -> Locator:
        target = str(tweet_id or self.selected_tweet_id or "")
        articles = page.locator('article[data-testid="tweet"]')
        if not target:
            return articles.first
        return articles.filter(has=page.locator(f'a[href*="/status/{target}"] time')).first

    async def _require_tweet(self, page: Page, tweet_id: Any) -> Locator:
        article = self._tweet_locator(page, str(tweet_id or ""))
        if not await article.count():
            raise ActionError("TARGET_NOT_FOUND", f"Post {tweet_id or self.selected_tweet_id or ''} is not in the current DOM.")
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
        raw = await article.evaluate(POST_EXTRACT_JS, {})
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
        return {"status": "success", "context": {"pageType": classify_page(page.url), "url": page.url, "title": await page.title(), "account": account, "tweets": tweets, "selectedTweetId": self.selected_tweet_id}}

    async def select_post(self, page: Page, payload: dict[str, Any], options: ExecutionOptions) -> dict[str, Any]:
        article = await self._require_tweet(page, payload.get("tweetId"))
        post = await self._post(article, include_ads=True)
        self.selected_tweet_id = post["postId"]
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
        if not await tab.count():
            raise ActionError("TARGET_NOT_FOUND", f"Could not find the {feed} Home tab.")
        if await tab.get_attribute("aria-selected") == "true":
            return {"status": "skipped", "reason": f"{feed} is already selected.", "timeline": feed, "evidence": ["aria-selected=true"]}
        await self._click(tab, f"{feed} Home tab", options)
        try:
            names = ["For you", "为你推荐", "推荐"] if feed == "for-you" else ["Following", "正在关注", "关注"]
            await page.wait_for_function("names => [...document.querySelectorAll('[role=tab]')].some(t => t.getAttribute('aria-selected') === 'true' && names.includes((t.innerText||t.textContent||'').trim()))", arg=names, timeout=options.timeout_ms)
        except Exception:
            current = self._timeline_tab(page, feed)
            if not await current.count() or await current.get_attribute("aria-selected") != "true":
                raise ActionError("TIMEOUT", f"Timed out verifying {feed} tab selection.", retryable=True)
        return {"status": "success", "timeline": feed, "evidence": ["aria-selected:false->true"]}

    async def timeline_browse(self, page: Page, payload: dict[str, Any], options: ExecutionOptions) -> dict[str, Any]:
        return await self._browse_timeline(page, payload, options, collect=False)

    async def timeline_collect(self, page: Page, payload: dict[str, Any], options: ExecutionOptions) -> dict[str, Any]:
        return await self._browse_timeline(page, payload, options, collect=True)

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
        tweet_id = payload.get("tweetId")
        article = await self._require_tweet(page, tweet_id)
        post = await self._post(article)
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
        await self._click(confirmation, "Delete confirmation", options)
        try:
            await article.wait_for(state="detached", timeout=min(options.timeout_ms, 10_000))
            return {"status": "success", "tweetId": str(tweet_id), "deleted": True, "evidence": ["post-removed"]}
        except Exception:
            toast = page.locator('[data-testid="toast"]')
            text = await toast.inner_text() if await toast.count() else ""
            if re.search(r"failed|error|try again|出错|失败", text, re.I):
                raise ActionError("SUBMISSION_REJECTED", "X rejected the post deletion.", {"toast": text})
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
        tweet_id = str(payload.get("tweetId"))
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

    async def _comment_article(self, page: Page, comment_id: Any) -> Locator:
        await self.comment_get(page, {"commentId": comment_id}, ExecutionOptions())
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
        await self._click(control, f"{action} button", options)
        if menu_confirm is not None:
            await self._click(menu_confirm, f"{action} confirmation", options)
        try:
            await (await self._owned_control(article, (desired_test_id,))).wait_for(state="visible", timeout=min(options.timeout_ms, 7000))
            return {"status": "success", "target": await self._post(article, include_ads=True), "evidence": [f"state:{current_test_id}->{desired_test_id}"]}
        except Exception:
            return {"status": "uncertain", "target": post, "reason": f"{action} was clicked but the final target state was not observed. Do not retry automatically."}

    async def comment_like(self, page: Page, payload: dict[str, Any], options: ExecutionOptions) -> dict[str, Any]:
        article = await self._comment_article(page, payload.get("commentId"))
        return await self._state_action(page, article, options, action="comment.like", current_test_id="like", desired_test_id="unlike", click_test_ids=("like",))

    async def comment_unlike(self, page: Page, payload: dict[str, Any], options: ExecutionOptions) -> dict[str, Any]:
        article = await self._comment_article(page, payload.get("commentId"))
        return await self._state_action(page, article, options, action="comment.unlike", current_test_id="unlike", desired_test_id="like", click_test_ids=("unlike",))

    async def _open_fresh_composer(self, page: Page, trigger: Locator, options: ExecutionOptions, *, kind: str) -> Locator:
        editors = page.locator('[data-testid="tweetTextarea_0"][contenteditable="true"]')
        before = await editors.count()
        await self._click(trigger, f"{kind} trigger", options)
        try:
            await page.wait_for_function("([before]) => document.querySelectorAll('[data-testid=\"tweetTextarea_0\"][contenteditable=\"true\"]').length > before", arg=[before], timeout=options.timeout_ms)
        except Exception as error:
            raise ActionError("TARGET_NOT_FOUND", f"A fresh {kind} composer did not open.") from error
        return editors.last

    async def _prepare_composer(self, page: Page, editor: Locator, text: str, options: ExecutionOptions) -> tuple[Locator, Locator]:
        if not text.strip():
            raise ActionError("CONTENT_MISMATCH", "Composer text is empty.")
        existing = (await editor.inner_text()).strip()
        if existing:
            raise ActionError("DRAFT_CONFLICT", "Refusing to overwrite an existing draft.", {"existingHash": hash_text(existing), "existingLength": len(existing)})
        await editor.click(timeout=options.timeout_ms)
        await editor.press_sequentially(text, delay=0, timeout=options.timeout_ms)
        actual = (await editor.inner_text()).replace("\r\n", "\n").strip()
        if actual != text.replace("\r\n", "\n").strip():
            raise ActionError("CONTENT_MISMATCH", "Editor content does not match requested text.", {"expectedHash": hash_text(text), "actualHash": hash_text(actual)})
        surface = editor.locator("xpath=ancestor::*[@role='dialog' or @aria-modal='true'][1]")
        if not await surface.count():
            surface = editor.locator("xpath=ancestor::*[.//*[@data-testid='tweetButton' or @data-testid='tweetButtonInline']][1]")
        if not await surface.count():
            raise ActionError("STATE_UNKNOWN", "Could not bind the composer to its local surface.")
        button = surface.locator('[data-testid="tweetButton"], [data-testid="tweetButtonInline"]')
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
        await self._click(button, f"{action} submit", options)
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
        article = await self._comment_article(page, payload.get("commentId"))
        trigger = await self._owned_control(article, ("reply",))
        editor = await self._open_fresh_composer(page, trigger, options, kind="comment reply")
        _, button = await self._prepare_composer(page, editor, str(payload.get("text") or ""), options)
        return await self._submit_composer(page, editor, button, str(payload["text"]), options, "comment.reply")

    async def _quote(self, page: Page, article: Locator, text: str, options: ExecutionOptions, action: str) -> dict[str, Any]:
        repost = await self._owned_control(article, ("retweet", "unretweet"))
        await self._click(repost, f"{action} repost menu", options)
        quote_item = page.get_by_role("menuitem", name=re.compile(r"^(Quote|引用|引用帖子)$", re.I))
        editor = await self._open_fresh_composer(page, quote_item, options, kind=action)
        _, button = await self._prepare_composer(page, editor, text, options)
        return await self._submit_composer(page, editor, button, text, options, action)

    async def comment_quote(self, page: Page, payload: dict[str, Any], options: ExecutionOptions) -> dict[str, Any]:
        return await self._quote(page, await self._comment_article(page, payload.get("commentId")), str(payload.get("text") or ""), options, "comment.quote")

    async def comment_delete_reply(self, page: Page, payload: dict[str, Any], options: ExecutionOptions) -> dict[str, Any]:
        if not payload.get("replyId"):
            raise ActionError("TARGET_NOT_FOUND", "comment.deleteReply requires replyId.")
        return await self.post_delete(page, {**payload, "tweetId": payload["replyId"]}, options)

    async def interaction_reply(self, page: Page, payload: dict[str, Any], options: ExecutionOptions) -> dict[str, Any]:
        article = await self._require_tweet(page, payload.get("tweetId"))
        trigger = await self._owned_control(article, ("reply",))
        editor = await self._open_fresh_composer(page, trigger, options, kind="post reply")
        _, button = await self._prepare_composer(page, editor, str(payload.get("text") or ""), options)
        return await self._submit_composer(page, editor, button, str(payload["text"]), options, "interaction.reply")

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

    def _profile_relationship_locator(self, page: Page, username: str) -> Locator:
        primary = page.locator('[data-testid="primaryColumn"]')
        return primary.locator(f'button[data-testid="{username}-follow"], button[data-testid="{username}-unfollow"], button[aria-label="Follow @{username}"], button[aria-label="Following @{username}"], button[aria-label="Requested @{username}"]').first

    async def account_follow(self, page: Page, payload: dict[str, Any], options: ExecutionOptions) -> dict[str, Any]:
        return await self._relationship_action(page, options, follow=True)

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
        await self._click(control, "Follow" if follow else "Unfollow", options)
        if not follow:
            confirm = page.locator('[data-testid="confirmationSheetConfirm"]')
            if not await confirm.count():
                confirm = page.get_by_role("dialog").get_by_role("button", name=re.compile(r"^(Unfollow|取消关注)$", re.I))
            await self._click(confirm, "Unfollow confirmation", options)
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
            path = Path(str(item.get("path") if isinstance(item, dict) else item)).expanduser()
            if not path.is_file():
                raise ActionError("CONTENT_MISMATCH", f"Media file does not exist: {path}")
            total += path.stat().st_size
            paths.append(str(path))
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

    async def publish_schedule(self, page: Page, payload: dict[str, Any], options: ExecutionOptions) -> dict[str, Any]:
        scheduled = parse_schedule(payload.get("scheduleAt"))
        now = datetime.now(scheduled.tzinfo or UTC)
        if scheduled <= now + timedelta(minutes=1):
            raise ActionError("INVALID_SCHEDULE_TIME", "Schedule time must be at least one minute in the future.")
        text = str(payload.get("text") or "")
        if not text.strip() and not payload.get("media"):
            raise ActionError("CONTENT_MISMATCH", "Post content is empty.")
        if options.dry_run:
            return {"status": "success", "dryRun": True, "wouldExecute": "publish.schedule", "contentHash": hash_text(text), "scheduleAt": scheduled.isoformat()}
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
        selects = dialog.locator("select")
        if await selects.count() not in {5, 6}:
            raise ActionError("ACTION_UNSUPPORTED", "X schedule controls have an unsupported structure.")
        values = [scheduled.month, scheduled.day, scheduled.year, scheduled.hour, scheduled.minute]
        for index, value in enumerate(values):
            await selects.nth(index).select_option(str(value))
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
        await editor.press_sequentially(text, delay=0, timeout=options.timeout_ms)
        surface = editor.locator("xpath=ancestor::*[.//*[@data-testid='dm-composer-send-button' or @data-testid='dmComposerSendButton']][1]")
        button = surface.locator('[data-testid="dm-composer-send-button"], [data-testid="dmComposerSendButton"]')
        if not await button.count() or not await button.first.is_enabled():
            raise ActionError("ACTION_UNSUPPORTED", "The chat composer did not expose an enabled local send button.")
        if options.dry_run:
            return {"status": "success", "dryRun": True, "wouldExecute": "message.replyConversation", "contentHash": hash_text(text)}
        await self._click(button, "chat send", options)
        try:
            await page.wait_for_function("el => !(el?.innerText || '').trim()", arg=await editor.element_handle(), timeout=min(options.timeout_ms, 7000))
            return {"status": "success", "evidence": ["chat-editor-cleared"], "contentHash": hash_text(text)}
        except Exception:
            return {"status": "uncertain", "reason": "The chat send button was clicked but the final state was not proven. Do not retry automatically.", "contentHash": hash_text(text)}
