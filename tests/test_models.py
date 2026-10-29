from app.storage.models import Account, ChannelAnalysis, ChannelBinding, ChatBinding, Example

def test_account_has_learning_controls() -> None:
    assert {"learning_status", "learning_started_at", "learning_ends_at", "learning_completed_at", "learning_limit", "learning_prompt"} <= set(Account.__table__.columns.keys())

def test_account_has_pause_and_personal_context_controls() -> None:
    columns = set(Account.__table__.columns.keys())
    assert {"manual_pause", "connection_status", "persona_style_override", "learning_prompt"} <= columns


def test_examples_support_account_specific_scope() -> None:
    assert "account_id" in Example.__table__.columns


def test_chat_bindings_support_auto_discovery() -> None:
    assert "auto_discovered" in ChatBinding.__table__.columns


def test_channels_have_independent_per_account_bindings() -> None:
    assert {"account_id", "channel_id", "enabled", "comment_wait_seconds"} <= set(ChannelBinding.__table__.columns.keys())


def test_channel_analysis_is_stored_separately_from_chat_responses() -> None:
    assert {"account_id", "channel_id", "post_id", "comments_summary", "on_topic", "suggested_response"} <= set(ChannelAnalysis.__table__.columns.keys())
