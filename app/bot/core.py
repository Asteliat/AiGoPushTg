"""Shared router, keyboards, access checks and input helpers."""

from __future__ import annotations

from app.services.ai_responses import global_ai_enabled
from app.services.challenge_monitor import ChallengeMode
from app.services.challenge_settings import read_challenge_mode
from app.settings import get_settings
from app.storage.db import SessionFactory
from app.storage.models import AIAgent
from app.storage.models import Account
from app.storage.models import AppSetting
from app.telegram_gateway import management as telegram_management
from app.telegram_gateway.joining import run_join_with_floodwait
from aiogram import Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State
from aiogram.fsm.state import StatesGroup
from aiogram.types import CallbackQuery
from aiogram.types import ErrorEvent
from aiogram.types import InlineKeyboardButton
from aiogram.types import InlineKeyboardMarkup
from contextlib import asynccontextmanager
from sqlalchemy import select
import asyncio
import logging

router = Router()


@router.errors()
async def handle_unexpected_update_error(event: ErrorEvent):
    error = event.exception
    callback = event.update.callback_query
    if callback:
        try:
            await callback.answer(
                "Не удалось выполнить действие. Попробуйте ещё раз.", show_alert=True
            )
        except Exception:
            pass
    logging.error(
        "Unhandled Telegram update error: %s",
        error,
        exc_info=(type(error), error, error.__traceback__),
    )
    return True


cfg = get_settings()

_CHALLENGE_MODE_LABELS = {
    ChallengeMode.AUTO: "авто",
    ChallengeMode.NOTIFY: "только уведомления",
    ChallengeMode.OFF: "выключены",
}
LOGIN_TASKS: dict[int, asyncio.Task] = {}
PHONE_LOGIN_DATA: dict[int, dict] = {}
QR_LOGIN_DATA: dict[int, dict] = {}
CHAT_CANDIDATES: dict[tuple[int, int], list[dict]] = {}
CHANNEL_CANDIDATES: dict[tuple[int, int], list[dict]] = {}
CHANNEL_PENDING_JOIN: dict[tuple[int, int], dict] = {}
CHAT_PENDING_JOIN: dict[tuple[int, int], dict] = {}
ACCOUNT_JOIN_LOCKS: dict[int, asyncio.Lock] = {}


async def get_join_floodwait_mode() -> str:
    async with SessionFactory() as db:
        setting = await db.get(AppSetting, "join_floodwait_mode")
    return setting.value if setting and setting.value in {"wait", "stop"} else "wait"


async def perform_join_request(aid: int, client, request, on_wait):
    """Serialize joins per account and apply the configured Telegram FloodWait policy."""
    lock = ACCOUNT_JOIN_LOCKS.setdefault(aid, asyncio.Lock())
    async with lock:
        client.flood_sleep_threshold = 0
        mode = await get_join_floodwait_mode()
        return await run_join_with_floodwait(lambda: client(request), mode=mode, on_wait=on_wait)


@asynccontextmanager
async def isolated_telegram_client(account):
    """Reuse the shared isolated-session manager and its cross-process account lock."""
    async with telegram_management.isolated_account_client(int(account.id)) as (_account, client):
        yield client


class Flow(StatesGroup):
    agent_name = State()
    agent_url = State()
    agent_key = State()
    agent_model = State()
    agent_timeout = State()
    agent_edit = State()
    persona_name = State()
    persona_style = State()
    example_incoming = State()
    example_reply = State()
    proxy_host = State()
    proxy_port = State()
    proxy_username = State()
    proxy_password = State()
    learning_prompt_edit = State()
    persona_override = State()
    runtime_value = State()
    channel_setting_value = State()
    log_chat_id = State()
    phone_number = State()
    phone_code = State()
    phone_password = State()
    qr_password = State()
    chat_search_query = State()
    chat_join_link = State()
    channel_search_query = State()
    trigger_terms = State()
    trigger_response = State()
    trigger_cooldown = State()
    trigger_message_cooldown = State()
    trigger_edit_value = State()
    manual_message_target = State()
    manual_message_text = State()
    manual_message_confirm = State()
    management_input = State()
    management_photo = State()
    management_story = State()


def admin(uid):
    return uid is not None and uid in cfg.admin_id_set


def kb(rows):
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=t, callback_data=d) for t, d in row] for row in rows
        ]
    )


MAIN = kb(
    [
        [("👤 Аккаунты", "acc:list"), ("🤖 ИИ-агенты", "agent:menu")],
        [("🎭 Персоны", "persona:menu"), ("💡 Примеры", "example:menu")],
        [("🧰 Инструменты", "tool:menu"), ("⚙️ Настройки", "nav:settings")],
        [("🛡 Проверки", "challenge:settings"), ("🧾 Логи", "logs:menu")],
    ]
)
CANCEL = kb([[("❌ Отмена", "nav:cancel")]])


async def home_text():
    async with SessionFactory() as db:
        accounts = list((await db.scalars(select(Account).order_by(Account.id))).all())
        agents = list((await db.scalars(select(AIAgent))).all())
        ai_on = await global_ai_enabled(db)
    training = sum(
        not a.manual_pause and a.learning_status in {"training", "stopping"} for a in accounts
    )
    paused = sum(a.manual_pause for a in accounts)
    checks = _CHALLENGE_MODE_LABELS[await read_challenge_mode()]
    return (
        "🧭 AiGoPushTg · панель управления\n\n"
        f"👤 аккаунтов: {len(accounts)} · ⏸ пауза: {paused} · 🧠 обучение: {training}\n"
        f"🤖 агентов: {len(agents)} · ИИ: {'включён' if ai_on else 'выключен'}\n"
        f"🛡 проверки: {checks}"
    )


async def edit_anchor(bot, data, text, keyboard):
    chat_id, message_id = data.get("ui_chat_id"), data.get("ui_message_id")
    if not chat_id or not message_id:
        return False
    try:
        await bot.edit_message_text(
            text, chat_id=chat_id, message_id=message_id, reply_markup=keyboard
        )
        return True
    except Exception as exc:
        if "message is not modified" in str(exc).lower():
            return True
        return False


async def prompt_message(m, state, text):
    data = await state.get_data()
    await delete_msg(m)
    if not await edit_anchor(m.bot, data, text, CANCEL):
        await m.answer(text, reply_markup=CANCEL)


async def finish_prompt(m, state, text, keyboard=MAIN):
    data = await state.get_data()
    await delete_msg(m)
    await state.clear()
    if not await edit_anchor(m.bot, data, text, keyboard):
        await m.answer(text, reply_markup=keyboard)


async def finish_prompt_keep_state(m, state, text, keyboard=MAIN):
    """Finish an input step while retaining selected account IDs for the next menu."""
    data = await state.get_data()
    await delete_msg(m)
    await state.set_state(None)
    if not await edit_anchor(m.bot, data, text, keyboard):
        await m.answer(text, reply_markup=keyboard)


async def check_msg(m):
    if not admin(m.from_user.id if m.from_user else None):
        await m.answer("Доступ закрыт. Проверьте ADMIN_IDS.")
        return False
    return True


async def check_cb(c):
    if not admin(c.from_user.id):
        await c.answer("Доступ закрыт", show_alert=True)
        return False
    return True


async def private(m):
    if m.chat.type != "private":
        await m.answer("Это действие доступно только в личном чате с ботом.")
        return False
    return True


async def delete_msg(m):
    try:
        await m.delete()
    except Exception:
        pass


async def show_home(m):
    await m.answer(await home_text(), reply_markup=MAIN)


async def edit(c, text, keyboard, *, answer_callback=True):
    # Every ordinary menu offers a direct route home. Input screens retain their cancel button.
    has_home = any(
        button.callback_data == "nav:home" for row in keyboard.inline_keyboard for button in row
    )
    has_cancel = any(
        button.callback_data == "nav:cancel" for row in keyboard.inline_keyboard for button in row
    )
    if not has_home and not has_cancel and keyboard is not MAIN:
        rows = [list(row) for row in keyboard.inline_keyboard]
        rows.append([InlineKeyboardButton(text="🏠 Главное меню", callback_data="nav:home")])
        keyboard = InlineKeyboardMarkup(inline_keyboard=rows)
    if answer_callback:
        # Acknowledge immediately so slow Telegram edits never leave the spinner running.
        try:
            await c.answer()
        except Exception:
            pass
    if c.message:
        try:
            await c.message.edit_text(text, reply_markup=keyboard)
        except Exception as exc:
            if "message is not modified" not in str(exc).lower():
                try:
                    await c.message.answer(text, reply_markup=keyboard)
                except Exception:
                    pass


def back_home():
    return kb([[("🏠 Главное меню", "nav:home")]])


async def management_prompt(
    c: CallbackQuery, state: FSMContext, action: str, aid: int, prompt: str
):
    previous = await state.get_data()
    selected_ids = previous.get("management_account_ids") or [aid]
    await state.clear()
    await state.update_data(
        management_action=action,
        management_account_id=aid,
        management_account_ids=[int(value) for value in selected_ids],
        ui_chat_id=c.message.chat.id,
        ui_message_id=c.message.message_id,
        module_return_callback="tool:account-settings:menu",
        return_callback="tool:account-settings:menu",
    )
    await state.set_state(Flow.management_input)
    await edit(c, prompt + "\n\nОтмена — кнопкой ниже.", CANCEL)
