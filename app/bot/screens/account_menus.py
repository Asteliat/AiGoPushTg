"""Account submenus: AI settings, learning, profile and proxy."""

from __future__ import annotations

from aiogram import F
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery

from app.bot.accounts_info import learning_label
from app.bot.core import check_cb, edit, kb, router
from app.storage.db import SessionFactory
from app.storage.models import Account, AIAgent, Persona


@router.callback_query(F.data.startswith("acc:menu:ai:"))
async def acc_ai_menu(c: CallbackQuery, account_id: int | None = None):
    if not await check_cb(c):
        return
    aid = account_id if account_id is not None else int(c.data.rsplit(":", 1)[1])
    async with SessionFactory() as db:
        account = await db.get(Account, aid)
        if account is None:
            await c.answer("Аккаунт не найден", show_alert=True)
            return
        agent = await db.get(AIAgent, account.ai_agent_id) if account.ai_agent_id else None
        persona = await db.get(Persona, account.persona_id) if account.persona_id else None
        cooldown = account.reply_cooldown_seconds_override
    text = (
        f"🧠 ИИ и агент · {account.display_name}\n\n"
        f"ИИ: {'включён' if account.ai_enabled else 'выключен'} · "
        f"ответы: {'автоматически' if account.reply_mode == 'automatic' else 'вручную'}\n"
        f"Агент: {agent.name if agent else 'не назначен'}\n"
        f"Персона: {persona.name if persona else 'не назначена'}\n"
        f"Пауза ответов: {'—' if cooldown is None else f'{cooldown} с'}\n"
        f"Правила стиля: {'заданы' if account.persona_style_override.strip() else 'нет'}"
    )
    await edit(
        c,
        text,
        kb(
            [
                [
                    (
                        ("🧠 ИИ включён" if account.ai_enabled else "🧠 ИИ выключен"),
                        f"acc:ai:{aid}",
                    ),
                    (
                        ("✍️ Автоматически" if account.reply_mode == "automatic" else "✍️ Вручную"),
                        f"acc:mode:{aid}",
                    ),
                ],
                [("🤖 Агент", f"acc:agents:{aid}"), ("🎭 Персона", f"acc:personas:{aid}")],
                [
                    ("✏️ Правила стиля", f"acc:personaoverride:{aid}"),
                    ("⏱ Пауза ответов", f"acc:runtime:menu:{aid}"),
                ],
                [("⬅️ К аккаунту", f"acc:view:{aid}")],
            ]
        ),
    )


@router.callback_query(F.data.startswith("acc:menu:learn:"))
async def acc_learn_menu(c: CallbackQuery, account_id: int | None = None):
    if not await check_cb(c):
        return
    aid = account_id if account_id is not None else int(c.data.rsplit(":", 1)[1])
    async with SessionFactory() as db:
        account = await db.get(Account, aid)
    if account is None:
        await c.answer("Аккаунт не найден", show_alert=True)
        return
    training = account.learning_status in {"training", "stopping"}
    await edit(
        c,
        (
            f"📚 Обучение · {account.display_name}\n\n"
            f"Статус: {learning_label(account)}\n"
            f"Лимит: {account.learning_limit} сообщений\n"
            f"Контекст: {'задан' if account.learning_prompt.strip() else 'пусто'}"
        ),
        kb(
            [
                [
                    (
                        "⏹ Остановить" if training else "🧠 Автообучение",
                        f"learn:stop:{aid}" if training else f"acc:learn:{aid}",
                    )
                ],
                [
                    ("📝 Контекст обучения", f"acc:learnprompt:{aid}"),
                    ("💡 Примеры", f"example:account:{aid}"),
                ],
                [("⬅️ К аккаунту", f"acc:view:{aid}")],
            ]
        ),
    )


@router.callback_query(F.data.startswith("acc:profile:"))
async def acc_profile(c: CallbackQuery):
    if not await check_cb(c):
        return
    aid = int(c.data.rsplit(":", 1)[1])
    async with SessionFactory() as db:
        account = await db.get(Account, aid)
    if account is None:
        await c.answer("Аккаунт не найден", show_alert=True)
        return
    await edit(
        c,
        (
            f"🪪 Профиль и прокси · {account.display_name}\n\n"
            f"Прокси: {'настроен' if account.proxy_config_encrypted else 'нет'}\n"
            f"Имя, BIO и username меняются через профиль Telegram."
        ),
        kb(
            [
                [("🪪 Изменить профиль", f"acc:editprofile:{aid}")],
                [("🌐 Прокси", f"acc:proxysettings:{aid}")],
                [("⬅️ К аккаунту", f"acc:view:{aid}")],
            ]
        ),
    )


@router.callback_query(F.data.startswith("acc:editprofile:"))
async def acc_edit_profile(c: CallbackQuery, state: FSMContext):
    """Reuse the multi-account profile editor with this single account selected."""
    if not await check_cb(c):
        return
    from app.bot.screens.tools import render_account_settings_menu

    aid = int(c.data.rsplit(":", 1)[1])
    await state.update_data(management_account_ids=[aid])
    await render_account_settings_menu(c, state)
