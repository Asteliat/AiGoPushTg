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
        # Serialize schema upgrades even when a trusted CLI tool starts concurrently.
        await connection.exec_driver_sql("SELECT pg_advisory_xact_lock(741928461)")
        await connection.run_sync(Base.metadata.create_all)
        await connection.exec_driver_sql("CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY, applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW())")
        # Safe additive migration for installations created by earlier versions.
        statements = [
            "ALTER TABLE accounts ADD COLUMN IF NOT EXISTS reply_mode VARCHAR(20) NOT NULL DEFAULT 'manual'",
            "ALTER TABLE accounts ADD COLUMN IF NOT EXISTS channel_reply_mode VARCHAR(20) NOT NULL DEFAULT 'manual'",
            "ALTER TABLE accounts ADD COLUMN IF NOT EXISTS channel_chat_replies_enabled BOOLEAN NOT NULL DEFAULT TRUE",
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
            "ALTER TABLE channel_bindings ADD COLUMN IF NOT EXISTS reply_mode_override VARCHAR(20) NULL",
            "ALTER TABLE channel_bindings ADD COLUMN IF NOT EXISTS chat_replies_enabled_override BOOLEAN NULL",
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
            "ALTER TABLE channel_bindings ADD COLUMN IF NOT EXISTS learning_started_at TIMESTAMPTZ NULL",
            "CREATE INDEX IF NOT EXISTS ix_chat_bindings_account_enabled ON chat_bindings (account_id, enabled)",
            "CREATE INDEX IF NOT EXISTS ix_channel_bindings_account_enabled ON channel_bindings (account_id, enabled)",
            "CREATE INDEX IF NOT EXISTS ix_channel_bindings_learning ON channel_bindings (account_id, learning_status)",
            "CREATE INDEX IF NOT EXISTS ix_channel_post_states_due ON channel_post_states (status, next_retry_at)",
            "CREATE INDEX IF NOT EXISTS ix_channel_analyses_account_created ON channel_analyses (account_id, created_at DESC)",
            "CREATE INDEX IF NOT EXISTS ix_notification_outbox_due ON notification_outbox (status, next_retry_at, created_at)",
            "CREATE TABLE IF NOT EXISTS automation_consents (id SERIAL PRIMARY KEY, account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE, peer_id BIGINT NOT NULL, peer_label VARCHAR(255) NOT NULL DEFAULT '', scope VARCHAR(24) NOT NULL DEFAULT 'broadcast', consent_source VARCHAR(255) NOT NULL DEFAULT '', granted_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), revoked_at TIMESTAMPTZ NULL, CONSTRAINT uq_automation_consent_peer_scope UNIQUE (account_id, peer_id, scope))",
            "CREATE INDEX IF NOT EXISTS ix_automation_consents_account_id ON automation_consents (account_id)",
            "CREATE INDEX IF NOT EXISTS ix_automation_consents_peer_id ON automation_consents (peer_id)",
            "CREATE TABLE IF NOT EXISTS automation_tasks (id SERIAL PRIMARY KEY, account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE, kind VARCHAR(24) NOT NULL, payload TEXT NOT NULL DEFAULT '{}', status VARCHAR(20) NOT NULL DEFAULT 'queued', scheduled_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), started_at TIMESTAMPTZ NULL, completed_at TIMESTAMPTZ NULL, last_error TEXT NOT NULL DEFAULT '', created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW())",
            "CREATE INDEX IF NOT EXISTS ix_automation_tasks_account_id ON automation_tasks (account_id)",
            "CREATE INDEX IF NOT EXISTS ix_automation_tasks_status ON automation_tasks (status)",
            "CREATE INDEX IF NOT EXISTS ix_automation_tasks_due ON automation_tasks (status, scheduled_at)",
            "CREATE TABLE IF NOT EXISTS automation_task_recipients (id SERIAL PRIMARY KEY, task_id INTEGER NOT NULL REFERENCES automation_tasks(id) ON DELETE CASCADE, peer_id BIGINT NOT NULL, peer_label VARCHAR(255) NOT NULL DEFAULT '', status VARCHAR(20) NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0, last_error TEXT NOT NULL DEFAULT '', delivered_at TIMESTAMPTZ NULL, CONSTRAINT uq_automation_task_recipient UNIQUE (task_id, peer_id))",
            "CREATE INDEX IF NOT EXISTS ix_automation_recipients_task_id ON automation_task_recipients (task_id)",
            "CREATE INDEX IF NOT EXISTS ix_automation_recipients_due ON automation_task_recipients (task_id, status)",
            "CREATE TABLE IF NOT EXISTS automation_schedules (id SERIAL PRIMARY KEY, account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE, kind VARCHAR(24) NOT NULL, payload TEXT NOT NULL DEFAULT '{}', recipients_json TEXT NOT NULL, interval_seconds INTEGER NOT NULL, next_run_at TIMESTAMPTZ NOT NULL, ends_at TIMESTAMPTZ NULL, max_runs INTEGER NOT NULL DEFAULT 30, runs_count INTEGER NOT NULL DEFAULT 0, last_task_id INTEGER NULL REFERENCES automation_tasks(id) ON DELETE SET NULL, status VARCHAR(20) NOT NULL DEFAULT 'active', last_error TEXT NOT NULL DEFAULT '', created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW())",
            "CREATE INDEX IF NOT EXISTS ix_automation_schedules_account_id ON automation_schedules (account_id)",
            "CREATE INDEX IF NOT EXISTS ix_automation_schedules_due ON automation_schedules (status, next_run_at)",
            "CREATE TABLE IF NOT EXISTS scheduled_reactions (id SERIAL PRIMARY KEY, account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE, chat_ref VARCHAR(255) NOT NULL, message_id BIGINT NOT NULL, emoji VARCHAR(32) NOT NULL, run_at TIMESTAMPTZ NOT NULL, status VARCHAR(20) NOT NULL DEFAULT 'queued', started_at TIMESTAMPTZ NULL, last_error TEXT NOT NULL DEFAULT '', created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW())",
            "CREATE INDEX IF NOT EXISTS ix_scheduled_reactions_account_id ON scheduled_reactions (account_id)",
            "CREATE INDEX IF NOT EXISTS ix_scheduled_reactions_due ON scheduled_reactions (status, run_at)",
            "ALTER TABLE automation_consents ADD COLUMN IF NOT EXISTS peer_ref VARCHAR(255) NOT NULL DEFAULT ''",
            "ALTER TABLE automation_task_recipients ADD COLUMN IF NOT EXISTS peer_ref VARCHAR(255) NOT NULL DEFAULT ''",
            "ALTER TABLE ai_agents ADD COLUMN IF NOT EXISTS channel_reply_probability_override INTEGER NULL",
            "ALTER TABLE ai_agents ADD COLUMN IF NOT EXISTS channel_reply_cooldown_seconds_override INTEGER NULL",
            "ALTER TABLE channel_analyses ADD COLUMN IF NOT EXISTS should_reply BOOLEAN NOT NULL DEFAULT FALSE",
            "ALTER TABLE channel_analyses ALTER COLUMN should_reply SET DEFAULT TRUE",
            "ALTER TABLE channel_analyses ADD COLUMN IF NOT EXISTS publication_status VARCHAR(20) NOT NULL DEFAULT 'legacy'",
            "ALTER TABLE channel_analyses ALTER COLUMN publication_status SET DEFAULT 'pending'",
            "ALTER TABLE channel_analyses ADD COLUMN IF NOT EXISTS discussion_group_id BIGINT NULL",
            "ALTER TABLE channel_analyses ADD COLUMN IF NOT EXISTS published_comment_id BIGINT NULL",
            "ALTER TABLE channel_analyses ADD COLUMN IF NOT EXISTS publication_error TEXT NOT NULL DEFAULT ''",
            "CREATE INDEX IF NOT EXISTS ix_channel_analysis_published_comment ON channel_analyses (account_id, discussion_group_id, published_comment_id)",
        ]
        for statement in statements:
            await connection.exec_driver_sql(statement)
        # Preserve the previous account-wide mode for existing channel automations once;
        # future chat mode changes must not silently alter channel publication behavior.
        await connection.exec_driver_sql(
            "WITH first_channel_mode AS ("
            "INSERT INTO schema_migrations(version) VALUES (2) "
            "ON CONFLICT (version) DO NOTHING RETURNING version"
            ") UPDATE accounts SET channel_reply_mode = reply_mode "
            "WHERE EXISTS (SELECT 1 FROM first_channel_mode)"
        )
        # One-time compatibility migration: preserve the historical default for bindings
        # that existed before discovery was introduced, without re-enabling them on deploy.
        # One-time compatibility migration without a procedural SQL block.
        # The INSERT claims version 1 only once; UPDATE runs only for that first claim.
        await connection.exec_driver_sql(
            "WITH first_run AS ("
            "INSERT INTO schema_migrations(version) VALUES (1) "
            "ON CONFLICT (version) DO NOTHING RETURNING version"
            ") UPDATE chat_bindings SET enabled = TRUE, auto_discovered = TRUE "
            "WHERE auto_discovered = FALSE AND EXISTS (SELECT 1 FROM first_run)"
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
