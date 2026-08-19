from __future__ import annotations

import pytest

from x_actions_playwright import ACTION_CATEGORIES, ACTION_DEFINITIONS, XActions

EXPECTED_ACTIONS = {
    "context.inspect", "context.selectPost",
    "timeline.open", "timeline.browse", "timeline.collect",
    "post.openDetails", "post.getDetails", "post.getType", "post.delete", "post.exitDetails", "post.expand", "post.getUrl", "post.copyLink",
    "image.open", "image.previous", "image.next", "image.close",
    "video.play", "video.pause", "video.unmute", "video.mute",
    "comment.listVisible", "comment.collect", "comment.get", "comment.like", "comment.unlike", "comment.reply", "comment.quote", "comment.deleteReply",
    "interaction.reply", "interaction.quote", "interaction.like", "interaction.unlike", "interaction.bookmark", "interaction.repost", "interaction.undoRepost", "interaction.sendViaChat",
    "browse.scrollTimeline", "browse.openForYou", "browse.openFollowing", "browse.browseForYou", "browse.browseFollowing", "browse.browseAndCollectForYou", "browse.browseAndCollectFollowing", "browse.scrollComments", "browse.wait",
    "account.search", "account.getSession", "account.getDetails", "account.listCandidates", "account.follow", "account.unfollow",
    "publish.post", "publish.schedule", "message.replyConversation",
}


def test_catalog_has_all_55_canonical_actions_and_11_categories():
    assert set(ACTION_DEFINITIONS) == EXPECTED_ACTIONS
    assert len(ACTION_DEFINITIONS) == 55
    assert set(ACTION_CATEGORIES) == {action.split(".", 1)[0] for action in EXPECTED_ACTIONS}
    assert len(ACTION_CATEGORIES) == 11


def test_every_action_has_complete_contract_and_implementation():
    actions = XActions()
    for action_id, definition in ACTION_DEFINITIONS.items():
        assert definition.failure_modes, action_id
        assert definition.edge_cases, action_id
        assert definition.input_schema["type"] == "object"
        assert definition.output_schema["type"] == "object"
        assert definition.access in {"read", "write"}
        assert definition.retry_policy in {"safe", "never"}
        assert hasattr(actions.adapter, definition.handler), action_id
        assert callable(getattr(getattr(actions, definition.category), definition.method)), action_id


@pytest.mark.parametrize("action_id", sorted(EXPECTED_ACTIONS))
def test_each_canonical_action_is_individually_registered(action_id):
    definition = ACTION_DEFINITIONS[action_id]
    actions = XActions()
    assert definition.id == action_id
    assert definition.category in ACTION_CATEGORIES
    assert definition.failure_modes
    assert definition.edge_cases
    assert hasattr(actions.adapter, definition.handler)
    assert callable(getattr(getattr(actions, definition.category), definition.method))


def test_write_actions_are_never_auto_retry_and_require_confirmation():
    writes = [definition for definition in ACTION_DEFINITIONS.values() if definition.access == "write"]
    assert writes
    assert all(item.retry_policy == "never" for item in writes)
    assert all(item.confirmation == "required" for item in writes)


def test_publish_is_playwright_enabled_and_fail_closed_by_layout():
    assert ACTION_DEFINITIONS["publish.post"].enabled is True
    assert ACTION_DEFINITIONS["publish.schedule"].enabled is True
    codes = {mode.code for mode in ACTION_DEFINITIONS["publish.post"].failure_modes}
    assert {"ACTION_UNSUPPORTED", "CONTENT_MISMATCH", "DRAFT_CONFLICT", "SUBMISSION_REJECTED"}.issubset(codes | {"ACTION_UNSUPPORTED"})
