from app.storage.models import Account, AIAgent, ChannelAnalysis, ChannelBinding, ChatBinding, Example
from app.services.channels import (
    effective_channel_chat_replies_enabled,
    effective_channel_reply_mode,
    resolve_channel_comment_policy,
)

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
    assert {"account_id", "channel_id", "enabled", "comment_wait_seconds", "reply_mode_override"} <= set(ChannelBinding.__table__.columns.keys())


def test_channel_analysis_is_stored_separately_from_chat_responses() -> None:
    assert {"account_id", "channel_id", "post_id", "comments_summary", "on_topic", "suggested_response"} <= set(ChannelAnalysis.__table__.columns.keys())

def test_channels_have_reply_and_inherited_settings() -> None:
    account_columns = set(Account.__table__.columns.keys())
    channel_columns = set(ChannelBinding.__table__.columns.keys())
    assert {
        "channel_post_instructions", "channel_comments_instructions",
        "channel_restrictions_instructions",
        "channel_reply_probability", "channel_reply_cooldown_seconds", "channel_reply_mode",
        "channel_chat_replies_enabled",
    } <= account_columns
    assert {
        "reply_enabled", "discussion_group_id", "post_instructions_override", "chat_replies_enabled_override",
        "comments_instructions_override", "restrictions_instructions_override",
        "reply_probability_override",
        "reply_cooldown_seconds_override", "learning_prompt",
        "learning_status", "learning_completed_at",
    } <= channel_columns


def test_agents_have_channel_reply_overrides() -> None:
    assert {
        "channel_reply_probability_override",
        "channel_reply_cooldown_seconds_override",
    } <= set(AIAgent.__table__.columns.keys())


def test_channel_publication_mode_is_independent_from_chat_mode() -> None:
    account = Account(telegram_user_id=1, session_path="/tmp/account.session")
    account.reply_mode = "automatic"
    account.channel_reply_mode = "manual"
    channel = ChannelBinding(account_id=1, channel_id=100)
    assert effective_channel_reply_mode(account, channel) == "manual"
    channel.reply_mode_override = "automatic"
    assert effective_channel_reply_mode(account, channel) == "automatic"
    account.reply_mode = "manual"
    assert effective_channel_reply_mode(account, channel) == "automatic"


def test_channel_discussion_chat_replies_inherit_global_setting_and_allow_override() -> None:
    account = Account(telegram_user_id=1, session_path="/tmp/account.session")
    channel = ChannelBinding(account_id=1, channel_id=100)
    account.channel_chat_replies_enabled = True
    assert effective_channel_chat_replies_enabled(account, channel) is True
    account.channel_chat_replies_enabled = False
    assert effective_channel_chat_replies_enabled(account, channel) is False
    channel.chat_replies_enabled_override = True
    assert effective_channel_chat_replies_enabled(account, channel) is True
    channel.chat_replies_enabled_override = False
    account.channel_chat_replies_enabled = True
    assert effective_channel_chat_replies_enabled(account, channel) is False


def test_legacy_comment_threshold_columns_remain_for_database_compatibility() -> None:
    account_columns = set(Account.__table__.columns.keys())
    channel_columns = set(ChannelBinding.__table__.columns.keys())
    assert {"channel_wait_seconds", "channel_min_comments", "channel_insufficient_action"} <= account_columns
    assert {"comment_wait_seconds", "comment_wait_seconds_override", "min_comments_override", "insufficient_action_override"} <= channel_columns


def test_channel_comment_policy_uses_existing_global_values_and_channel_overrides() -> None:
    account = Account(telegram_user_id=1, session_path="/tmp/account.session")
    account.channel_wait_seconds = 300
    account.channel_min_comments = 2
    account.channel_insufficient_action = "wait"
    channel = ChannelBinding(account_id=1, channel_id=100)

    assert resolve_channel_comment_policy(account, channel) == (True, "wait", 2, 300)

    channel.comment_wait_seconds_override = 60
    channel.min_comments_override = 1
    channel.insufficient_action_override = "off_skip"
    assert resolve_channel_comment_policy(account, channel) == (False, "skip", 1, 60)



def test_account_tool_queue_models_are_persistent():
    from app.storage.models import (
        AutomationOperationEvent,
        AutomationSchedule,
        AutomationTask,
        AutomationTaskRecipient,
        ScheduledReaction,
    )

    assert {"account_id", "kind", "payload", "status", "scheduled_at"} <= set(AutomationTask.__table__.columns.keys())
    assert {"task_id", "peer_id", "status", "attempts"} <= set(AutomationTaskRecipient.__table__.columns.keys())
    assert {"account_id", "kind", "recipients_json", "interval_seconds", "next_run_at", "max_runs"} <= set(AutomationSchedule.__table__.columns.keys())
    assert {"account_id", "chat_ref", "message_id", "emoji", "run_at", "status"} <= set(ScheduledReaction.__table__.columns.keys())
    assert {"account_id", "operation", "occurred_at"} <= set(AutomationOperationEvent.__table__.columns.keys())



def test_direct_invite_consent_is_group_specific():
    from app.storage.models import AutomationInviteConsent

    columns = set(AutomationInviteConsent.__table__.columns.keys())
    assert {"account_id", "peer_id", "target_group_id", "consent_source", "revoked_at"} <= columns
    constraints = [
        constraint for constraint in AutomationInviteConsent.__table__.constraints
        if getattr(constraint, "name", None) == "uq_automation_invite_consent_target"
    ]
    assert len(constraints) == 1
    assert {column.name for column in constraints[0].columns} == {
        "account_id", "peer_id", "target_group_id"
    }


def test_accounts_have_independent_reply_runtime_overrides() -> None:
    columns = set(Account.__table__.columns.keys())
    assert {
        "mention_reply_probability_override",
        "dm_reply_probability_override",
        "proactive_reply_probability_override",
        "reply_cooldown_seconds_override",
    } <= columns
