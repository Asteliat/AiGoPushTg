"""Account state wording shared by the account list and the account card."""

from __future__ import annotations

from sqlalchemy import select

from app.storage.db import SessionFactory
from app.storage.models import Account, ChannelBinding, ChatBinding


_CONNECTION_LABELS = {
    "connected": "🟢 подключён",
    "connecting": "🔄 подключается",
    "paused": "⏸ на паузе",
    "disconnected": "⚪ отключён",
    "error": "🔴 ошибка подключения",
    "unauthorized": "🔑 сессия не авторизована",
}
GROUP_ICONS = {"work": "🟢 В работе", "training": "🧠 На обучении", "idle": "💤 В простое"}


def connection_label(account: Account) -> str:
    return _CONNECTION_LABELS.get(account.connection_status, account.connection_status)


def account_icon(account: Account) -> str:
    if account.manual_pause:
        return "⏸"
    if account.connection_status == "connected":
        return "🟢"
    return "⚪"


def learning_label(account: Account) -> str:
    """One short phrase, never a paragraph."""
    if account.learning_status in {"training", "stopping"} and account.learning_ends_at:
        deadline = account.learning_ends_at.strftime("%d.%m %H:%M UTC")
        if account.learning_status == "stopping":
            return f"⏳ завершается ({deadline})"
        prefix = "⏸ пауза до" if account.manual_pause else "⏳ идёт до"
        return f"{prefix} {deadline} · {account.learning_limit} сообщений"
    if account.learning_prompt.strip() and account.learning_completed_at:
        completed = account.learning_completed_at.strftime("%d.%m %H:%M UTC")
        return f"✅ контекст от {completed}"
    if account.learning_prompt.strip():
        return "✅ контекст задан вручную"
    return "— не запускалось"


async def chat_and_channel_counts(account_id: int) -> tuple[int, int]:
    """Chats that really are chats: channel posts and discussion groups are excluded."""
    async with SessionFactory() as db:
        active_chat_ids = list(
            (
                await db.scalars(
                    select(ChatBinding.chat_id).where(
                        ChatBinding.account_id == account_id,
                        ChatBinding.enabled.is_(True),
                    )
                )
            ).all()
        )
        discussion_ids = set(
            (
                await db.scalars(
                    select(ChannelBinding.discussion_group_id).where(
                        ChannelBinding.account_id == account_id,
                        ChannelBinding.discussion_group_id.is_not(None),
                    )
                )
            ).all()
        )
        channel_ids = set(
            (
                await db.scalars(
                    select(ChannelBinding.channel_id).where(
                        ChannelBinding.account_id == account_id,
                    )
                )
            ).all()
        )
    excluded = discussion_ids | channel_ids
    chats = sum(1 for chat_id in active_chat_ids if chat_id not in excluded)
    return chats, len(channel_ids)


async def neighbour_account_ids(account_id: int) -> tuple[int | None, int | None]:
    """Neighbouring accounts for the prev/next switch, wrapping around."""
    async with SessionFactory() as db:
        ids = list((await db.scalars(select(Account.id).order_by(Account.id))).all())
    if len(ids) < 2 or account_id not in ids:
        return None, None
    index = ids.index(account_id)
    return ids[index - 1], ids[(index + 1) % len(ids)]
