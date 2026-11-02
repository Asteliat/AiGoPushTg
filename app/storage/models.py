from datetime import datetime, timezone

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.storage.db import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Account(Base):
    __tablename__ = "accounts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    telegram_user_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    username: Mapped[str | None] = mapped_column(String(64), nullable=True)
    display_name: Mapped[str] = mapped_column(String(160), default="")
    session_path: Mapped[str] = mapped_column(String(512))
    proxy_config_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true", nullable=False)
    manual_pause: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false", nullable=False)
    connection_status: Mapped[str] = mapped_column(String(20), default="disconnected", server_default="disconnected", nullable=False)
    persona_style_override: Mapped[str] = mapped_column(Text, default="", server_default="", nullable=False)
    reply_mode: Mapped[str] = mapped_column(String(20), default="manual", server_default="manual", nullable=False)
    channel_reply_mode: Mapped[str] = mapped_column(String(20), default="manual", server_default="manual", nullable=False)
    channel_chat_replies_enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true", nullable=False)
    ai_enabled: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false", nullable=False)
    ai_agent_id: Mapped[int | None] = mapped_column(ForeignKey("ai_agents.id", ondelete="SET NULL"), nullable=True, index=True)
    persona_id: Mapped[int | None] = mapped_column(ForeignKey("personas.id", ondelete="SET NULL"), nullable=True)
    learning_status: Mapped[str] = mapped_column(String(20), default="idle", server_default="idle", nullable=False)
    learning_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    learning_ends_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    learning_completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    learning_limit: Mapped[int] = mapped_column(Integer, default=500, server_default="500", nullable=False)
    learning_prompt: Mapped[str] = mapped_column(Text, default="", server_default="", nullable=False)
    ai_identity_disclosed: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false", nullable=False)
    mention_reply_probability_override: Mapped[int | None] = mapped_column(Integer, nullable=True)
    dm_reply_probability_override: Mapped[int | None] = mapped_column(Integer, nullable=True)
    proactive_reply_probability_override: Mapped[int | None] = mapped_column(Integer, nullable=True)
    reply_cooldown_seconds_override: Mapped[int | None] = mapped_column(Integer, nullable=True)
    channel_post_instructions: Mapped[str] = mapped_column(Text, default="", server_default="", nullable=False)
    channel_comments_instructions: Mapped[str] = mapped_column(Text, default="", server_default="", nullable=False)
    channel_restrictions_instructions: Mapped[str] = mapped_column(Text, default="", server_default="", nullable=False)
    # Deprecated compatibility columns: audience-comment count no longer gates channel posts.
    channel_wait_seconds: Mapped[int] = mapped_column(Integer, default=300, server_default="300", nullable=False)
    channel_min_comments: Mapped[int] = mapped_column(Integer, default=2, server_default="2", nullable=False)
    channel_insufficient_action: Mapped[str] = mapped_column(String(10), default="wait", server_default="wait", nullable=False)
    channel_reply_probability: Mapped[int] = mapped_column(Integer, default=100, server_default="100", nullable=False)
    channel_reply_cooldown_seconds: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Persona(Base):
    __tablename__ = "personas"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(80), unique=True)
    description: Mapped[str] = mapped_column(Text, default="")
    style_instructions: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Example(Base):
    __tablename__ = "examples"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_id: Mapped[int | None] = mapped_column(ForeignKey("accounts.id", ondelete="CASCADE"), nullable=True, index=True)
    persona_id: Mapped[int | None] = mapped_column(ForeignKey("personas.id", ondelete="SET NULL"), nullable=True)
    title: Mapped[str] = mapped_column(String(160), default="")
    incoming_text: Mapped[str] = mapped_column(Text)
    preferred_reply: Mapped[str] = mapped_column(Text)
    consent_confirmed: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AuditEvent(Base):
    __tablename__ = "audit_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    actor_telegram_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    action: Mapped[str] = mapped_column(String(80), index=True)
    object_type: Mapped[str] = mapped_column(String(80), default="")
    object_id: Mapped[str] = mapped_column(String(80), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ChatBinding(Base):
    __tablename__ = "chat_bindings"
    __table_args__ = (UniqueConstraint("account_id", "chat_id", name="uq_account_chat_binding"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id", ondelete="CASCADE"), index=True)
    auto_discovered: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false", nullable=False)
    chat_id: Mapped[int] = mapped_column(BigInteger, index=True)
    title: Mapped[str] = mapped_column(String(255), default="")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ChannelBinding(Base):
    __tablename__ = "channel_bindings"
    __table_args__ = (UniqueConstraint("account_id", "channel_id", name="uq_account_channel_binding"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id", ondelete="CASCADE"), index=True)
    channel_id: Mapped[int] = mapped_column(BigInteger, index=True)
    discussion_group_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    title: Mapped[str] = mapped_column(String(255), default="")
    enabled: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false", nullable=False)
    # Deprecated compatibility fields: retained for existing databases, not read by channel processing.
    comment_wait_seconds: Mapped[int] = mapped_column(Integer, default=300, server_default="300", nullable=False)
    reply_enabled: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false", nullable=False)
    reply_mode_override: Mapped[str | None] = mapped_column(String(20), nullable=True)
    chat_replies_enabled_override: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    post_instructions_override: Mapped[str | None] = mapped_column(Text, nullable=True)
    comments_instructions_override: Mapped[str | None] = mapped_column(Text, nullable=True)
    restrictions_instructions_override: Mapped[str | None] = mapped_column(Text, nullable=True)
    comment_wait_seconds_override: Mapped[int | None] = mapped_column(Integer, nullable=True)
    min_comments_override: Mapped[int | None] = mapped_column(Integer, nullable=True)
    insufficient_action_override: Mapped[str | None] = mapped_column(String(10), nullable=True)
    reply_probability_override: Mapped[int | None] = mapped_column(Integer, nullable=True)
    reply_cooldown_seconds_override: Mapped[int | None] = mapped_column(Integer, nullable=True)
    learning_prompt: Mapped[str] = mapped_column(Text, default="", server_default="", nullable=False)
    learning_status: Mapped[str] = mapped_column(String(20), default="idle", server_default="idle", nullable=False)
    learning_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    learning_completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ChannelAnalysis(Base):
    __tablename__ = "channel_analyses"
    __table_args__ = (
        UniqueConstraint("account_id", "channel_id", "post_id", name="uq_channel_post_analysis"),
        Index("ix_channel_analysis_published_comment", "account_id", "discussion_group_id", "published_comment_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id", ondelete="CASCADE"), index=True)
    channel_id: Mapped[int] = mapped_column(BigInteger, index=True)
    channel_title: Mapped[str] = mapped_column(String(255), default="")
    post_id: Mapped[int] = mapped_column(BigInteger)
    post_text: Mapped[str] = mapped_column(Text)
    comments_summary: Mapped[str] = mapped_column(Text, default="")
    on_topic: Mapped[bool] = mapped_column(Boolean, default=False)
    suggested_response: Mapped[str] = mapped_column(Text, default="")
    comments_count: Mapped[int] = mapped_column(Integer, default=0)
    should_reply: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true", nullable=False)
    publication_status: Mapped[str] = mapped_column(String(20), default="pending", server_default="pending", nullable=False)
    discussion_group_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    published_comment_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    publication_error: Mapped[str] = mapped_column(Text, default="", server_default="", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ChannelPostState(Base):
    """Durable processing state for one channel post."""
    __tablename__ = "channel_post_states"
    __table_args__ = (UniqueConstraint("account_id", "channel_id", "post_id", name="uq_channel_post_state"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id", ondelete="CASCADE"), index=True)
    channel_id: Mapped[int] = mapped_column(BigInteger, index=True)
    post_id: Mapped[int] = mapped_column(BigInteger)
    channel_title: Mapped[str] = mapped_column(String(255), default="")
    post_text: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(20), default="queued", server_default="queued", nullable=False, index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)
    next_retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    last_error: Mapped[str] = mapped_column(Text, default="", server_default="", nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)



class TriggerRule(Base):
    """Account-specific or global chat trigger configuration."""
    __tablename__ = "trigger_rules"
    __table_args__ = (Index("ix_trigger_rules_scope_enabled", "account_id", "enabled"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_id: Mapped[int | None] = mapped_column(
        ForeignKey("accounts.id", ondelete="CASCADE"), nullable=True, index=True
    )
    apply_mode: Mapped[str] = mapped_column(
        String(20), default="empty_only", server_default="empty_only", nullable=False
    )
    terms: Mapped[str] = mapped_column(Text, nullable=False)
    response_instructions: Mapped[str] = mapped_column(Text, default="", server_default="", nullable=False)
    cooldown_seconds: Mapped[int] = mapped_column(Integer, default=900, server_default="900", nullable=False)
    message_cooldown_seconds: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class TriggerEvent(Base):
    """Durable deduplication record: a message is processed at most once per account."""
    __tablename__ = "trigger_events"
    __table_args__ = (
        UniqueConstraint("account_id", "chat_id", "message_id", name="uq_trigger_event_message"),
        Index("ix_trigger_events_account_created", "account_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id", ondelete="CASCADE"), index=True)
    trigger_id: Mapped[int | None] = mapped_column(
        ForeignKey("trigger_rules.id", ondelete="SET NULL"), nullable=True, index=True
    )
    chat_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    message_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    sender_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    matched_term: Mapped[str] = mapped_column(String(255), default="", server_default="", nullable=False)
    status: Mapped[str] = mapped_column(String(24), default="processing", server_default="processing", nullable=False)
    error: Mapped[str] = mapped_column(Text, default="", server_default="", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class AIAgent(Base):
    __tablename__ = "ai_agents"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    base_url: Mapped[str] = mapped_column(String(500), default="https://api.openai.com/v1")
    api_key: Mapped[str] = mapped_column(Text, default="")
    model: Mapped[str] = mapped_column(String(160), default="gpt-4o-mini")
    timeout_seconds: Mapped[int] = mapped_column(Integer, default=45)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true", nullable=False)
    is_default: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false", nullable=False)
    mention_reply_probability_override: Mapped[int | None] = mapped_column(Integer, nullable=True)
    dm_reply_probability_override: Mapped[int | None] = mapped_column(Integer, nullable=True)
    proactive_reply_probability_override: Mapped[int | None] = mapped_column(Integer, nullable=True)
    reply_cooldown_seconds_override: Mapped[int | None] = mapped_column(Integer, nullable=True)
    channel_reply_probability_override: Mapped[int | None] = mapped_column(Integer, nullable=True)
    channel_reply_cooldown_seconds_override: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AppSetting(Base):
    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String(80), primary_key=True)
    value: Mapped[str] = mapped_column(Text, default="")


class OutboxNotification(Base):
    """Durable Telegram notification queue; delivery is retried until acknowledged."""
    __tablename__ = "notification_outbox"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    category: Mapped[str] = mapped_column(String(40), default="main", nullable=False)
    chat_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    thread_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="pending", server_default="pending", nullable=False, index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)
    next_retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    last_error: Mapped[str] = mapped_column(Text, default="", server_default="", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class AutomationConsent(Base):
    """Explicit opt-in for one account to contact one Telegram peer for a scope."""
    __tablename__ = "automation_consents"
    __table_args__ = (UniqueConstraint("account_id", "peer_id", "scope", name="uq_automation_consent_peer_scope"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id", ondelete="CASCADE"), index=True)
    peer_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    peer_ref: Mapped[str] = mapped_column(String(255), default="", server_default="", nullable=False)
    peer_label: Mapped[str] = mapped_column(String(255), default="", server_default="", nullable=False)
    scope: Mapped[str] = mapped_column(String(24), default="broadcast", server_default="broadcast", nullable=False)
    consent_source: Mapped[str] = mapped_column(String(255), default="", server_default="", nullable=False)
    granted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class AutomationTask(Base):
    """Durable scheduled campaign/task with resumable state."""
    __tablename__ = "automation_tasks"
    __table_args__ = (Index("ix_automation_tasks_due", "status", "scheduled_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(24), nullable=False)
    payload: Mapped[str] = mapped_column(Text, default="{}", server_default="{}", nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="queued", server_default="queued", nullable=False, index=True)
    scheduled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str] = mapped_column(Text, default="", server_default="", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)


class AutomationTaskRecipient(Base):
    """Per-recipient delivery record; retries never duplicate acknowledged deliveries."""
    __tablename__ = "automation_task_recipients"
    __table_args__ = (
        UniqueConstraint("task_id", "peer_id", name="uq_automation_task_recipient"),
        Index("ix_automation_recipients_due", "task_id", "status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    task_id: Mapped[int] = mapped_column(ForeignKey("automation_tasks.id", ondelete="CASCADE"), index=True)
    peer_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    peer_ref: Mapped[str] = mapped_column(String(255), default="", server_default="", nullable=False)
    peer_label: Mapped[str] = mapped_column(String(255), default="", server_default="", nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="pending", server_default="pending", nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)
    last_error: Mapped[str] = mapped_column(Text, default="", server_default="", nullable=False)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)



class AutomationSchedule(Base):
    """Recurring opt-in campaign definition; each occurrence is materialized transactionally."""
    __tablename__ = "automation_schedules"
    __table_args__ = (
        Index("ix_automation_schedules_due", "status", "next_run_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(24), nullable=False)
    payload: Mapped[str] = mapped_column(Text, default="{}", server_default="{}", nullable=False)
    recipients_json: Mapped[str] = mapped_column(Text, nullable=False)
    interval_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    next_run_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    ends_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    max_runs: Mapped[int] = mapped_column(Integer, default=30, server_default="30", nullable=False)
    runs_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)
    last_task_id: Mapped[int | None] = mapped_column(ForeignKey("automation_tasks.id", ondelete="SET NULL"), nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="active", server_default="active", nullable=False, index=True)
    last_error: Mapped[str] = mapped_column(Text, default="", server_default="", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)



class ScheduledReaction(Base):
    """One explicitly selected reaction scheduled for one message."""
    __tablename__ = "scheduled_reactions"
    __table_args__ = (
        Index("ix_scheduled_reactions_due", "status", "run_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id", ondelete="CASCADE"), index=True)
    chat_ref: Mapped[str] = mapped_column(String(255), nullable=False)
    message_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    emoji: Mapped[str] = mapped_column(String(32), nullable=False)
    run_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(20), default="queued", server_default="queued", nullable=False, index=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str] = mapped_column(Text, default="", server_default="", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)



class AutomationOperationEvent(Base):
    """Persistent per-account operation ledger used for local rate policies."""
    __tablename__ = "automation_operation_events"
    __table_args__ = (
        Index("ix_automation_operation_events_account_kind_time", "account_id", "operation", "occurred_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id", ondelete="CASCADE"), index=True)
    operation: Mapped[str] = mapped_column(String(24), nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False, index=True)



class AutomationInviteConsent(Base):
    """Explicit consent to be directly added to one specific supergroup."""
    __tablename__ = "automation_invite_consents"
    __table_args__ = (
        UniqueConstraint("account_id", "peer_id", "target_group_id", name="uq_automation_invite_consent_target"),
        Index("ix_automation_invite_consents_lookup", "account_id", "target_group_id", "peer_id"),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id", ondelete="CASCADE"), index=True)
    peer_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    peer_ref: Mapped[str] = mapped_column(String(255), default="", server_default="", nullable=False)
    peer_label: Mapped[str] = mapped_column(String(255), default="", server_default="", nullable=False)
    target_group_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    target_group_ref: Mapped[str] = mapped_column(String(255), default="", server_default="", nullable=False)
    target_group_label: Mapped[str] = mapped_column(String(255), default="", server_default="", nullable=False)
    consent_source: Mapped[str] = mapped_column(String(255), default="", server_default="", nullable=False)
    granted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
