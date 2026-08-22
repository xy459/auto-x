from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from x_actions_playwright import ActionError, XActions
from x_actions_playwright.adapter import XAdapter
from x_actions_playwright.models import ExecutionOptions


def tweet_html(post_id="100", username="alice", *, liked=False, quote=False, ad=False, own=False):
    state = "unlike" if liked else "like"
    quoted = """
      <div data-testid="quoteTweet">
        <a href="/quoted/status/999"><time datetime="2026-01-01T00:00:00Z"></time></a>
        <a href="/quoted">Quoted</a><a href="/quoted">@quoted</a>
        <div data-testid="tweetText">quoted text</div>
        <button data-testid="like">quoted like</button>
        <a href="/quoted/status/999/photo/1"><img src="https://img/quote.jpg"></a>
      </div>
    """ if quote else ""
    return f"""
      <article data-testid="tweet">
        <div data-testid="User-Name"><a href="/{username}">{username.title()}</a><a href="/{username}">@{username}</a></div>
        <a href="/{username}/status/{post_id}"><time datetime="2026-01-01T00:00:00Z"></time></a>
        <div data-testid="tweetText" lang="en">hello {post_id}</div>
        <div role="group" aria-label="2 Replies, 3 Reposts, 1.2K Likes, 4K Views"></div>
        <button data-testid="reply">reply</button>
        <button data-testid="{state}" onclick="this.dataset.testid=this.dataset.testid==='like'?'unlike':'like'">like state</button>
        <button data-testid="bookmark" onclick="this.dataset.testid='removeBookmark'">bookmark</button>
        <button data-testid="retweet" onclick="document.querySelector('#repost-menu').hidden=false">repost</button>
        <button data-testid="share" onclick="document.querySelector('#share-menu').hidden=false">share</button>
        <button data-testid="caret" onclick="document.querySelector('#delete-menu').hidden=false">more</button>
        <a href="/{username}/status/{post_id}/photo/1"><img alt="main" src="https://img/main.jpg"></a>
        {quoted}
        {'<span>Ad</span>' if ad else ''}
      </article>
      <div id="repost-menu" hidden><div role="menuitem" data-testid="retweetConfirm" onclick="document.querySelector('[data-testid=retweet]').dataset.testid='unretweet';this.remove()">Repost</div><div role="menuitem">Quote</div></div>
      <div id="share-menu" hidden><div role="menuitem" data-testid="copyLink" onclick="this.remove()">Copy link</div></div>
      <div id="delete-menu" hidden><div role="menuitem" onclick="document.querySelector('[data-testid=confirmationSheetConfirm]').hidden=false">Delete</div></div>
      <div role="dialog"><button data-testid="confirmationSheetConfirm" hidden onclick="document.querySelector('article').remove();this.closest('[role=dialog]').remove()">Delete</button></div>
      {'<div data-testid="SideNav_AccountSwitcher_Button">Alice\n@alice</div>' if own else ''}
    """


@pytest.mark.asyncio
async def test_post_details_scope_main_post_not_quote(page):
    await page.set_content(tweet_html(quote=True))
    actions = XActions()
    result = await actions.post.getDetails(page, {"tweetId": "100"})
    post = result.data["post"]
    assert post["content"]["text"] == "hello 100"
    assert post["media"]["imageCount"] == 1
    assert post["quotedPost"]["postId"] == "999"
    assert post["metrics"]["likeCount"] == 1200


@pytest.mark.asyncio
async def test_like_uses_target_state_and_never_quote_button(page):
    await page.set_content(tweet_html(quote=True))
    actions = XActions()
    result = await actions.interaction.like(page, {"tweetId": "100"}, {"confirmLive": True})
    assert result.status == "success"
    assert await page.locator('article > button[data-testid="unlike"]').count() == 1
    assert await page.locator('[data-testid="quoteTweet"] [data-testid="like"]').count() == 1
    second = await actions.interaction.like(page, {"tweetId": "100"}, {"confirmLive": True})
    assert second.status == "skipped"


@pytest.mark.asyncio
async def test_profile_posts_exclude_other_authors_replies_and_pinned_by_default(page):
    await page.route("https://x.com/**", lambda route: route.fulfill(status=200, body="<html></html>"))
    await page.goto("https://x.com/alice")
    reply = tweet_html(post_id="100", username="alice").replace(
        '<article data-testid="tweet">',
        '<article data-testid="tweet"><div>Replying to @bob</div>',
        1,
    )
    await page.set_content(
        reply
        + tweet_html(post_id="101", username="alice")
        + tweet_html(post_id="200", username="bob")
    )
    result = await XActions().account.listPosts(
        page,
        {"includeReplies": False, "includePinned": False},
    )
    assert [post["postId"] for post in result.data["posts"]] == ["101"]


@pytest.mark.asyncio
async def test_dry_run_does_not_click_write_action(page):
    await page.set_content(tweet_html())
    result = await XActions().interaction.like(page, {"tweetId": "100"}, {"dryRun": True})
    assert result.status == "success"
    assert await page.locator('article > button[data-testid="like"]').count() == 1


@pytest.mark.asyncio
async def test_selected_post_is_scoped_to_its_page(page):
    await page.set_content(tweet_html(post_id="100"))
    second = await page.context.new_page()
    await second.set_content(tweet_html(post_id="200"))
    actions = XActions()
    await actions.context.selectPost(page, {"tweetId": "100"})
    selected = await actions.post.getDetails(page)
    assert selected.data["post"]["postId"] == "100"
    with pytest.raises(ActionError) as caught:
        await actions.post.getDetails(second)
    assert caught.value.code == "TARGET_NOT_FOUND"
    await second.close()


@pytest.mark.asyncio
async def test_selected_post_id_is_used_by_delete_result(page):
    await page.set_content(tweet_html(own=True))
    actions = XActions()
    await actions.context.selectPost(page, {"tweetId": "100"})

    result = await actions.post.delete(page, options={"confirmLive": True})

    assert result.status == "success"
    assert result.data["tweetId"] == "100"


@pytest.mark.asyncio
async def test_selected_post_id_is_used_to_open_image(page):
    await page.route(
        "https://x.com/**",
        lambda route: route.fulfill(status=200, body="<html></html>"),
    )
    await page.goto("https://x.com/home")
    await page.set_content(tweet_html())
    actions = XActions()
    await actions.context.selectPost(page, {"tweetId": "100"})

    result = await actions.image.open(page, {"index": 1})

    assert result.status == "success"
    assert result.data["viewer"]["tweetId"] == "100"
    assert result.data["viewer"]["index"] == 1


@pytest.mark.asyncio
async def test_reply_dry_run_does_not_open_or_mutate_composer(page):
    await page.set_content(
        tweet_html()
        + """
        <div id="composer" hidden>
          <div data-testid="tweetTextarea_0" contenteditable="true"></div>
          <button data-testid="tweetButton">Reply</button>
        </div>
        <script>
          document.querySelector('article > [data-testid=reply]').onclick = () => {
            document.querySelector('#composer').hidden = false;
          };
        </script>
        """
    )
    result = await XActions().interaction.reply(
        page,
        {"tweetId": "100", "text": "dry run"},
        {"dryRun": True},
    )
    assert result.status == "success"
    assert await page.locator("#composer").is_hidden()
    assert await page.locator('[data-testid="tweetTextarea_0"]').inner_text() == ""


@pytest.mark.asyncio
async def test_reply_uses_reused_visible_dialog_composer_not_background_editor(page):
    await page.set_content(
        tweet_html()
        + """
        <section id="background-composer">
          <div data-testid="tweetTextarea_0" contenteditable="true">existing draft</div>
          <button data-testid="tweetButtonInline">Post</button>
        </section>
        <div id="reply-dialog" role="dialog" hidden>
          <div id="reply-editor" data-testid="tweetTextarea_0" contenteditable="true"></div>
          <button id="reply-submit" data-testid="tweetButton">Reply</button>
        </div>
        <script>
          document.querySelector('article > [data-testid=reply]').onclick = () => {
            document.querySelector('#reply-dialog').hidden = false;
          };
          document.querySelector('#reply-submit').onclick = () => {
            const editor = document.querySelector('#reply-editor');
            document.body.dataset.submittedReply = editor.innerText;
            editor.innerText = '';
            document.querySelector('#reply-dialog').hidden = true;
          };
        </script>
        """
    )

    await XActions().interaction.reply(
        page,
        {"tweetId": "100", "text": "generated reply"},
        {"confirmLive": True, "timeoutMs": 2_000},
    )

    assert await page.locator("body").get_attribute("data-submitted-reply") == "generated reply"
    assert await page.locator("#background-composer [contenteditable=true]").inner_text() == "existing draft"


@pytest.mark.asyncio
async def test_ad_target_fails_closed(page):
    await page.set_content(tweet_html(ad=True))
    with pytest.raises(ActionError) as caught:
        await XActions().post.getDetails(page, {"tweetId": "100"})
    assert caught.value.code == "TARGET_UNSAFE"


@pytest.mark.asyncio
async def test_native_repost_menu_and_postcondition(page):
    await page.set_content(tweet_html())
    result = await XActions().interaction.repost(page, {"tweetId": "100"}, {"confirmLive": True})
    assert result.status == "success"
    assert await page.locator('[data-testid="unretweet"]').count() == 1


@pytest.mark.asyncio
async def test_delete_checks_owner_and_confirms(page):
    await page.set_content(tweet_html(own=True))
    result = await XActions().post.delete(page, {"tweetId": "100"}, {"confirmLive": True})
    assert result.status == "success"
    assert result.data["deleted"] is True


@pytest.mark.asyncio
async def test_delete_other_users_post_is_rejected(page):
    html = tweet_html(username="bob") + '<div data-testid="SideNav_AccountSwitcher_Button">Alice\n@alice</div>'
    await page.set_content(html)
    with pytest.raises(ActionError) as caught:
        await XActions().post.delete(page, {"tweetId": "100"}, {"confirmLive": True})
    assert caught.value.code == "TARGET_UNSAFE"


@pytest.mark.asyncio
async def test_timeline_open_selects_requested_tab(page):
    await page.set_content("""
      <div role="tab" aria-selected="true" onclick="for(const x of document.querySelectorAll('[role=tab]'))x.ariaSelected='false';this.ariaSelected='true'">For you</div>
      <div role="tab" aria-selected="false" onclick="for(const x of document.querySelectorAll('[role=tab]'))x.ariaSelected='false';this.ariaSelected='true'">Following</div>
    """)
    result = await XActions().timeline.open(page, {"feed": "following"})
    assert result.status == "success"
    assert await page.get_by_role("tab", name="Following").get_attribute("aria-selected") == "true"


@pytest.mark.asyncio
async def test_timeline_open_waits_for_home_tabs_to_render(page):
    await page.set_content("""
      <div id="tabs"></div>
      <script>
        setTimeout(() => {
          document.querySelector('#tabs').innerHTML = '<div role="tab" aria-selected="true">为你推荐</div>';
        }, 50);
      </script>
    """)

    result = await XActions().timeline.open(
        page,
        {"feed": "for-you"},
        {"timeoutMs": 1000},
    )

    assert result.status == "skipped"
    assert result.data["timeline"] == "for-you"


@pytest.mark.asyncio
async def test_account_session_distinguishes_signed_in_and_out(page):
    await page.set_content('<div data-testid="SideNav_AccountSwitcher_Button">Alice\n@alice</div>')
    signed_in = await XActions().account.getSession(page)
    assert signed_in.data["session"]["username"] == "alice"
    await page.set_content('<a href="/i/flow/login">Log in</a>')
    signed_out = await XActions().account.getSession(page)
    assert signed_out.data["session"]["state"] == "unauthenticated"


@pytest.mark.asyncio
async def test_login_fills_segmented_two_factor_inputs(page):
    await page.set_content(
        """
        <div role="dialog">
          <input inputmode="numeric" maxlength="1">
          <input inputmode="numeric" maxlength="1">
          <input inputmode="numeric" maxlength="1">
          <input inputmode="numeric" maxlength="1">
          <input inputmode="numeric" maxlength="1">
          <input inputmode="numeric" maxlength="1">
          <button>続ける</button>
        </div>
        """
    )
    adapter = XAdapter()
    inputs = page.locator('input[inputmode="numeric"]')

    await adapter._fill_two_factor_code(
        page,
        inputs.first,
        "123456",
        ExecutionOptions(timeout_ms=2_000),
        typing_delay_ms=20,
    )

    assert [await inputs.nth(index).input_value() for index in range(6)] == list("123456")
    assert await adapter._click_button_by_name(
        page,
        ["続ける"],
        ExecutionOptions(timeout_ms=2_000),
    )


@pytest.mark.asyncio
async def test_login_recognizes_japanese_account_not_found(page):
    await page.set_content(
        "<main>そのユーザー名を使用している有効なアカウントが見つかりません。</main>"
    )

    assert await XAdapter()._login_challenge_reason(page) == "credentials_rejected"


@pytest.mark.asyncio
async def test_profile_terminal_errors_are_structured(page):
    await page.goto("https://x.com/alice")
    await page.set_content('<main data-testid="primaryColumn">Account suspended</main>')
    with pytest.raises(ActionError) as caught:
        await XActions().account.getDetails(page)
    assert caught.value.code == "ACCOUNT_SUSPENDED"


@pytest.mark.asyncio
async def test_publish_uses_native_playwright_typing_and_success_toast(page):
    await page.set_content("""
      <button data-testid="SideNav_NewTweet_Button" onclick="document.querySelector('#dialog').hidden=false">Post</button>
      <div id="dialog" role="dialog" hidden>
        <div data-testid="tweetTextarea_0" contenteditable="true" role="textbox" oninput="document.querySelector('[data-testid=tweetButton]').disabled=!this.innerText.trim()"></div>
        <button data-testid="tweetButton" disabled onclick="document.querySelector('[data-testid=toast]').hidden=false;document.querySelector('[data-testid=tweetTextarea_0]').innerText=''">Post</button>
      </div>
      <div data-testid="toast" hidden>Your post was sent successfully</div>
    """)
    result = await XActions().publish.post(page, {"text": "hello from Playwright"}, {"confirmLive": True})
    assert result.status == "success"
    assert result.data["contentHash"]


@pytest.mark.asyncio
async def test_publish_fails_closed_when_composer_layout_is_unsupported(page):
    await page.set_content("<main>No composer</main>")
    with pytest.raises(ActionError) as caught:
        await XActions(default_timeout_ms=300).publish.post(page, {"text": "hello"}, {"confirmLive": True, "timeoutMs": 300})
    assert caught.value.code in {"ACTION_UNSUPPORTED", "TARGET_NOT_FOUND", "TIMEOUT"}


@pytest.mark.asyncio
async def test_schedule_dry_run_converts_absolute_time_to_profile_timezone(page):
    timezone_name = await page.evaluate("Intl.DateTimeFormat().resolvedOptions().timeZone")
    requested = datetime(2035, 8, 20, 13, 45, tzinfo=UTC)

    result = await XActions().publish.schedule(
        page,
        {"text": "scheduled", "scheduleAt": requested.isoformat()},
        {"dryRun": True},
    )

    expected = requested.astimezone(ZoneInfo(timezone_name))
    assert result.data["profileTimezone"] == timezone_name
    assert result.data["profileScheduleAt"] == expected.isoformat()


@pytest.mark.asyncio
async def test_schedule_controls_support_labeled_24_hour_layout(page):
    await page.set_content(
        """
        <div id="schedule">
          <label>Year<select aria-label="Year"><option value="2035">2035</option></select></label>
          <label>Month<select aria-label="Month"><option value="8">August</option></select></label>
          <label>Day<select aria-label="Day"><option value="20">20</option></select></label>
          <label>Minute<select aria-label="Minute"><option value="45">45</option></select></label>
          <label>Hour<select aria-label="Hour"><option value="13">13</option></select></label>
        </div>
        """
    )

    await XAdapter()._fill_schedule_controls(
        page.locator("#schedule"), datetime(2035, 8, 20, 13, 45)
    )

    assert await page.get_by_label("Hour").input_value() == "13"
    assert await page.get_by_label("Minute").input_value() == "45"


@pytest.mark.asyncio
async def test_schedule_controls_support_labeled_12_hour_layout_and_pm(page):
    await page.set_content(
        """
        <div id="schedule">
          <label>Month<select aria-label="Month"><option value="8">August</option></select></label>
          <label>Day<select aria-label="Day"><option value="20">20</option></select></label>
          <label>Year<select aria-label="Year"><option value="2035">2035</option></select></label>
          <label>Hour<select aria-label="Hour"><option value="1">1</option></select></label>
          <label>Minute<select aria-label="Minute"><option value="45">45</option></select></label>
          <label>AM/PM<select aria-label="AM/PM"><option value="am">AM</option><option value="pm">PM</option></select></label>
        </div>
        """
    )

    await XAdapter()._fill_schedule_controls(
        page.locator("#schedule"), datetime(2035, 8, 20, 13, 45)
    )

    assert await page.get_by_label("Hour").input_value() == "1"
    assert await page.get_by_label("AM/PM").input_value() == "pm"


@pytest.mark.asyncio
async def test_message_requires_specific_conversation(page):
    await page.goto("https://x.com/home")
    with pytest.raises(ActionError) as caught:
        await XActions().message.replyConversation(page, {"text": "hello"}, {"confirmLive": True})
    assert caught.value.code == "PAGE_UNSUPPORTED"


@pytest.mark.asyncio
async def test_message_dry_run_does_not_type(page):
    await page.goto("https://x.com/i/chat/conversation-1")
    await page.set_content(
        """
        <div>
          <div contenteditable="true" role="textbox"></div>
          <button data-testid="dm-composer-send-button">Send</button>
        </div>
        """
    )
    result = await XActions().message.replyConversation(page, {"text": "dry run"}, {"dryRun": True})
    assert result.status == "success"
    assert await page.get_by_role("textbox").inner_text() == ""
