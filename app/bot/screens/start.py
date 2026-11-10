"""Entry point, main menu and cancel handling."""

from __future__ import annotations

from aiogram import F
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from app.bot.core import (
    LOGIN_TASKS,
    MAIN,
    check_cb,
    check_msg,
    edit,
    home_text,
    kb,
    router,
    show_home,
)
from app.bot.navigation import restore_previous_screen


def _login_cleanups():
    """Imported lazily so screen modules register handlers in the documented order."""
    from app.bot.screens.login import cleanup_phone_login, cleanup_qr_login

    return cleanup_phone_login, cleanup_qr_login


async def _abort_login(telegram_id: int) -> None:
    task = LOGIN_TASKS.pop(telegram_id, None)
    if task and not task.done():
        task.cancel()
    cleanup_phone_login, cleanup_qr_login = _login_cleanups()
    await cleanup_phone_login(telegram_id)
    await cleanup_qr_login(telegram_id)


@router.message(CommandStart())
async def start(m: Message, state: FSMContext):
    if not await check_msg(m):
        return
    await state.clear()
    await _abort_login(m.from_user.id)
    await show_home(m)


@router.message(F.text.in_({"❌ Отмена", "Отмена"}))
async def cancel(m: Message, state: FSMContext):
    if not await check_msg(m):
        return
    data = await state.get_data()
    previous = data.get("return_callback")
    await state.clear()
    await _abort_login(m.from_user.id)
    if previous:
        await m.answer(
            "Действие отменено. Вернитесь в предыдущий раздел:",
            reply_markup=kb([[("↩️ Назад", previous)]]),
        )
        return
    await m.answer("Действие отменено.", reply_markup=MAIN)


@router.callback_query(F.data == "nav:home")
async def home_cb(c: CallbackQuery, state: FSMContext):
    if not await check_cb(c):
        return
    await state.clear()
    await edit(c, await home_text(), MAIN)


@router.callback_query(F.data == "nav:cancel")
async def cancel_cb(c: CallbackQuery, state: FSMContext):
    if not await check_cb(c):
        return
    data = await state.get_data()
    previous = data.get("return_callback")
    await _abort_login(c.from_user.id)
    # Drop the input step but keep the selection stored for the screen that comes back:
    # the account profile editor needs its account list to restore itself.
    await state.set_state(None)
    if previous:
        await restore_previous_screen(c, previous, state)
        return
    await state.clear()
    await edit(c, "Действие отменено.", MAIN)
