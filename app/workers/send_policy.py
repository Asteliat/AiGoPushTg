"""Final gate before an automatic Telegram reply is sent.

The pipeline re-checks the account right before the side effect: settings may have
changed while the AI was generating, and an outdated draft must never be sent.
"""
from __future__ import annotations

from sqlalchemy import select

from app.services.ai_responses import global_ai_enabled
from app.storage.db import SessionFactory
from app.storage.models import Account, AIAgent, ChatBinding

_TRAINING_STATUSES = frozenset({"training", "stopping"})


async def automatic_send_allowed(
    account_id: int,
    chat_id: int,
    expected_agent_id: int,
    *,
    allow_unbound: bool = False,
) -> bool:
    """True only if this account may still send automatically to this chat now."""
    async with SessionFactory() as db:
        account = await db.get(Account, account_id)
        if (
            not account
            or not account.enabled
            or account.manual_pause
            or not account.ai_enabled
            or account.reply_mode != "automatic"
            or account.learning_status in _TRAINING_STATUSES
            or account.ai_agent_id != expected_agent_id
            or not await global_ai_enabled(db)
        ):
            return False
        binding = None
        if not allow_unbound:
            binding = await db.scalar(
                select(ChatBinding).where(
                    ChatBinding.account_id == account_id,
                    ChatBinding.chat_id == chat_id,
                    ChatBinding.enabled.is_(True),
                )
            )
        agent = await db.get(AIAgent, account.ai_agent_id) if account.ai_agent_id else None
        return bool(agent and agent.enabled and (allow_unbound or binding))
