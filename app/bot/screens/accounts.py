"""Account list and status filters."""

from __future__ import annotations

from aiogram import F
from aiogram.types import CallbackQuery, Message
from sqlalchemy import select

from app.bot.accounts_info import GROUP_ICONS, account_icon
from app.bot.core import check_cb, check_msg, edit, kb, router
from app.storage.db import SessionFactory
from app.storage.models import Account, ChatBinding


@router.message(F.text == "👤 Аккаунты")
async def accounts(m: Message):
    if not await check_msg(m):
        return
    await accounts_screen(m)


async def accounts_screen(m: Message):
    async with SessionFactory() as db:
        rows = list((await db.scalars(select(Account).order_by(Account.id))).all())
    await m.answer(
        "👤 Аккаунты\nВыберите аккаунт или подключите новый."
        if rows
        else "👤 Аккаунты\nПока ни одного. Подключите первый кнопкой ниже.",
        reply_markup=_accounts_keyboard(rows),
    )


def _accounts_keyboard(rows: list[Account]):
    buttons = [
        [
            (
                f"{account_icon(account)} #{account.id} · {account.display_name[:28]}",
                f"acc:view:{account.id}",
            )
        ]
        for account in rows
    ]
    buttons += [[("🟢 В работе", "acc:status:work"), ("🧠 На обучении", "acc:status:training")]]
    buttons += [[("💤 В простое", "acc:status:idle"), ("➕ Добавить", "acc:add")]]
    buttons += [[("⬅️ Главное меню", "nav:home")]]
    return kb(buttons)


@router.callback_query(F.data == "acc:list")
async def acc_list(c: CallbackQuery):
    if not await check_cb(c):
        return
    async with SessionFactory() as db:
        rows = list((await db.scalars(select(Account).order_by(Account.id))).all())
    header = f"👤 Аккаунты · {len(rows)}"
    await edit(c, header if rows else f"{header}\nПока ни одного.", _accounts_keyboard(rows))


@router.callback_query(F.data.startswith("acc:status:"))
async def acc_status(c: CallbackQuery):
    if not await check_cb(c):
        return
    status = c.data.split(":")[2]
    if status not in GROUP_ICONS:
        await c.answer("Неизвестная группа", show_alert=True)
        return
    async with SessionFactory() as db:
        rows = list((await db.scalars(select(Account).order_by(Account.id))).all())
        bindings = list(
            (await db.scalars(select(ChatBinding).where(ChatBinding.enabled.is_(True)))).all()
        )
    chat_accounts = {binding.account_id for binding in bindings}
    selected = [account for account in rows if _group_of(account, chat_accounts) == status]
    buttons = [
        [(f"#{account.id} · {account.display_name[:32]}", f"acc:view:{account.id}")]
        for account in selected
    ]
    buttons += [[("⬅️ Все аккаунты", "acc:list")]]
    await edit(c, f"{GROUP_ICONS[status]} · {len(selected)}", kb(buttons))


def _group_of(account: Account, chat_accounts: set[int]) -> str:
    if account.manual_pause:
        return "idle"
    if account.learning_status in {"training", "stopping"}:
        return "training"
    if account.enabled and account.connection_status == "connected" and account.id in chat_accounts:
        return "work"
    return "idle"
