"""Example incoming messages and preferred replies."""
from __future__ import annotations

from app.bot.core import CANCEL
from app.bot.core import Flow
from app.bot.core import check_cb
from app.bot.core import check_msg
from app.bot.core import delete_msg
from app.bot.core import edit
from app.bot.core import finish_prompt
from app.bot.core import kb
from app.bot.core import private
from app.bot.core import prompt_message
from app.bot.core import router
from app.bot.navigation import cancel_return_for
from app.storage.db import SessionFactory
from app.storage.models import Account
from app.storage.models import Example
from app.storage.models import Persona
from aiogram import F
from aiogram.fsm.context import FSMContext
from sqlalchemy import select

@router.message(F.text=="🧩 Примеры")
async def examples(m):
    if not await check_msg(m): return
    await example_screen(m)
async def example_screen(m):
    async with SessionFactory() as db:
        es=list((await db.scalars(select(Example).where(Example.account_id.is_(None)).order_by(Example.id.desc()).limit(15))).all())
        ps={p.id:p.name for p in (await db.scalars(select(Persona))).all()}
    buttons=[[(f"#{e.id} · {ps.get(e.persona_id,'Общие')} · {e.incoming_text[:24]}",f"example:view:{e.id}")] for e in es]
    buttons += [[("➕ Добавить общий пример","example:add:0")],[("⬅️ Главное меню","nav:home")]]
    await m.answer("🧩 Примеры · выберите запись." if es else "Примеров пока нет.",reply_markup=kb(buttons))
@router.callback_query(F.data=="example:menu")
async def example_menu(c):
    if not await check_cb(c): return
    async with SessionFactory() as db:
        es=list((await db.scalars(select(Example).where(Example.account_id.is_(None)).order_by(Example.id.desc()).limit(15))).all()); ps={p.id:p.name for p in (await db.scalars(select(Persona))).all()}
    buttons=[[(f"#{e.id} · {ps.get(e.persona_id,'Общие')} · {e.incoming_text[:24]}",f"example:view:{e.id}")] for e in es]
    buttons += [[("➕ Добавить общий пример","example:add:0")],[("⬅️ Главное меню","nav:home")]]
    await edit(c,"🧩 Примеры",kb(buttons))
@router.callback_query(F.data.startswith("example:account:"))
async def account_examples(c, account_id: int | None = None):
    if not await check_cb(c): return
    aid = account_id if account_id is not None else int(c.data.split(":")[2])
    async with SessionFactory() as db:
        account = await db.get(Account, aid)
        examples = list((await db.scalars(select(Example).where(Example.account_id == aid).order_by(Example.id.desc()).limit(15))).all())
        common_count = len((await db.scalars(select(Example).where(Example.account_id.is_(None), Example.consent_confirmed.is_(True)))).all())
    if not account: await c.answer("Аккаунт не найден", show_alert=True); return
    buttons = [[(f"#{e.id} · {e.incoming_text[:30]}", f"example:view:{e.id}")] for e in examples]
    buttons += [[("➕ Добавить пример аккаунта", f"example:addacc:{aid}")],
                [("💡 Общая библиотека примеров", "example:menu")],
                [("⬅️ К аккаунту", f"acc:view:{aid}"), ("🏠 Главное меню", "nav:home")]]
    await edit(c, f"Примеры аккаунта #{aid}\nИндивидуальных: {len(examples)} · Общих доступно: {common_count}", kb(buttons))
@router.callback_query(F.data.startswith("example:addacc:"))
async def account_example_add(c, state: FSMContext):
    if not await check_cb(c): return
    aid = int(c.data.split(":")[2])
    async with SessionFactory() as db:
        if not await db.get(Account, aid): await c.answer("Аккаунт не найден", show_alert=True); return
    await state.clear()
    await state.update_data(example_pid=0, example_account_id=aid, ui_chat_id=c.message.chat.id, ui_message_id=c.message.message_id, return_callback=cancel_return_for(c.data))
    await state.set_state(Flow.example_incoming)
    await edit(c, "Добавление примера для конкретного аккаунта. Отправьте входящее сообщение без личных данных.", CANCEL)
@router.callback_query(F.data.startswith("example:add:"))
async def example_add(c,state):
    if not await check_cb(c): return
    pid=int(c.data.split(":")[2])
    async with SessionFactory() as db:
        if pid and not await db.get(Persona,pid): await c.answer("Персона не найдена",show_alert=True); return
    await state.clear(); await state.update_data(example_pid=pid, example_account_id=None, ui_chat_id=c.message.chat.id, ui_message_id=c.message.message_id, return_callback=cancel_return_for(c.data)); await state.set_state(Flow.example_incoming)
    await edit(c, "Введите входящее сообщение для примера. Удалите личные данные и сохраняйте только разрешённый текст.", CANCEL)
@router.message(Flow.example_incoming)
async def example_incoming(m,state):
    if not await check_msg(m) or not await private(m): return
    v=(m.text or "").strip()
    if not v or len(v)>3500: await prompt_message(m,state,"Введите текст до 3500 символов."); return
    await delete_msg(m)
    await state.update_data(example_incoming=v); await state.set_state(Flow.example_reply); await prompt_message(m,state,"Введите предпочтительный ответ.")
@router.message(Flow.example_reply)
async def example_reply(m,state):
    if not await check_msg(m) or not await private(m): return
    v=(m.text or "").strip()
    if not v or len(v)>3500: await prompt_message(m,state,"Введите ответ до 3500 символов."); return
    d=await state.get_data()
    async with SessionFactory() as db:
        e=Example(account_id=d.get("example_account_id"),persona_id=d["example_pid"] or None,title="Добавлено через бот",incoming_text=d["example_incoming"],preferred_reply=v,consent_confirmed=True)
        db.add(e)
        await db.commit()
    if d.get("example_account_id"):
        aid = d["example_account_id"]
        await finish_prompt(m,state,"Пример аккаунта сохранён.",kb([[("💡 Примеры аккаунта",f"example:account:{aid}")],[("🏠 Главное меню","nav:home")]]))
    else:
        await finish_prompt(m,state,"Общий пример сохранён.",kb([[("💡 К примерам","example:menu")],[("🏠 Главное меню","nav:home")]]))
@router.callback_query(F.data.startswith("example:view:"))
async def example_view(c, example_id: int | None = None):
    if not await check_cb(c): return
    eid=example_id if example_id is not None else int(c.data.split(":")[2])
    async with SessionFactory() as db: e=await db.get(Example,eid); p=await db.get(Persona,e.persona_id) if e and e.persona_id else None
    if not e: await c.answer("Пример не найден",show_alert=True); return
    back = f"example:account:{e.account_id}" if e.account_id else "example:menu"
    scope = f"Аккаунт #{e.account_id}" if e.account_id else (p.name if p else "Общий")
    await edit(c,f"Пример #{eid} · {scope}\n\nВходящее:\n{e.incoming_text[:1500]}\n\nОтвет:\n{e.preferred_reply[:1500]}",kb([[("🗑 Удалить",f"example:delete:{eid}")],[("⬅️ Назад",back),("🏠 Главное меню","nav:home")]]))
@router.callback_query(F.data.startswith("example:delete:"))
async def example_delete(c):
    if not await check_cb(c): return
    eid=int(c.data.split(":")[2])
    async with SessionFactory() as db:
        e=await db.get(Example,eid)
        account_id = e.account_id if e else None
        if e: await db.delete(e); await db.commit()
    if account_id:
        await edit(c, "Пример удалён.", kb([[("💡 К примерам аккаунта", f"example:account:{account_id}")], [("🏠 Главное меню", "nav:home")]]))
    else:
        await example_menu(c)
