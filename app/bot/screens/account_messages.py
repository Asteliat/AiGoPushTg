"""Manual message preview and confirmation flow."""
from __future__ import annotations

from app.bot.core import CANCEL
from app.bot.core import Flow
from app.bot.core import check_cb
from app.bot.core import check_msg
from app.bot.core import delete_msg
from app.bot.core import edit
from app.bot.core import finish_prompt_keep_state
from app.bot.core import kb
from app.bot.core import private
from app.bot.core import prompt_message
from app.bot.core import router
from app.storage.db import SessionFactory
from app.storage.models import Account
from app.telegram_gateway import management as telegram_management
from aiogram import F
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery
from aiogram.types import Message
import html

@router.callback_query(F.data.regexp(r"^acc:sendmessage:[0-9]+$"))
async def acc_send_message_start(c: CallbackQuery, state: FSMContext):
    if not await check_cb(c):
        return
    aid = int(c.data.split(":")[2])
    async with SessionFactory() as db:
        account = await db.get(Account, aid)
    if not account or not account.enabled:
        await c.answer("Аккаунт не найден или выключен.", show_alert=True)
        return
    await state.clear()
    await state.update_data(
        manual_message_account_id=aid,
        ui_chat_id=c.message.chat.id,
        ui_message_id=c.message.message_id,
        return_callback=f"acc:view:{aid}",
    )
    await state.set_state(Flow.manual_message_target)
    await edit(
        c,
        f"✉️ Ручная отправка · {account.display_name}\n\n"
        "Укажите один чат для отправки: @username, ссылку https://t.me/username или ID чата.\n"
        "Ссылки-приглашения не поддерживаются. Каналы для публикаций исключены.",
        CANCEL,
    )
@router.message(Flow.manual_message_target)
async def manual_message_target_received(m: Message, state: FSMContext):
    if not await check_msg(m) or not await private(m):
        return
    data = await state.get_data()
    target = (m.text or "").strip()
    await delete_msg(m)
    if not target or len(target) > 256:
        await prompt_message(m, state, "Укажите @username, ссылку t.me или ID чата (до 256 символов).")
        return
    try:
        preview = await telegram_management.preview_message_target(
            int(data["manual_message_account_id"]), target
        )
    except Exception as exc:
        await prompt_message(
            m, state,
            f"Не удалось найти чат: {type(exc).__name__}: {str(exc)[:220]}\n\n"
            "Проверьте username, ссылку или ID и отправьте ещё раз.",
        )
        return
    await state.update_data(manual_message_target=target, manual_message_peer=preview)
    await state.set_state(Flow.manual_message_text)
    await prompt_message(
        m, state,
        f"Чат найден: {preview['title']} · {preview['kind']}\n"
        f"ID: {preview['peer_id']}\n\n"
        "Теперь отправьте текст сообщения (до 4096 символов). Оно будет отправлено только после вашего подтверждения.",
    )
@router.message(Flow.manual_message_text)
async def manual_message_text_received(m: Message, state: FSMContext):
    if not await check_msg(m) or not await private(m):
        return
    data = await state.get_data()
    message_text = (m.text or "").strip()
    await delete_msg(m)
    if not message_text or len(message_text) > 4096:
        await prompt_message(m, state, "Текст должен содержать от 1 до 4096 символов. Отправьте исправленный вариант.")
        return
    preview = data.get("manual_message_peer") or {}
    target = data.get("manual_message_target") or ""
    await state.update_data(manual_message_body=message_text)
    await state.set_state(Flow.manual_message_confirm)
    await finish_prompt_keep_state(
        m, state,
        "📨 Подтвердите отправку\n\n"
        f"Аккаунт: #{data['manual_message_account_id']}\n"
        f"Чат: {html.escape(str(preview.get('title') or target))}\n"
        f"Тип: {html.escape(str(preview.get('kind') or 'чат'))}\n\n"
        f"{html.escape(message_text)}",
        kb([
            [("✅ Отправить сейчас", f"acc:sendmessage:confirm:{data['manual_message_account_id']}")],
            [("❌ Отмена", "nav:cancel")],
        ]),
    )
    await state.set_state(Flow.manual_message_confirm)
@router.callback_query(F.data.startswith("acc:sendmessage:confirm:"))
async def acc_send_message_confirm(c: CallbackQuery, state: FSMContext):
    if not await check_cb(c):
        return
    aid = int(c.data.split(":")[3])
    data = await state.get_data()
    if (
        await state.get_state() != Flow.manual_message_confirm.state
        or int(data.get("manual_message_account_id") or 0) != aid
        or not data.get("manual_message_body")
        or not data.get("manual_message_target")
    ):
        await c.answer("Предпросмотр отправки истёк. Начните заново.", show_alert=True)
        return
    if data.get("manual_message_sending"):
        await c.answer("Отправка уже выполняется.", show_alert=True)
        return
    await state.update_data(manual_message_sending=True)
    await c.answer("Отправляю сообщение…")
    try:
        result = await telegram_management.send_message_to_chat(
            aid, data["manual_message_target"], data["manual_message_body"]
        )
    except telegram_management.FloodWaitError as exc:
        await state.clear()
        await edit(c, f"⏳ Telegram ограничил отправку. Повторите не раньше чем через {int(exc.seconds)} сек.", kb([
            [("↩️ К аккаунту", f"acc:view:{aid}")],
            [("🏠 Главное меню", "nav:home")],
        ]), answer_callback=False)
        return
    except Exception as exc:
        await state.update_data(manual_message_sending=False)
        await edit(
            c,
            f"❌ Не удалось отправить сообщение: {type(exc).__name__}: {str(exc)[:300]}",
            kb([
                [("🔁 Повторить отправку", f"acc:sendmessage:confirm:{aid}")],
                [("↩️ К аккаунту", f"acc:view:{aid}")],
                [("🏠 Главное меню", "nav:home")],
            ]),
            answer_callback=False,
        )
        return
    await state.clear()
    await edit(
        c,
        f"✅ Сообщение отправлено.\n\nАккаунт: #{aid}\nЧат: {html.escape(result['title'])}\n"
        f"ID сообщения: {result['message_id']}",
        kb([
            [("✉️ Отправить ещё", f"acc:sendmessage:{aid}")],
            [("👤 К аккаунту", f"acc:view:{aid}")],
            [("🏠 Главное меню", "nav:home")],
        ]),
        answer_callback=False,
    )
