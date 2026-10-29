from collections.abc import AsyncIterator
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase
from app.settings import get_settings

engine = create_async_engine(get_settings().database_url, pool_pre_ping=True)
SessionFactory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

class Base(DeclarativeBase):
    pass

async def get_session() -> AsyncIterator[AsyncSession]:
    async with SessionFactory() as session:
        yield session

async def init_db() -> None:
    from app.storage import models  # noqa: F401
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
        # Drafts were explicitly removed from the product; drop the legacy table if present.
        await connection.exec_driver_sql("DROP TABLE IF EXISTS drafts")
        # Safe additive migration for installations created by earlier versions.
        statements = [
            "ALTER TABLE accounts ADD COLUMN IF NOT EXISTS reply_mode VARCHAR(20) NOT NULL DEFAULT 'manual'",
            "ALTER TABLE accounts ADD COLUMN IF NOT EXISTS ai_enabled BOOLEAN NOT NULL DEFAULT FALSE",
            "ALTER TABLE accounts ADD COLUMN IF NOT EXISTS ai_agent_id INTEGER NULL REFERENCES ai_agents(id) ON DELETE SET NULL",
            "ALTER TABLE accounts ADD COLUMN IF NOT EXISTS persona_id INTEGER NULL REFERENCES personas(id) ON DELETE SET NULL",
            "ALTER TABLE accounts ADD COLUMN IF NOT EXISTS learning_status VARCHAR(20) NOT NULL DEFAULT 'idle'",
            "ALTER TABLE accounts ADD COLUMN IF NOT EXISTS learning_started_at TIMESTAMPTZ NULL",
            "ALTER TABLE accounts ADD COLUMN IF NOT EXISTS learning_ends_at TIMESTAMPTZ NULL",
            "ALTER TABLE accounts ADD COLUMN IF NOT EXISTS learning_completed_at TIMESTAMPTZ NULL",
            "ALTER TABLE accounts ADD COLUMN IF NOT EXISTS learning_limit INTEGER NOT NULL DEFAULT 500",
            "ALTER TABLE accounts ADD COLUMN IF NOT EXISTS learning_prompt TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE accounts ADD COLUMN IF NOT EXISTS ai_identity_disclosed BOOLEAN NOT NULL DEFAULT FALSE",
            "ALTER TABLE ai_agents ADD COLUMN IF NOT EXISTS mention_reply_probability_override INTEGER NULL",
            "ALTER TABLE ai_agents ADD COLUMN IF NOT EXISTS dm_reply_probability_override INTEGER NULL",
            "ALTER TABLE ai_agents ADD COLUMN IF NOT EXISTS proactive_reply_probability_override INTEGER NULL",
            "ALTER TABLE ai_agents ADD COLUMN IF NOT EXISTS reply_cooldown_seconds_override INTEGER NULL",
            "ALTER TABLE accounts ADD COLUMN IF NOT EXISTS proxy_config_encrypted TEXT NULL",
            "ALTER TABLE accounts ADD COLUMN IF NOT EXISTS manual_pause BOOLEAN NOT NULL DEFAULT FALSE",
            "ALTER TABLE accounts ADD COLUMN IF NOT EXISTS connection_status VARCHAR(20) NOT NULL DEFAULT 'disconnected'",
            "ALTER TABLE accounts ADD COLUMN IF NOT EXISTS persona_style_override TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE examples ADD COLUMN IF NOT EXISTS account_id INTEGER NULL REFERENCES accounts(id) ON DELETE CASCADE",
            "ALTER TABLE chat_bindings ADD COLUMN IF NOT EXISTS auto_discovered BOOLEAN NOT NULL DEFAULT FALSE",
            "ALTER TABLE channel_bindings ADD COLUMN IF NOT EXISTS comment_wait_seconds INTEGER NOT NULL DEFAULT 300",
            "ALTER TABLE accounts ADD COLUMN IF NOT EXISTS channel_post_instructions TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE accounts ADD COLUMN IF NOT EXISTS channel_comments_instructions TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE accounts ADD COLUMN IF NOT EXISTS channel_restrictions_instructions TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE accounts ADD COLUMN IF NOT EXISTS channel_wait_seconds INTEGER NOT NULL DEFAULT 300",
            "ALTER TABLE accounts ADD COLUMN IF NOT EXISTS channel_min_comments INTEGER NOT NULL DEFAULT 2",
            "ALTER TABLE accounts ADD COLUMN IF NOT EXISTS channel_insufficient_action VARCHAR(10) NOT NULL DEFAULT 'wait'",
            "ALTER TABLE accounts ADD COLUMN IF NOT EXISTS channel_reply_probability INTEGER NOT NULL DEFAULT 100",
            "ALTER TABLE accounts ADD COLUMN IF NOT EXISTS channel_reply_cooldown_seconds INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE channel_bindings ADD COLUMN IF NOT EXISTS discussion_group_id BIGINT NULL",
            "ALTER TABLE channel_bindings ADD COLUMN IF NOT EXISTS reply_enabled BOOLEAN NOT NULL DEFAULT FALSE",
            "ALTER TABLE channel_bindings ADD COLUMN IF NOT EXISTS post_instructions_override TEXT NULL",
            "ALTER TABLE channel_bindings ADD COLUMN IF NOT EXISTS comments_instructions_override TEXT NULL",
            "ALTER TABLE channel_bindings ADD COLUMN IF NOT EXISTS restrictions_instructions_override TEXT NULL",
            "ALTER TABLE channel_bindings ADD COLUMN IF NOT EXISTS comment_wait_seconds_override INTEGER NULL",
            "ALTER TABLE channel_bindings ADD COLUMN IF NOT EXISTS min_comments_override INTEGER NULL",
            "ALTER TABLE channel_bindings ADD COLUMN IF NOT EXISTS insufficient_action_override VARCHAR(10) NULL",
            "ALTER TABLE channel_bindings ADD COLUMN IF NOT EXISTS reply_probability_override INTEGER NULL",
            "ALTER TABLE channel_bindings ADD COLUMN IF NOT EXISTS reply_cooldown_seconds_override INTEGER NULL",
            "ALTER TABLE channel_bindings ADD COLUMN IF NOT EXISTS learning_prompt TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE channel_bindings ADD COLUMN IF NOT EXISTS learning_status VARCHAR(20) NOT NULL DEFAULT 'idle'",
            "ALTER TABLE channel_bindings ADD COLUMN IF NOT EXISTS learning_completed_at TIMESTAMPTZ NULL",
            "ALTER TABLE ai_agents ADD COLUMN IF NOT EXISTS channel_reply_probability_override INTEGER NULL",
            "ALTER TABLE ai_agents ADD COLUMN IF NOT EXISTS channel_reply_cooldown_seconds_override INTEGER NULL",
        ]
        for statement in statements:
            await connection.exec_driver_sql(statement)
        # Existing bindings were the only available chat list before auto-discovery.
        # Enable them once, then mark them discovered so future manual disables persist.
        await connection.exec_driver_sql(
            "UPDATE chat_bindings SET enabled = TRUE, auto_discovered = TRUE WHERE auto_discovered = FALSE"
        )
        await connection.exec_driver_sql(
            "INSERT INTO app_settings (key, value) VALUES ('ai_global_enabled', 'false') ON CONFLICT (key) DO NOTHING"
        )
        await connection.exec_driver_sql(
            "INSERT INTO app_settings (key, value) VALUES ('mention_reply_probability', '100'), ('dm_reply_probability', '100'), ('proactive_reply_probability', '0'), ('reply_cooldown_seconds', '0') ON CONFLICT (key) DO NOTHING"
        )
        # Preserve previously customized channel wait values as per-channel overrides.
        await connection.exec_driver_sql(
            "UPDATE channel_bindings SET comment_wait_seconds_override = comment_wait_seconds "
            "WHERE comment_wait_seconds <> 300 AND comment_wait_seconds_override IS NULL"
        )
