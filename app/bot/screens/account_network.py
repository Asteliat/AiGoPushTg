"""Per-account proxy settings."""

from __future__ import annotations

from app.bot.core import cfg
from app.bot.core import check_cb
from app.bot.core import edit
from app.bot.core import kb
from app.bot.core import router
from app.bot.navigation import cancel_return_for
from app.storage.db import SessionFactory
from app.storage.models import Account
from aiogram import F
from aiogram.fsm.context import FSMContext


@router.callback_query(F.data.startswith("acc:proxysettings:"))
async def acc_proxy_settings(c, account_id: int | None = None):
    if not await check_cb(c):
        return
    aid = account_id if account_id is not None else int(c.data.split(":")[2])
    async with SessionFactory() as db:
        account = await db.get(Account, aid)
    if not account:
        await c.answer("Аккаунт не найден", show_alert=True)
        return
    current = "прокси настроен" if account.proxy_config_encrypted else "прямое соединение"
    await edit(
        c,
        f"🌐 Настройки прокси аккаунта #{aid}\nТекущее подключение: {current}. Настройки сохраняются зашифрованными.",
        kb(
            [
                [("🔄 Изменить прокси", f"acc:proxyset:{aid}")],
                [("🚫 Убрать прокси", f"acc:proxyclear:{aid}")],
                [("⬅️ К аккаунту", f"acc:view:{aid}"), ("🏠 Главное меню", "nav:home")],
            ]
        ),
    )


@router.callback_query(F.data.startswith("acc:proxyset:"))
async def acc_proxy_set(c, state: FSMContext):
    if not await check_cb(c):
        return
    aid = int(c.data.split(":")[2])
    if not cfg.proxy_encryption_key.strip():
        await c.answer("Сначала задайте PROXY_ENCRYPTION_KEY в .env.", show_alert=True)
        return
    await state.clear()
    await state.update_data(
        proxy_account_id=aid,
        ui_chat_id=c.message.chat.id,
        ui_message_id=c.message.message_id,
        return_callback=cancel_return_for(c.data),
    )
    await edit(
        c,
        "Выберите тип прокси для существующего аккаунта:",
        kb(
            [
                [("SOCKS5", "proxy:type:socks5"), ("HTTP", "proxy:type:http")],
                [("⬅️ Отмена", f"acc:proxysettings:{aid}")],
            ]
        ),
    )


@router.callback_query(F.data.startswith("acc:proxyclear:"))
async def acc_proxy_clear_confirm(c):
    if not await check_cb(c):
        return
    aid = int(c.data.split(":")[2])
    await edit(
        c,
        f"Убрать прокси у аккаунта #{aid}? После этого worker переподключит его напрямую, если аккаунт включён и не на паузе.",
        kb(
            [
                [("🚫 Да, убрать прокси", f"acc:proxyclearconfirm:{aid}")],
                [("⬅️ Отмена", f"acc:proxysettings:{aid}")],
            ]
        ),
    )


@router.callback_query(F.data.startswith("acc:proxyclearconfirm:"))
async def acc_proxy_clear(c):
    if not await check_cb(c):
        return
    aid = int(c.data.split(":")[2])
    async with SessionFactory() as db:
        account = await db.get(Account, aid)
        if not account:
            await c.answer("Аккаунт не найден", show_alert=True)
            return
        account.proxy_config_encrypted = None
        account.connection_status = (
            "paused"
            if account.manual_pause
            else ("connecting" if account.enabled else "disconnected")
        )
        await db.commit()
    await edit(
        c,
        f"Прокси аккаунта #{aid} удалён. Worker переподключится согласно состоянию аккаунта.",
        kb(
            [
                [("🌐 Настройки прокси", f"acc:proxysettings:{aid}")],
                [("👤 К аккаунту", f"acc:view:{aid}"), ("🏠 Главное меню", "nav:home")],
            ]
        ),
    )
