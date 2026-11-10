"""Account list and the account card."""
from __future__ import annotations

from app.bot.core import check_cb
from app.bot.core import check_msg
from app.bot.core import edit
from app.bot.core import kb
from app.bot.core import router
from app.storage.db import SessionFactory
from app.storage.models import AIAgent
from app.storage.models import Account
from app.storage.models import ChannelBinding
from app.storage.models import ChatBinding
from app.storage.models import Persona
from aiogram import F
from aiogram.types import Message
from sqlalchemy import select

@router.message(F.text=="👤 Аккаунты")
async def accounts(m: Message):
    if not await check_msg(m): return
    await accounts_screen(m)
async def accounts_screen(m):
    async with SessionFactory() as db: rows=list((await db.scalars(select(Account).order_by(Account.id))).all())
    buttons=[[(f"{'⏸' if a.manual_pause else ('🟢' if a.connection_status == 'connected' else '⚪')} #{a.id} · {a.display_name[:28]}",f"acc:view:{a.id}")] for a in rows]
    buttons += [[("🟢 В работе","acc:status:work"),("🧠 На обучении","acc:status:training")], [("💤 В простое","acc:status:idle")]]
    buttons += [[("➕ Добавить аккаунт","acc:add")],[("⬅️ Главное меню","nav:home")]]
    await m.answer("👤 Аккаунты\nВыберите аккаунт или подключите новый." if rows else "Аккаунтов пока нет. Подключите первый аккаунт кнопкой ниже.",reply_markup=kb(buttons))
@router.callback_query(F.data=="acc:list")
async def acc_list(c):
    if not await check_cb(c): return
    async with SessionFactory() as db: rows=list((await db.scalars(select(Account).order_by(Account.id))).all())
    buttons=[[(f"{'⏸' if a.manual_pause else ('🟢' if a.connection_status == 'connected' else '⚪')} #{a.id} · {a.display_name[:28]}",f"acc:view:{a.id}")] for a in rows]
    buttons += [[("🟢 В работе","acc:status:work"),("🧠 На обучении","acc:status:training")], [("💤 В простое","acc:status:idle")]]
    buttons += [[("➕ Добавить аккаунт","acc:add")],[("⬅️ Главное меню","nav:home")]]
    await edit(c,"👤 Аккаунты",kb(buttons))
@router.callback_query(F.data.startswith("acc:status:"))
async def acc_status(c):
    if not await check_cb(c): return
    status = c.data.split(":")[2]
    if status not in {"work", "training", "idle"}:
        await c.answer("Неизвестный статус", show_alert=True); return
    async with SessionFactory() as db:
        rows = list((await db.scalars(select(Account).order_by(Account.id))).all())
        bindings = list((await db.scalars(select(ChatBinding).where(ChatBinding.enabled.is_(True)))).all())
    active_chat_accounts = {b.account_id for b in bindings}
    selected = []
    for account in rows:
        if account.manual_pause:
            current = "idle"
        elif account.learning_status in {"training", "stopping"}:
            current = "training"
        elif account.enabled and account.connection_status == "connected" and account.id in active_chat_accounts:
            current = "work"
        else:
            current = "idle"
        if current == status:
            selected.append(account)
    labels = {"work": "🟢 В работе", "training": "🧠 На обучении", "idle": "💤 В простое"}
    buttons = [[(f"#{a.id} · {a.display_name[:32]}", f"acc:view:{a.id}")] for a in selected]
    buttons += [[("⬅️ Все аккаунты", "acc:list")]]
    await edit(c, f"{labels[status]} · {len(selected)}", kb(buttons))
@router.callback_query(F.data.startswith("acc:view:"))
async def acc_view(c, account_id: int | None = None):
    if not await check_cb(c): return
    aid = account_id if account_id is not None else int(c.data.split(":")[2])
    async with SessionFactory() as db:
        a=await db.get(Account,aid)
        if not a: await c.answer("Аккаунт не найден",show_alert=True); return
        agent=await db.get(AIAgent,a.ai_agent_id) if a.ai_agent_id else None
        persona=await db.get(Persona,a.persona_id) if a.persona_id else None
        active_chat_ids = list((await db.scalars(select(ChatBinding.chat_id).where(
            ChatBinding.account_id == aid, ChatBinding.enabled.is_(True)
        ))).all())
        discussion_ids = set((await db.scalars(select(ChannelBinding.discussion_group_id).where(
            ChannelBinding.account_id == aid, ChannelBinding.discussion_group_id.is_not(None)
        ))).all())
        channel_ids = set((await db.scalars(select(ChannelBinding.channel_id).where(
            ChannelBinding.account_id == aid
        ))).all())
        excluded_chat_ids = discussion_ids | channel_ids
        chats = sum(1 for chat_id in active_chat_ids if chat_id not in excluded_chat_ids)
    learning = a.learning_status in {"training", "stopping"}
    if learning and a.learning_ends_at:
        deadline = a.learning_ends_at.strftime("%d.%m.%Y %H:%M UTC")
        learning_text = f"обучение на паузе · {a.learning_limit} сообщений · до {deadline}" if a.manual_pause else (f"обучение · {a.learning_limit} сообщений · до {deadline}" if a.learning_status == "training" else "завершается")
    else:
        if a.learning_prompt.strip() and a.learning_completed_at:
            completed = a.learning_completed_at.strftime("%d.%m.%Y %H:%M UTC")
            learning_text = f"контекст сохранён · последнее обучение {completed}"
        else:
            learning_text = "контекст сохранён вручную" if a.learning_prompt.strip() else "не запускалось"
    conn_label = {"connected": "подключён", "connecting": "подключается", "paused": "пауза", "disconnected": "отключён", "error": "ошибка подключения", "unauthorized": "сессия не авторизована"}.get(a.connection_status, a.connection_status)
    txt=f"👤 {a.display_name} (@{a.username or 'нет username'})\nID: #{aid}\nАккаунт: {'включён' if a.enabled else 'выключен'}\nСоединение: {conn_label}\nРабота: {'⏸ на паузе' if a.manual_pause else ('активна' if a.enabled else 'отключена')}\nИИ для аккаунта: {'включён' if a.ai_enabled else 'выключен'}\nРежим ответов: {'автоматический' if a.reply_mode=='automatic' else 'ручной'}\nАгент: {agent.name if agent else 'не назначен'}\nПерсона: {persona.name if persona else 'не назначена'}\nАктивных чатов: {chats}\nОбучение: {learning_text}\nПрокси: {'настроен' if a.proxy_config_encrypted else 'нет'}\nИндивидуальные правила: {'заданы' if a.persona_style_override.strip() else 'нет'}"
    buttons=[
        [(("▶️ Возобновить" if a.manual_pause else "⏸ Пауза"),f"acc:pause:{aid}"),
         (("▶️ Включить" if not a.enabled else "⏸ Отключить"),f"acc:toggle:{aid}")],
        [("ИИ 🟢" if a.ai_enabled else "ИИ 🔴",f"acc:ai:{aid}"),
         ("Автомат" if a.reply_mode=="automatic" else "Ручной",f"acc:mode:{aid}")],
        [("🧠 ИИ Агент",f"acc:agents:{aid}"),("🎭 Персона",f"acc:personas:{aid}")],
        [("⚙️ Прокси",f"acc:proxysettings:{aid}"),("✏️ Правила",f"acc:personaoverride:{aid}")],
        [("💡 Примеры",f"example:account:{aid}"),("💬 Чаты",f"chat:account:{aid}")],
        [("⏱ Пауза ответов",f"acc:runtime:menu:{aid}")],
        [("✉️ Отправить сообщение",f"acc:sendmessage:{aid}")],
        [("🔗 Вступить в группу/канал",f"chat:joinlink:{aid}"),
         ((("⏳ Завершение" if a.learning_status == "stopping" else "⏹ Остановить") if learning else "🧠 Автообучение"),(f"learn:stop:{aid}" if learning else f"acc:learn:{aid}"))],
        [("📡 Каналы",f"channel:account:{aid}")],
        [("🎯 Триггеры",f"trigger:account:{aid}")],
        [("⬅️ К аккаунтам","acc:list"),("🏠 Главное меню","nav:home")]]
    await edit(c,txt,kb(buttons))
@router.callback_query(F.data.startswith("acc:pause:"))
async def acc_pause(c):
    if not await check_cb(c): return
    aid=int(c.data.split(":")[2])
    async with SessionFactory() as db:
        a=await db.get(Account,aid)
        if not a: await c.answer("Аккаунт не найден",show_alert=True); return
        if not a.enabled:
            await c.answer("Сначала подключите аккаунт.", show_alert=True); return
        a.manual_pause=not a.manual_pause
        a.connection_status = "paused" if a.manual_pause else "connecting"
        await db.commit()
    await acc_view(c)
@router.callback_query(F.data.startswith("acc:toggle:"))
async def acc_toggle(c):
    if not await check_cb(c): return
    aid=int(c.data.split(":")[2])
    async with SessionFactory() as db:
        a=await db.get(Account,aid)
        if not a: await c.answer("Аккаунт не найден",show_alert=True); return
        if a.enabled and a.learning_status in {"training", "stopping"}:
            await c.answer("Сначала завершите автообучение, затем отключайте аккаунт.", show_alert=True); return
        a.enabled=not a.enabled
        a.manual_pause = False
        a.connection_status = "connecting" if a.enabled else "disconnected"
        await db.commit()
    await acc_view(c)
@router.callback_query(F.data.startswith("acc:ai:"))
async def acc_ai(c):
    if not await check_cb(c): return
    aid=int(c.data.split(":")[2])
    async with SessionFactory() as db:
        a=await db.get(Account,aid)
        if not a: await c.answer("Аккаунт не найден",show_alert=True); return
        a.ai_enabled=not a.ai_enabled; await db.commit()
    await acc_view(c)
@router.callback_query(F.data.startswith("acc:mode:"))
async def acc_mode(c):
    if not await check_cb(c): return
    aid=int(c.data.split(":")[2])
    async with SessionFactory() as db:
        a=await db.get(Account,aid)
        if not a: await c.answer("Аккаунт не найден",show_alert=True); return
        if a.learning_status in {"training", "stopping"} and a.reply_mode == "manual":
            await c.answer("Нельзя включить автоматические ответы во время обучения.", show_alert=True); return
        a.reply_mode="manual" if a.reply_mode=="automatic" else "automatic"; await db.commit()
    await acc_view(c)
@router.callback_query(F.data.startswith("acc:agents:"))
async def acc_agents(c):
    if not await check_cb(c): return
    aid=int(c.data.split(":")[2])
    async with SessionFactory() as db: agents=list((await db.scalars(select(AIAgent).order_by(AIAgent.id))).all())
    buttons=[[(f"{'⭐ ' if a.is_default else ''}{a.name} · {'вкл' if a.enabled else 'выкл'}",f"acc:setagent:x:{aid}:{a.id}")] for a in agents]
    buttons += [[("Без агента",f"acc:setagent:x:{aid}:0")],[("⬅️ Назад",f"acc:view:{aid}")]]
    await edit(c,"Выберите ИИ-агента:",kb(buttons))
@router.callback_query(F.data.startswith("acc:setagent:"))
async def acc_set_agent(c):
    if not await check_cb(c): return
    _,_,_,aid,agent= c.data.split(":")
    async with SessionFactory() as db:
        a=await db.get(Account,int(aid)); ag=await db.get(AIAgent,int(agent)) if int(agent) else None
        if not a: await c.answer("Аккаунт не найден",show_alert=True); return
        a.ai_agent_id=ag.id if ag else None; await db.commit()
    await acc_view(c, int(aid))
@router.callback_query(F.data.startswith("acc:personas:"))
async def acc_personas(c):
    if not await check_cb(c): return
    aid=int(c.data.split(":")[2])
    async with SessionFactory() as db: ps=list((await db.scalars(select(Persona).order_by(Persona.id))).all())
    buttons=[[(p.name,f"acc:setpersona:x:{aid}:{p.id}")] for p in ps]
    buttons += [[("Без персоны",f"acc:setpersona:x:{aid}:0")],[("⬅️ Назад",f"acc:view:{aid}")]]
    await edit(c,"Выберите персону:",kb(buttons))
@router.callback_query(F.data.startswith("acc:setpersona:"))
async def acc_set_persona(c):
    if not await check_cb(c): return
    _,_,_,aid,pid=c.data.split(":")
    async with SessionFactory() as db:
        a=await db.get(Account,int(aid))
        if not a: await c.answer("Аккаунт не найден",show_alert=True); return
        a.persona_id=int(pid) or None; await db.commit()
    await acc_view(c, int(aid))
