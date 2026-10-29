from datetime import datetime, timezone

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
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
    title: Mapped[str] = mapped_column(String(255), default="")
    enabled: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false", nullable=False)
    comment_wait_seconds: Mapped[int] = mapped_column(Integer, default=300, server_default="300", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ChannelAnalysis(Base):
    __tablename__ = "channel_analyses"
    __table_args__ = (UniqueConstraint("account_id", "channel_id", "post_id", name="uq_channel_post_analysis"),)

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
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AppSetting(Base):
    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String(80), primary_key=True)
    value: Mapped[str] = mapped_column(Text, default="")
