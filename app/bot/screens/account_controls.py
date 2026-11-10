"""Quick account switches: pause, enable, AI, reply mode, agent and persona."""

from __future__ import annotations

from aiogram import F
from aiogram.types import CallbackQuery
from sqlalchemy import select

from app.bot.core import check_cb, edit, kb, router
from app.bot.screens.account_card import acc_view
from app.bot.screens.account_menus import acc_ai_menu
from app.storage.db import SessionFactory
from app.storage.models import Account, AIAgent, Persona


@router.callback_query(F.data.startswith("acc:pause:"))
async def acc_pause(c: CallbackQuery):
    if not await check_cb(c):
        return
    aid = int(c.data.split(":")[2])
    async with SessionFactory() as db:
        account = await db.get(Account, aid)
        if account is None:
            await c.answer("Аккаунт не найден", show_alert=True)
            return
        if not account.enabled:
            await c.answer("Сначала включите аккаунт.", show_alert=True)
            return
        account.manual_pause = not account.manual_pause
        account.connection_status = "paused" if account.manual_pause else "connecting"
        await db.commit()
    await acc_view(c)


@router.callback_query(F.data.startswith("acc:toggle:"))
async def acc_toggle(c: CallbackQuery):
    if not await check_cb(c):
        return
    aid = int(c.data.split(":")[2])
    async with SessionFactory() as db:
        account = await db.get(Account, aid)
        if account is None:
            await c.answer("Аккаунт не найден", show_alert=True)
            return
        if account.enabled and account.learning_status in {"training", "stopping"}:
            await c.answer(
                "Сначала завершите автообучение, затем отключайте аккаунт.", show_alert=True
            )
            return
        account.enabled = not account.enabled
        account.manual_pause = False
        account.connection_status = "connecting" if account.enabled else "disconnected"
        await db.commit()
    await acc_view(c)


@router.callback_query(F.data.startswith("acc:ai:"))
async def acc_ai(c: CallbackQuery):
    if not await check_cb(c):
        return
    aid = int(c.data.split(":")[2])
    async with SessionFactory() as db:
        account = await db.get(Account, aid)
        if account is None:
            await c.answer("Аккаунт не найден", show_alert=True)
            return
        account.ai_enabled = not account.ai_enabled
        await db.commit()
    await acc_ai_menu(c, aid)


@router.callback_query(F.data.startswith("acc:mode:"))
async def acc_mode(c: CallbackQuery):
    if not await check_cb(c):
        return
    aid = int(c.data.split(":")[2])
    async with SessionFactory() as db:
        account = await db.get(Account, aid)
        if account is None:
            await c.answer("Аккаунт не найден", show_alert=True)
            return
        if account.learning_status in {"training", "stopping"} and account.reply_mode == "manual":
            await c.answer(
                "Нельзя включить автоматические ответы во время обучения.", show_alert=True
            )
            return
        account.reply_mode = "manual" if account.reply_mode == "automatic" else "automatic"
        await db.commit()
    await acc_ai_menu(c, aid)


@router.callback_query(F.data.startswith("acc:agents:"))
async def acc_agents(c: CallbackQuery):
    if not await check_cb(c):
        return
    aid = int(c.data.split(":")[2])
    async with SessionFactory() as db:
        agents = list((await db.scalars(select(AIAgent).order_by(AIAgent.id))).all())
    buttons = [
        [
            (
                f"{'⭐ ' if agent.is_default else ''}{agent.name} · {'вкл' if agent.enabled else 'выкл'}",
                f"acc:setagent:x:{aid}:{agent.id}",
            )
        ]
        for agent in agents
    ]
    buttons += [[("Без агента", f"acc:setagent:x:{aid}:0")], [("⬅️ Назад", f"acc:menu:ai:{aid}")]]
    await edit(c, "🤖 Агент для аккаунта\n\nВыберите ИИ-агента:", kb(buttons))


@router.callback_query(F.data.startswith("acc:setagent:"))
async def acc_set_agent(c: CallbackQuery):
    if not await check_cb(c):
        return
    _, _, _, aid, agent_id = c.data.split(":")
    async with SessionFactory() as db:
        account = await db.get(Account, int(aid))
        if account is None:
            await c.answer("Аккаунт не найден", show_alert=True)
            return
        agent = await db.get(AIAgent, int(agent_id)) if int(agent_id) else None
        account.ai_agent_id = agent.id if agent else None
        await db.commit()
    await acc_ai_menu(c, int(aid))


@router.callback_query(F.data.startswith("acc:personas:"))
async def acc_personas(c: CallbackQuery):
    if not await check_cb(c):
        return
    aid = int(c.data.split(":")[2])
    async with SessionFactory() as db:
        personas = list((await db.scalars(select(Persona).order_by(Persona.id))).all())
    buttons = [[(persona.name, f"acc:setpersona:x:{aid}:{persona.id}")] for persona in personas]
    buttons += [[("Без персоны", f"acc:setpersona:x:{aid}:0")], [("⬅️ Назад", f"acc:menu:ai:{aid}")]]
    await edit(c, "🎭 Персона для аккаунта\n\nВыберите персону:", kb(buttons))


@router.callback_query(F.data.startswith("acc:setpersona:"))
async def acc_set_persona(c: CallbackQuery):
    if not await check_cb(c):
        return
    _, _, _, aid, persona_id = c.data.split(":")
    async with SessionFactory() as db:
        account = await db.get(Account, int(aid))
        if account is None:
            await c.answer("Аккаунт не найден", show_alert=True)
            return
        account.persona_id = int(persona_id) or None
        await db.commit()
    await acc_ai_menu(c, int(aid))
