"""Entry point, main menu and cancel handling."""
from __future__ import annotations

from app.bot.core import LOGIN_TASKS
from app.bot.core import MAIN
from app.bot.core import check_cb
from app.bot.core import check_msg
from app.bot.core import edit
from app.bot.core import home_text
from app.bot.core import kb
from app.bot.core import router
from app.bot.core import show_home
from app.bot.navigation import restore_previous_screen
from app.bot.screens.login import cleanup_phone_login
from app.bot.screens.login import cleanup_qr_login
from aiogram import F
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery
from aiogram.types import Message

@router.message(CommandStart())
async def start(m: Message, state: FSMContext):
    if await check_msg(m):
        await state.clear()
        await cleanup_phone_login(m.from_user.id)
        await cleanup_qr_login(m.from_user.id)
        await show_home(m)
@router.message(F.text.in_({"❌ Отмена","Отмена"}))
async def cancel(m: Message, state: FSMContext):
    if await check_msg(m):
        data = await state.get_data()
        previous = data.get("return_callback")
        await state.clear()
        task = LOGIN_TASKS.pop(m.from_user.id, None)
        if task and not task.done(): task.cancel()
        await cleanup_phone_login(m.from_user.id)
        await cleanup_qr_login(m.from_user.id)
        if previous:
            await m.answer("Действие отменено. Вернитесь в предыдущий раздел:", reply_markup=kb([[("↩️ Назад", previous)]]))
        else:
            await m.answer("Действие отменено.", reply_markup=MAIN)
@router.callback_query(F.data=="nav:home")
async def home_cb(c: CallbackQuery, state: FSMContext):
    if not await check_cb(c): return
    await state.clear()
    await edit(c, await home_text(), MAIN)
@router.callback_query(F.data=="nav:cancel")
async def cancel_cb(c: CallbackQuery, state: FSMContext):
    if not await check_cb(c): return
    data = await state.get_data()
    previous = data.get("return_callback")
    await state.clear()
    task = LOGIN_TASKS.pop(c.from_user.id, None)
    if task and not task.done(): task.cancel()
    await cleanup_phone_login(c.from_user.id)
    await cleanup_qr_login(c.from_user.id)
    await restore_previous_screen(c, previous, state)
