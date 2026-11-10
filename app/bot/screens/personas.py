"""Personas with shared style instructions."""

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


@router.message(F.text == "🎭 Персоны")
async def personas(m):
    if not await check_msg(m):
        return
    await persona_screen(m)


async def persona_screen(m):
    async with SessionFactory() as db:
        ps = list((await db.scalars(select(Persona).order_by(Persona.id))).all())
    await m.answer(
        "🎭 Персоны\nВыберите персону." if ps else "Персон пока нет. Добавьте первую.",
        reply_markup=kb(
            [[(p.name, f"persona:view:{p.id}")] for p in ps]
            + [[("➕ Добавить персону", "persona:add")], [("⬅️ Главное меню", "nav:home")]]
        ),
    )


@router.callback_query(F.data == "persona:menu")
async def persona_menu(c):
    if not await check_cb(c):
        return
    async with SessionFactory() as db:
        ps = list((await db.scalars(select(Persona).order_by(Persona.id))).all())
    await edit(
        c,
        "🎭 Персоны",
        kb(
            [[(p.name, f"persona:view:{p.id}")] for p in ps]
            + [[("➕ Добавить персону", "persona:add")], [("⬅️ Главное меню", "nav:home")]]
        ),
    )


@router.callback_query(F.data == "persona:add")
async def persona_add(c, state):
    if not await check_cb(c):
        return
    await state.clear()
    await state.update_data(
        ui_chat_id=c.message.chat.id,
        ui_message_id=c.message.message_id,
        return_callback=cancel_return_for(c.data),
    )
    await state.set_state(Flow.persona_name)
    await edit(c, "Введите название персоны.", CANCEL)


@router.message(Flow.persona_name)
async def persona_name(m, state):
    if not await check_msg(m) or not await private(m):
        return
    name = (m.text or "").strip()
    if not name or len(name) > 80:
        await prompt_message(m, state, "Название должно содержать от 1 до 80 символов.")
        return
    async with SessionFactory() as db:
        if await db.scalar(select(Persona.id).where(Persona.name == name)):
            await prompt_message(m, state, "Такое имя уже есть.")
            return
    await delete_msg(m)
    await state.update_data(persona_name=name)
    await state.set_state(Flow.persona_style)
    await prompt_message(m, state, "Введите инструкции стиля общения.")


@router.message(Flow.persona_style)
async def persona_style(m, state):
    if not await check_msg(m) or not await private(m):
        return
    style = (m.text or "").strip()
    if not style or len(style) > 5000:
        await prompt_message(m, state, "Текст должен содержать от 1 до 5000 символов.")
        return
    d = await state.get_data()
    async with SessionFactory() as db:
        if d.get("persona_edit_id"):
            p = await db.get(Persona, d["persona_edit_id"])
            if not p:
                await finish_prompt(m, state, "Персона не найдена.")
                return
            p.style_instructions = style
            label = (
                "Общая персона обновлена. Изменения применены ко всем использующим её аккаунтам."
            )
        else:
            p = Persona(
                name=d["persona_name"], description="Создана через бот", style_instructions=style
            )
            db.add(p)
            label = "Персона сохранена."
        await db.commit()
        await db.refresh(p)
    await finish_prompt(
        m,
        state,
        label,
        kb([[("🎭 К персонам", "persona:menu")], [("🏠 Главное меню", "nav:home")]]),
    )


@router.callback_query(F.data.startswith("persona:view:"))
async def persona_view(c, persona_id: int | None = None):
    if not await check_cb(c):
        return
    pid = persona_id if persona_id is not None else int(c.data.split(":")[2])
    async with SessionFactory() as db:
        p = await db.get(Persona, pid)
        n = len((await db.scalars(select(Example).where(Example.persona_id == pid))).all())
    if not p:
        await c.answer("Персона не найдена", show_alert=True)
        return
    await edit(
        c,
        f"🎭 {p.name}\n\n{p.style_instructions[:2200]}\n\nПримеров: {n}",
        kb(
            [
                [("✏️ Изменить стиль", f"persona:edit:{pid}")],
                [("🧩 Добавить пример", f"example:add:{pid}")],
                [("🗑 Удалить персону", f"persona:delete:{pid}")],
                [("⬅️ К персонам", "persona:menu"), ("🏠 Главное меню", "nav:home")],
            ]
        ),
    )


@router.callback_query(F.data.startswith("persona:edit:"))
async def persona_edit_start(c, state: FSMContext):
    if not await check_cb(c):
        return
    pid = int(c.data.split(":")[2])
    async with SessionFactory() as db:
        persona = await db.get(Persona, pid)
    if not persona:
        await c.answer("Персона не найдена", show_alert=True)
        return
    await state.clear()
    await state.update_data(
        persona_edit_id=pid,
        ui_chat_id=c.message.chat.id,
        ui_message_id=c.message.message_id,
        return_callback=cancel_return_for(c.data),
    )
    await state.set_state(Flow.persona_style)
    await edit(
        c,
        f"Изменение общей персоны «{persona.name}». Изменения сразу повлияют на все аккаунты, которые её используют. Отправьте новый стиль (до 5000 символов).",
        CANCEL,
    )


@router.callback_query(F.data.startswith("persona:delete:"))
async def persona_delete(c):
    if not await check_cb(c):
        return
    pid = int(c.data.split(":")[2])
    async with SessionFactory() as db:
        p = await db.get(Persona, pid)
        if p:
            for a in (await db.scalars(select(Account).where(Account.persona_id == pid))).all():
                a.persona_id = None
            await db.delete(p)
            await db.commit()
    await persona_menu(c)
