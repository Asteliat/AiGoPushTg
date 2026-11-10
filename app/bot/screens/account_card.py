"""Account card: one screen with the state summary and the way into its submenus."""

from __future__ import annotations

from aiogram import F
from aiogram.types import CallbackQuery

from app.bot.accounts_info import (
    chat_and_channel_counts,
    connection_label,
    learning_label,
    neighbour_account_ids,
)
from app.bot.core import check_cb, edit, kb, router
from app.storage.db import SessionFactory
from app.storage.models import Account, AIAgent, Persona


@router.callback_query(F.data.startswith("acc:view:"))
async def acc_view(c: CallbackQuery, account_id: int | None = None):
    if not await check_cb(c):
        return
    aid = account_id if account_id is not None else int(c.data.split(":")[2])
    try:
        text, keyboard = await _account_card(aid)
    except LookupError:
        await c.answer("Аккаунт не найден", show_alert=True)
        return
    await edit(c, text, keyboard)


async def _account_card(account_id: int):
    """Card text and keyboard; raises LookupError when the account is gone."""
    async with SessionFactory() as db:
        account = await db.get(Account, account_id)
        if account is None:
            raise LookupError(account_id)
        agent = await db.get(AIAgent, account.ai_agent_id) if account.ai_agent_id else None
        persona = await db.get(Persona, account.persona_id) if account.persona_id else None
    chats, channels = await chat_and_channel_counts(account_id)
    previous_id, next_id = await neighbour_account_ids(account_id)

    username = f"@{account.username}" if account.username else "без username"
    ai_state = "включён" if account.ai_enabled else "выключен"
    reply_mode = "автоматически" if account.reply_mode == "automatic" else "вручную"
    text = (
        f"👤 Аккаунты → {account.display_name} · #{account.id}\n"
        f"{connection_label(account)} · {username}\n\n"
        f"🧠 ИИ: {ai_state} · ✍️ отвечает {reply_mode}\n"
        f"🤖 агент: {agent.name if agent else 'не назначен'}\n"
        f"🎭 персона: {persona.name if persona else 'не назначена'}\n"
        f"💬 чатов: {chats} · 📡 каналов: {channels}\n"
        f"📚 обучение: {learning_label(account)}"
    )
    rows = [
        [
            (("▶️ Продолжить" if account.manual_pause else "⏸ Пауза"), f"acc:pause:{account.id}"),
            (
                ("🟢 Включить" if not account.enabled else "⛔ Выключить"),
                f"acc:toggle:{account.id}",
            ),
        ],
        [("💬 Чаты", f"chat:account:{account.id}"), ("📡 Каналы", f"channel:account:{account.id}")],
        [
            ("🧠 ИИ и агент", f"acc:menu:ai:{account.id}"),
            ("📚 Обучение", f"acc:menu:learn:{account.id}"),
        ],
        [
            ("🎯 Триггеры", f"trigger:account:{account.id}"),
            ("✉️ Сообщение", f"acc:sendmessage:{account.id}"),
        ],
        [
            ("🔗 Вступить", f"chat:joinlink:{account.id}"),
            ("🪪 Профиль и прокси", f"acc:profile:{account.id}"),
        ],
    ]
    navigation = []
    if previous_id is not None:
        navigation.append(("◀️", f"acc:view:{previous_id}"))
    navigation.append(("📋 К аккаунтам", "acc:list"))
    if next_id is not None:
        navigation.append(("▶️", f"acc:view:{next_id}"))
    rows.append(navigation)
    return text, kb(rows)
