from __future__ import annotations
import asyncio, csv, io, os, re, secrets, tempfile, logging, hashlib, json
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from aiogram import F, Router
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message, BufferedInputFile, ErrorEvent
from sqlalchemy import select
from telethon import TelegramClient, utils
from telethon.errors import FloodWaitError, SessionPasswordNeededError
from app.telegram_gateway.joining import run_join_with_floodwait
from telethon.tl.functions.channels import JoinChannelRequest
from telethon.tl.functions.messages import CheckChatInviteRequest, ImportChatInviteRequest
from telethon.tl.types import ChatInviteAlready
from app.telegram_gateway.discovery import search_public_groups, search_web_groups, verify_group_candidates, find_invite_links_in_dialogs, search_public_channels, search_web_channels, verify_channel_candidates
from app.services.ai_responses import global_ai_enabled
from app.services.chat_bindings import discovered_binding_values
from app.services.channels import (
    decode_channel_comment_policy_action,
    encode_channel_comment_policy_action,
    effective_channel_chat_replies_enabled,
    effective_channel_reply_mode,
    resolve_channel_comment_policy,
)
from app.settings import get_settings
from app.storage.db import SessionFactory
from app.storage.models import Account, AIAgent, AppSetting, ChatBinding, ChannelBinding, ChannelAnalysis, Example, Persona, TriggerRule
from app.services.triggers import format_trigger_duration, parse_trigger_terms
from app.telegram_gateway.proxy import ProxyConfigurationError, encrypt_proxy, telethon_proxy
from app.telegram_gateway.session import configure_telethon_sqlite_session
from app.telegram_gateway import management as telegram_management
from app.services import automation as telegram_automation

router = Router()


@router.errors()
async def handle_unexpected_update_error(event: ErrorEvent):
    error = event.exception
    callback = event.update.callback_query
    if callback:
        try:
            await callback.answer("Не удалось выполнить действие. Попробуйте ещё раз.", show_alert=True)
        except Exception:
            pass
    logging.error(
        "Unhandled Telegram update error: %s",
        error,
        exc_info=(type(error), error, error.__traceback__),
    )
    return True
cfg = get_settings()


async def record_contact_operation(account_id: int, action: str, details: str) -> None:
    key = f"contacts.log.{int(account_id)}"
    async with SessionFactory() as db:
        row = await db.get(AppSetting, key)
        entries = []
        if row and row.value:
            try:
                loaded = json.loads(row.value)
                entries = loaded if isinstance(loaded, list) else []
            except (TypeError, ValueError):
                entries = []
        entries.append({
            "at": datetime.now(timezone.utc).isoformat(),
            "action": str(action)[:40],
            "details": str(details)[:500],
        })
        value = json.dumps(entries[-200:], ensure_ascii=False)
        if row:
            row.value = value
        else:
            db.add(AppSetting(key=key, value=value))
        await db.commit()
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
        return await run_join_with_floodwait(
            lambda: client(request), mode=mode, on_wait=on_wait
        )



@asynccontextmanager
async def isolated_telegram_client(account):
    """Reuse the shared isolated-session manager and its cross-process account lock."""
    async with telegram_management.isolated_account_client(int(account.id)) as (_account, client):
        yield client

# All navigation uses inline keyboards.

class Flow(StatesGroup):
    agent_name=State(); agent_url=State(); agent_key=State(); agent_model=State(); agent_timeout=State()
    agent_edit=State(); persona_name=State(); persona_style=State()
    example_incoming=State(); example_reply=State()
    proxy_host=State(); proxy_port=State(); proxy_username=State(); proxy_password=State()
    learning_prompt_edit=State(); persona_override=State()
    runtime_value=State(); channel_setting_value=State(); log_chat_id=State()
    phone_number=State(); phone_code=State(); phone_password=State(); qr_password=State()
    chat_search_query=State(); chat_join_link=State(); channel_search_query=State()
    trigger_terms=State(); trigger_response=State(); trigger_cooldown=State(); trigger_message_cooldown=State(); trigger_edit_value=State(); management_input=State(); management_campaign_content=State(); management_campaign_confirm=State(); management_photo=State()

def admin(uid): return uid is not None and uid in cfg.admin_id_set
def kb(rows):
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=t, callback_data=d) for t,d in row] for row in rows])

MAIN = kb([
    [("👤 Аккаунты", "acc:list"), ("🤖 ИИ-агенты", "agent:menu")],
    [("🎭 Персоны", "persona:menu"), ("💡 Примеры", "example:menu")],
    [("🧰 Инструменты", "tool:menu"), ("⚙️ Настройки", "nav:settings")],
])
CANCEL = kb([[("❌ Отмена", "nav:cancel")]])

async def home_text():
    async with SessionFactory() as db:
        accounts = list((await db.scalars(select(Account).order_by(Account.id))).all())
        agents = list((await db.scalars(select(AIAgent))).all())
        ai_on = await global_ai_enabled(db)
    training = sum(not a.manual_pause and a.learning_status in {"training", "stopping"} for a in accounts)
    paused = sum(a.manual_pause for a in accounts)
    return (
        "🧭 AtcivPerson\nПанель управления Telegram-аккаунтами\n\n"
        f"👤 Аккаунтов: {len(accounts)} · ⏸ На паузе: {paused}\n"
        f"🧠 На обучении: {training}\n"
        f"🤖 ИИ-агентов: {len(agents)} · Глобальный ИИ: {'включён' if ai_on else 'выключен'}\n"
        "Выберите раздел ниже."
    )

async def edit_anchor(bot, data, text, keyboard):
    chat_id, message_id = data.get("ui_chat_id"), data.get("ui_message_id")
    if not chat_id or not message_id:
        return False
    try:
        await bot.edit_message_text(text, chat_id=chat_id, message_id=message_id, reply_markup=keyboard)
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
async def check_msg(m):
    if not admin(m.from_user.id if m.from_user else None):
        await m.answer("Доступ закрыт. Проверьте ADMIN_IDS."); return False
    return True
async def check_cb(c):
    if not admin(c.from_user.id):
        await c.answer("Доступ закрыт", show_alert=True); return False
    return True
async def private(m):
    if m.chat.type != "private":
        await m.answer("Это действие доступно только в личном чате с ботом."); return False
    return True
async def delete_msg(m):
    try: await m.delete()
    except Exception: pass
async def show_home(m):
    await m.answer(await home_text(), reply_markup=MAIN)
async def edit(c, text, keyboard, *, answer_callback=True):
    # Every ordinary menu offers a direct route home. Input screens retain their cancel button.
    has_home = any(button.callback_data == "nav:home" for row in keyboard.inline_keyboard for button in row)
    has_cancel = any(button.callback_data == "nav:cancel" for row in keyboard.inline_keyboard for button in row)
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
                try: await c.message.answer(text, reply_markup=keyboard)
                except Exception: pass
def back_home(): return kb([[('🏠 Главное меню','nav:home')]])

def cancel_return_for(callback_data: str) -> str:
    parts = callback_data.split(":")
    if callback_data.startswith("channel:edit:") and len(parts) >= 5:
        return f"channel:{'gsettings' if parts[2] == 'g' else 'csettings'}:{parts[3]}"
    if callback_data.startswith("acc:context:edit:"): return f"acc:learnprompt:{parts[3]}"
    if callback_data.startswith("acc:personaoverride:"): return f"acc:view:{parts[2]}"
    if callback_data.startswith("trigger:account:"): return callback_data
    if callback_data.startswith("trigger:global"): return "trigger:global"
    if callback_data.startswith("acc:proxyset:"): return f"acc:proxysettings:{parts[2]}"
    if callback_data.startswith("chat:search:"): return "tool:menu"
    if callback_data.startswith("chat:joinlink:"): return f"acc:view:{parts[2]}"
    if callback_data.startswith("logs:add:"): return f"logs:category:{parts[2]}"
    if callback_data.startswith(("runtime:input:", "runtime:apply:")): return "runtime:menu"
    if callback_data.startswith(("agent:rt:input:", "agent:channelrt:input:")): return f"agent:runtime:menu:{parts[3]}"
    if callback_data.startswith("agent:edit:"): return f"agent:view:{parts[3]}"
    if callback_data.startswith("agent:"): return "agent:menu"
    if callback_data.startswith("persona:edit:"): return f"persona:view:{parts[2]}"
    if callback_data.startswith("persona:"): return "persona:menu"
    if callback_data.startswith("example:addacc:"): return f"example:account:{parts[2]}"
    if callback_data.startswith("example:add:"): return f"persona:view:{parts[2]}" if parts[2] != "0" else "example:menu"
    if callback_data.startswith("example:account:"): return callback_data
    if callback_data.startswith("example:"): return "example:menu"
    if callback_data.startswith("channel:"): return "channel:menu"
    if callback_data.startswith("acc:"): return f"acc:view:{parts[-1]}" if parts[-1].isdigit() else "acc:list"
    return callback_data


async def restore_previous_screen(c, callback_data):
    if not callback_data:
        await edit(c, await home_text(), MAIN)
        return
    parts = callback_data.split(":")
    if callback_data == "tool:menu": await tools_menu(c)
    elif callback_data == "trigger:global": await trigger_screen(c, "global")
    elif callback_data.startswith("trigger:account:"): await trigger_screen(c, "account", int(parts[2]))
    elif callback_data.startswith("acc:view:"): await acc_view(c, int(parts[2]))
    elif callback_data == "acc:list": await acc_list(c)
    elif callback_data.startswith("acc:learnprompt:"): await acc_learning_prompt(c, int(parts[2]))
    elif callback_data.startswith("acc:learn:"): await acc_learning_menu(c, int(parts[2]))
    elif callback_data.startswith("acc:proxysettings:"): await acc_proxy_settings(c, int(parts[2]))
    elif callback_data.startswith("channel:account:"): await channel_account_screen(c, int(parts[2]))
    elif callback_data.startswith("channel:gsettings:"): await channel_settings_screen(c, "global", int(parts[2]))
    elif callback_data.startswith("channel:csettings:"): await channel_settings_screen(c, "channel", int(parts[2]))
    elif callback_data.startswith("chat:account:"): await chat_account_screen(c, int(parts[2]), int(parts[3]) if len(parts)>3 else 0)
    elif callback_data.startswith("agent:view:"): await agent_view(c, int(parts[2]))
    elif callback_data == "agent:menu": await agents_cb(c)
    elif callback_data.startswith("agent:runtime:menu:"): await agent_runtime_menu(c, int(parts[-1]))
    elif callback_data.startswith("persona:view:"): await persona_view(c, int(parts[2]))
    elif callback_data == "persona:menu": await persona_menu(c)
    elif callback_data.startswith("example:account:"): await account_examples(c, int(parts[2]))
    elif callback_data.startswith("example:view:"): await example_view(c, int(parts[2]))
    elif callback_data == "example:menu": await example_menu(c)
    elif callback_data == "logs:menu": await logs_menu(c)
    elif callback_data.startswith("logs:category:"): await logs_category(c, parts[2])
    elif callback_data == "runtime:menu": await runtime_settings_menu(c)
    elif callback_data == "channel:menu": await channels_legacy_redirect(c)
    else: await edit(c, "Действие отменено. Вернитесь в предыдущий раздел:", kb([[("↩️ Назад", callback_data)]]))


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
    await restore_previous_screen(c, previous)


@router.callback_query(F.data == "tool:menu")
async def tools_menu(c: CallbackQuery):
    if not await check_cb(c): return
    await edit(c, "🧰 Инструменты\n\nВыберите модуль. Его параметры настраиваются внутри самого модуля и не смешиваются с настройками других модулей.", kb([
        [("📩 Инвайтинг", "tool:module:invite"), ("🔍 Парсинг", "tool:module:parser")],
        [("📇 Контакты", "tool:module:contacts"), ("📤 Рассылка", "tool:module:broadcast")],
        [("📢 Подписка и отписка", "tool:module:subscription"), ("❤️ Реакции", "tool:module:reaction")],
        [("✏️ Редактирование профиля", "tool:module:profile")],
        [("🔎 Поиск групп", "tool:groups"), ("📣 Поиск каналов", "tool:channels")],
        [("🎯 Глобальные триггеры", "trigger:global")],
        [("⬅️ Главное меню", "nav:home")],
    ]))


MODULE_ACTIONS = {
    "invite": ("📩 Инвайтинг", [
        ("➕ Пригласить выбранных пользователей", "campaign"),
        ("👥 Парсинг участников своей группы", "parseparticipants"),
        ("📋 Очередь и журнал", "campaigns"),
    ]),
    "parser": ("🔍 Парсинг", [
        ("🗂 Мои группы и каналы", "parsechats"),
        ("ℹ️ Публичная информация о чате", "chatinfo"),
        ("📝 История сообщений, ссылки и комментарии", "parsechannel"),
        ("👥 Участники собственной группы", "parseparticipants"),
    ]),
    "contacts": ("📇 Контакты", [
        ("📇 Просмотреть контакты", "contacts"),
        ("➕ Добавить контакт", "addcontact"),
        ("🗑 Удалить контакт", "delcontact"),
        ("🔎 Проверить дубликаты", "dedupecontacts"),
        ("📥 Импорт TXT/CSV", "importcontacts"),
        ("📤 Экспорт CSV", "exportcontacts"),
        ("📜 Журнал операций", "contactlog"),
    ]),
    "broadcast": ("📤 Рассылка", [
        ("✉️ Создать рассылку", "campaign"),
        ("🤖 Автоответчик (только при выключенном ИИ)", "autoresponder"),
        ("📋 Очередь и журнал", "campaigns"),
        ("📅 Список расписаний", "schedules"),
    ]),
    "subscription": ("📢 Подписка и отписка", [
        ("➕ Пакетная подписка", "joinbatch"),
        ("➖ Пакетная отписка", "leavebatch"),
        ("🗓 Повторяющаяся подписка", "joinrepeat"),
        ("🗓 Повторяющаяся отписка", "leaverepeat"),
        ("➕ Вступить по ссылке", "join"),
        ("➖ Выйти из чата", "leave"),
        ("🗓 Расписания", "schedules"),
        ("📋 Очередь и журнал", "campaigns"),
    ]),
    "reaction": ("❤️ Реакции", [
        ("❤️ Реакция на публикацию", "reaction"),
        ("🕒 Запланировать реакцию", "schedreaction"),
        ("📆 Очередь реакций", "reactions"),
    ]),
    "profile": ("✏️ Редактирование профиля", [
        ("👤 Посмотреть профиль", "profile"),
        ("✏️ Изменить имя, BIO и username", "editprofile"),
        ("📷 Одно фото для выбранных аккаунтов", "photo"),
        ("🖼 Отдельное фото каждому аккаунту", "photosequential"),
        ("🤖 Автоответчик (только при выключенном ИИ)", "autoresponder"),
    ]),
}


async def render_module_account_selection(c: CallbackQuery, state: FSMContext, module: str, selected: list[int]):
    async with SessionFactory() as db:
        accounts = list((await db.scalars(
            select(Account).where(Account.enabled.is_(True)).order_by(Account.id)
        )).all())
    selected = [int(aid) for aid in selected if any(int(account.id) == int(aid) for account in accounts)]
    await state.update_data(module_selection_module=module, module_selected_accounts=selected)
    rows = [
        [(("☑️ " if int(account.id) in selected else "▫️ ") + f"{account.display_name[:24]} · #{account.id}",
          f"tool:module:toggle:{module}:{account.id}")]
        for account in accounts
    ]
    rows.extend([
        [("🌐 Выбрать все аккаунты", f"tool:module:all:{module}")],
        [("➡️ Продолжить · " + str(len(selected)), f"tool:module:continue:{module}")],
        [("↩️ Инструменты", "tool:menu")],
    ])
    if module in {"invite", "broadcast", "subscription", "reaction"}:
        rows.append([("⚙️ Настройки модуля", f"tool:policy:{module}")])
    await edit(c, f"{MODULE_ACTIONS[module][0]}\n\nОтметьте один или несколько аккаунтов. Настройки применяются только к этому модулю.", kb(rows))


async def render_campaign_kind_selection(c: CallbackQuery, state: FSMContext, module: str, account_ids: list[int]):
    if not account_ids:
        await c.answer("Выберите хотя бы один аккаунт.", show_alert=True)
        return
    async with SessionFactory() as db:
        valid = list((await db.scalars(
            select(Account).where(Account.id.in_(account_ids), Account.enabled.is_(True)).order_by(Account.id)
        )).all())
    ids = [int(account.id) for account in valid]
    if not ids:
        await c.answer("Нет включённых выбранных аккаунтов.", show_alert=True)
        return
    await state.update_data(
        campaign_account_ids=ids, management_account_id=ids[0],
        module_selection_module=module,
    )
    if module == "invite":
        kinds = [
            [("🔗 Отправить ссылку-приглашение", f"tool:campaign:start:{ids[0]}:invite:once")],
            [("🗓 Повторять отправку ссылки", f"tool:campaign:start:{ids[0]}:invite:recurring")],
            [("👥 Пригласить выбранных пользователей в группу", f"tool:campaign:start:{ids[0]}:direct_invite:once")],
            [("🗓 Повторять прямые приглашения", f"tool:campaign:start:{ids[0]}:direct_invite:recurring")],
        ]
    else:
        kinds = [
            [("📨 Личные сообщения выбранным получателям", f"tool:campaign:start:{ids[0]}:broadcast:once")],
            [("📣 Публикация в своих группах", f"tool:campaign:start:{ids[0]}:group_broadcast:once")],
            [("🗓 Повторяющаяся рассылка", f"tool:campaign:start:{ids[0]}:broadcast:recurring")],
        ]
    kinds.extend([
        [("↩️ Выбрать аккаунты", f"tool:module:{module}")],
        [("🏠 Главное меню", "nav:home")],
    ])
    await edit(c, f"{MODULE_ACTIONS[module][0]}\n\nВыбрано аккаунтов: {len(ids)}\n"
        + ", ".join(f"#{aid}" for aid in ids) + "\n\nВыберите тип задачи:", kb(kinds))


MULTI_ACCOUNT_MODULES = {"broadcast", "invite", "subscription", "reaction", "profile", "contacts"}


async def render_selected_module_actions(c: CallbackQuery, state: FSMContext, module: str, account_ids: list[int]):
    async with SessionFactory() as db:
        accounts = list((await db.scalars(
            select(Account).where(Account.id.in_(account_ids), Account.enabled.is_(True)).order_by(Account.id)
        )).all())
    ids = [int(account.id) for account in accounts]
    if not ids:
        await c.answer("Выберите хотя бы один включённый аккаунт.", show_alert=True)
        return
    await state.update_data(
        management_account_ids=ids, management_account_id=ids[0],
        module_selection_module=module,
    )
    title, actions = MODULE_ACTIONS[module]
    rows = [[(label, f"tool:mg:{action}:{ids[0]}")] for label, action in actions]
    rows.extend([
        [("⛔ Статус FloodWait", f"tool:quarantine:{module}:{ids[0]}")],
        *([[("⚙️ Настройки модуля", f"tool:policy:{module}")]] if module in {"invite", "broadcast", "subscription", "reaction"} else []),
        [("↩️ Выбрать аккаунты", f"tool:module:{module}")],
    ])
    await edit(
        c,
        f"{title}\n\nВыбрано аккаунтов: {len(ids)} · " + ", ".join(f"#{aid}" for aid in ids)
        + "\nДействие будет создано отдельно для каждого выбранного аккаунта.",
        kb(rows),
    )


@router.callback_query(F.data.startswith("tool:module:toggle:"))
async def tool_module_toggle_account(c: CallbackQuery, state: FSMContext):
    if not await check_cb(c):
        return
    _, _, _, module, aid_raw = c.data.split(":")
    data = await state.get_data()
    selected = list(data.get("module_selected_accounts") or [])
    aid = int(aid_raw)
    if aid in selected:
        selected.remove(aid)
    else:
        selected.append(aid)
    await render_module_account_selection(c, state, module, selected)


@router.callback_query(F.data.startswith("tool:module:all:"))
async def tool_module_select_all(c: CallbackQuery, state: FSMContext):
    if not await check_cb(c):
        return
    module = c.data.rsplit(":", 1)[-1]
    if module not in MULTI_ACCOUNT_MODULES:
        await c.answer("Для этого модуля выберите один аккаунт.", show_alert=True)
        return
    accounts = await _enabled_accounts_for_module()
    ids = [int(account.id) for account in accounts]
    if module in {"broadcast", "invite"}:
        await render_campaign_kind_selection(c, state, module, ids)
    else:
        await render_selected_module_actions(c, state, module, ids)


async def _enabled_accounts_for_module():
    async with SessionFactory() as db:
        return list((await db.scalars(
            select(Account).where(Account.enabled.is_(True)).order_by(Account.id)
        )).all())


@router.callback_query(F.data.startswith("tool:module:continue:"))
async def tool_module_continue_accounts(c: CallbackQuery, state: FSMContext):
    if not await check_cb(c):
        return
    module = c.data.rsplit(":", 1)[-1]
    data = await state.get_data()
    selected = data.get("module_selected_accounts") or []
    if module in {"broadcast", "invite"}:
        await render_campaign_kind_selection(c, state, module, selected)
    elif module in MULTI_ACCOUNT_MODULES:
        await render_selected_module_actions(c, state, module, selected)
    else:
        await c.answer("Для этого модуля выберите один аккаунт.", show_alert=True)


@router.callback_query(F.data.startswith("tool:module:"))
async def tool_module_accounts(c: CallbackQuery, state: FSMContext):
    if not await check_cb(c):
        return
    if ":account:" in c.data:
        parts = c.data.split(":")
        module, aid = parts[3], int(parts[4])
        if module not in MODULE_ACTIONS:
            await c.answer("Неизвестный модуль.", show_alert=True)
            return
        async with SessionFactory() as db:
            account = await db.get(Account, aid)
        if not account or not account.enabled:
            await c.answer("Аккаунт не найден или выключен.", show_alert=True)
            return
        if module in {"broadcast", "invite"}:
            await state.clear()
            await render_campaign_kind_selection(c, state, module, [aid])
            return
        if module in MULTI_ACCOUNT_MODULES:
            await state.clear()
            await render_selected_module_actions(c, state, module, [aid])
            return
        title, actions = MODULE_ACTIONS[module]
        rows = [[(label, f"tool:mg:{action}:{aid}")] for label, action in actions]
        rows.extend([
            [("⛔ Статус FloodWait / снять карантин", f"tool:quarantine:{module}:{aid}")],
            *([[("⚙️ Настройки модуля", f"tool:policy:{module}")]] if module in {"invite", "broadcast", "subscription", "reaction"} else []),
            [("↩️ К аккаунтам модуля", f"tool:module:{module}")],
        ])
        await edit(c, f"{title} · {account.display_name} · #{aid}\n\nВыберите действие:", kb(rows))
        return
    module = c.data.rsplit(":", 1)[-1]
    if module not in MODULE_ACTIONS:
        await c.answer("Неизвестный модуль.", show_alert=True)
        return
    async with SessionFactory() as db:
        accounts = list((await db.scalars(
            select(Account).where(Account.enabled.is_(True)).order_by(Account.id)
        )).all())
    title = MODULE_ACTIONS[module][0]
    if module in MULTI_ACCOUNT_MODULES:
        await state.clear()
        await render_module_account_selection(c, state, module, [])
        return
    rows = [[(f"👤 {a.display_name[:28]} · #{a.id}", f"tool:module:account:{module}:{a.id}")] for a in accounts]
    rows.extend([
        *([[("⚙️ Настройки модуля", f"tool:policy:{module}")]] if module in {"invite", "broadcast", "subscription", "reaction"} else []),
        [("↩️ Инструменты", "tool:menu")],
    ])
    await edit(c, f"{title}\n\nВыберите аккаунт для выполнения операций. Параметры этого модуля настраиваются отдельно от остальных.", kb(rows))


@router.callback_query(F.data.startswith("tool:module:account:"))
async def tool_module_account_actions(c: CallbackQuery):
    if not await check_cb(c):
        return
    _, _, _, module, aid_raw = c.data.split(":")
    aid = int(aid_raw)
    if module not in MODULE_ACTIONS:
        await c.answer("Неизвестный модуль.", show_alert=True)
        return
    async with SessionFactory() as db:
        account = await db.get(Account, aid)
    if not account or not account.enabled:
        await c.answer("Аккаунт не найден или выключен.", show_alert=True)
        return
    title, actions = MODULE_ACTIONS[module]
    rows = [[(label, f"tool:mg:{action}:{aid}")] for label, action in actions]
    rows.extend([
        [("⛔ Статус FloodWait / снять карантин", f"tool:quarantine:{module}:{aid}")],
        *([[("⚙️ Настройки модуля", f"tool:policy:{module}")]] if module in {"invite", "broadcast", "subscription", "reaction"} else []),
        [("↩️ К аккаунтам модуля", f"tool:module:{module}")],
    ])
    await edit(c, f"{title} · {account.display_name} · #{aid}\n\nВыберите действие:", kb(rows))


@router.callback_query(F.data.startswith("tool:quarantine:"))
async def tool_module_quarantine(c: CallbackQuery):
    if not await check_cb(c):
        return
    if c.data.startswith("tool:quarantine:clear:"):
        return await tool_module_quarantine_clear(c)
    _, _, module, aid_raw = c.data.split(":")
    aid = int(aid_raw)
    if module not in {"invite", "broadcast", "subscription", "reaction"}:
        await c.answer("Неизвестный модуль.", show_alert=True)
        return
    status = await telegram_automation.module_quarantine_status(aid, module)
    if status["quarantined"]:
        text = (
            f"⛔ Карантин · {module} · аккаунт #{aid}\\n\\n"
            f"FloodWait-срабатываний: {status['floodwait_count']}\\n"
            f"Причина: {status['reason']}\\n\\n"
            "Остальные модули аккаунта продолжают работать. Снять карантин можно вручную."
        )
        rows = [[("♻️ Снять карантин", f"tool:quarantine:clear:{module}:{aid}")]]
    else:
        text = (
            f"✅ Карантина нет · {module} · аккаунт #{aid}\\n\\n"
            f"Зафиксировано FloodWait: {status['floodwait_count']}."
        )
        rows = []
    rows.extend([
        [("↩️ К модулю", f"tool:module:account:{module}:{aid}")],
        [("🏠 Главное меню", "nav:home")],
    ])
    await edit(c, text, kb(rows))


@router.callback_query(F.data.startswith("tool:quarantine:clear:"))
async def tool_module_quarantine_clear(c: CallbackQuery):
    if not await check_cb(c):
        return
    _, _, _, module, aid_raw = c.data.split(":")
    aid = int(aid_raw)
    await telegram_automation.clear_module_quarantine(aid, module)
    await edit(c, f"Карантин модуля «{module}» для аккаунта #{aid} снят. Счётчик FloodWait сброшен.", kb([
        [("↩️ К модулю", f"tool:module:account:{module}:{aid}")],
        [("🏠 Главное меню", "nav:home")],
    ]))


POLICY_FIELDS = {
    "interval_seconds": ("⏱ Интервал между действиями", "секунд", 1, 9999),
    "per_account_limit": ("📦 Лимит на аккаунт за задачу", "операций", 1, 9999),
    "daily_account_limit": ("📅 Лимит на аккаунт за 24 часа", "операций", 1, 9999),
    "batch_size": ("🧩 Размер пакета", "операций", 1, 9999),
    "batch_pause_seconds": ("⏸ Пауза между пакетами", "секунд", 1, 9999),
    "floodwait_wait_seconds": ("🛡 Максимальное ожидание FloodWait", "секунд", 1, 9999),
    "floodwait_threshold": ("🚧 FloodWait до карантина", "срабатываний", 1, 100),
}


@router.callback_query(F.data.startswith("tool:policy:field:"))
async def tool_policy_field(c: CallbackQuery, state: FSMContext):
    if not await check_cb(c):
        return
    _, _, _, module, field = c.data.split(":")
    if module not in {"invite", "broadcast", "subscription", "reaction"} or field not in POLICY_FIELDS:
        await c.answer("Неизвестный параметр.", show_alert=True)
        return
    label, unit, minimum, maximum = POLICY_FIELDS[field]
    policy = await telegram_automation.module_policy(module)
    await state.clear()
    await state.update_data(
        management_action=f"policy-field-{module}-{field}",
        management_account_id=0,
        ui_chat_id=c.message.chat.id,
        ui_message_id=c.message.message_id,
        return_callback=f"tool:policy:{module}",
    )
    await state.set_state(Flow.management_input)
    await edit(c, f"{label}\n\nТекущее значение: {policy[field]} {unit}.\nВведите новое целое число от {minimum} до {maximum}.", CANCEL)


@router.callback_query(F.data.startswith("tool:policy:"))
async def tool_policy_edit(c: CallbackQuery, state: FSMContext):
    if not await check_cb(c):
        return
    module = c.data.rsplit(":", 1)[-1]
    if module not in {"invite", "broadcast", "subscription", "reaction"}:
        await c.answer("Неизвестный модуль.", show_alert=True)
        return
    policy = await telegram_automation.module_policy(module)
    title = MODULE_ACTIONS[module][0]
    rows = []
    for field, (label, unit, _minimum, _maximum) in POLICY_FIELDS.items():
        rows.append([(f"{label}: {policy[field]} {unit}", f"tool:policy:field:{module}:{field}")])
    rows.extend([
        [("↩️ Назад в модуль", f"tool:module:{module}")],
        [("🏠 Главное меню", "nav:home")],
    ])
    await edit(c, f"⚙️ Настройки модуля · {title}\n\nВыберите параметр, который хотите изменить. Каждое значение настраивается отдельно кнопкой.", kb(rows))


@router.callback_query(F.data == "tool:groups")
async def tool_groups_accounts(c: CallbackQuery):
    if not await check_cb(c): return
    async with SessionFactory() as db:
        accounts = list((await db.scalars(
            select(Account).where(Account.enabled.is_(True)).order_by(Account.id)
        )).all())
    if not accounts:
        await edit(c, "🔎 Поиск групп\n\nНет включённых аккаунтов. Сначала подключите и включите Telegram-аккаунт.", kb([
            [("👤 Аккаунты", "acc:list")],
            [("⬅️ Инструменты", "tool:menu")],
        ]))
        return
    rows = [[(f"🌐 Все аккаунты · {len(accounts)}", "tool:groups:all")]]
    rows += [[(f"👤 {a.display_name[:32]} · #{a.id}", f"tool:groups:account:{a.id}")] for a in accounts]
    rows += [[("⬅️ Инструменты", "tool:menu")]]
    await edit(c, "🔎 Поиск Telegram-групп\n\nМожно искать сразу всеми включёнными аккаунтами — так Telegram-native поиск соберёт больше вариантов. Либо выберите один аккаунт. В результатах останутся только группы и супергруппы; каналы будут отсеяны.", kb(rows))


@router.callback_query(F.data == "tool:groups:all")
async def tool_groups_all_accounts(c: CallbackQuery, state: FSMContext):
    if not await check_cb(c): return
    async with SessionFactory() as db:
        accounts = list((await db.scalars(
            select(Account).where(Account.enabled.is_(True)).order_by(Account.id)
        )).all())
    if not accounts:
        await c.answer("Нет включённых аккаунтов.", show_alert=True)
        return
    ids = [int(account.id) for account in accounts]
    await state.clear()
    await state.update_data(
        ui_chat_id=c.message.chat.id,
        ui_message_id=c.message.message_id,
        return_callback="tool:menu",
        chat_search_account_id=ids[0],
        chat_search_account_ids=ids,
    )
    await state.set_state(Flow.chat_search_query)
    await edit(
        c,
        f"🌐 Поиск всеми аккаунтами · {len(ids)} аккаунтов\n\n"
        "Введите тему или несколько ключевых слов. Например: «Python разработка», "
        "«настольные игры», «дизайн».\n\n"
        "Каждый включённый аккаунт выполнит собственный поиск в Telegram. "
        "Результаты объединятся без дублей, затем ссылки будут проверены через Telegram API. "
        "Поисковики работают параллельно; если источник показывает CAPTCHA или ограничивает запросы, "
        "он временно отключается, а остальные продолжают поиск.",
        CANCEL,
    )


@router.callback_query(F.data.startswith("tool:groups:account:"))
async def tool_groups_account(c: CallbackQuery, state: FSMContext):
    if not await check_cb(c): return
    aid = int(c.data.rsplit(":", 1)[1])
    async with SessionFactory() as db:
        account = await db.get(Account, aid)
    if not account or not account.enabled:
        await c.answer("Аккаунт не найден или выключен.", show_alert=True)
        return
    await state.clear()
    await state.update_data(
        ui_chat_id=c.message.chat.id,
        ui_message_id=c.message.message_id,
        return_callback="tool:menu",
        chat_search_account_id=aid,
        chat_search_account_ids=[aid],
    )
    await state.set_state(Flow.chat_search_query)
    await edit(
        c,
        f"🔎 Поиск Telegram-групп · {account.display_name} · аккаунт #{aid}\n\n"
        "Введите тему или несколько ключевых слов. Например: «Python разработка», "
        "«настольные игры», «дизайн».\n\n"
        "Поиск использует Telegram и несколько поисковых систем. Каждая найденная ссылка "
        "проверяется через Telegram API: каналы, профили, боты и нерабочие ссылки исключаются. "
        "Приватные группы попадут в результаты только если приглашение доступно и Telegram "
        "позволяет проверить его тип.",
        CANCEL,
    )



# Telegram account management tools. Mutations use isolated SQLite snapshots.
@router.callback_query(F.data == "tool:management")
async def management_accounts(c: CallbackQuery):
    if not await check_cb(c): return
    async with SessionFactory() as db:
        accounts = list((await db.scalars(
            select(Account).where(Account.enabled.is_(True)).order_by(Account.id)
        )).all())
    rows = [[(f"👤 {a.display_name[:30]} · #{a.id}", f"tool:management:account:{a.id}")] for a in accounts]
    rows.append([("⬅️ Инструменты", "tool:menu")])
    await edit(c, "🛠 Управление Telegram-аккаунтами\n\nВыберите аккаунт. Все операции выполняются через изолированную копию сессии.", kb(rows))


@router.callback_query(F.data.startswith("tool:management:account:"))
async def management_account_menu(c: CallbackQuery):
    if not await check_cb(c): return
    aid = int(c.data.rsplit(":", 1)[1])
    async with SessionFactory() as db:
        account = await db.get(Account, aid)
    if not account or not account.enabled:
        await c.answer("Аккаунт не найден или выключен.", show_alert=True); return
    await edit(c, f"🛠 Инструменты · {account.display_name} · #{aid}\n\nВыберите действие:", kb([
        [("👤 Профиль", f"tool:mg:profile:{aid}"), ("📇 Контакты", f"tool:mg:contacts:{aid}")],
        [("➕ Добавить контакт", f"tool:mg:addcontact:{aid}"), ("🗑 Удалить контакт", f"tool:mg:delcontact:{aid}")],
        [("📥 Импорт контактов", f"tool:mg:importcontacts:{aid}"), ("📤 Экспорт контактов", f"tool:mg:exportcontacts:{aid}")],
        [("🔎 Информация о чате", f"tool:mg:chatinfo:{aid}")],
        [("🗂 Мои группы и каналы · парсер", f"tool:mg:parsechats:{aid}")],
        [("🔍 Парсер истории чата / канала", f"tool:mg:parsechannel:{aid}")],
        [("➕ Пакетная подписка", f"tool:mg:joinbatch:{aid}"), ("➖ Пакетная отписка", f"tool:mg:leavebatch:{aid}")],
        [("👥 Сбор участников (CSV)", f"tool:mg:parseparticipants:{aid}")],
        [("➕ Вступить в чат", f"tool:mg:join:{aid}"), ("➖ Выйти из чата", f"tool:mg:leave:{aid}")],
        [("❤️ Реакция на публикацию", f"tool:mg:reaction:{aid}")],
        [("🕒 Запланировать реакцию", f"tool:mg:schedreaction:{aid}"), ("📆 Мои реакции", f"tool:mg:reactions:{aid}")],
        [("✏️ Изменить профиль", f"tool:mg:editprofile:{aid}")],
        [("📷 Изменить фото профиля", f"tool:mg:photo:{aid}")],
        [("📨 Разовая рассылка / приглашение", f"tool:mg:campaign:{aid}")],
        [("🗓 Создать расписание", f"tool:mg:schedule:{aid}"), ("📅 Мои расписания", f"tool:mg:schedules:{aid}")],
        [("📋 Очередь и история задач", f"tool:mg:campaigns:{aid}")],
        [("🤖 Автоответчик (только при выключенном ИИ)", f"tool:mg:autoresponder:{aid}")],
        [("🎯 Автоответчики и триггеры", f"trigger:account:{aid}")],
        [("⬅️ К аккаунтам", "tool:management")],
    ]))


async def management_prompt(c: CallbackQuery, state: FSMContext, action: str, aid: int, prompt: str):
    previous = await state.get_data()
    selected_ids = previous.get("management_account_ids") or [aid]
    module = previous.get("module_selection_module")
    await state.clear()
    await state.update_data(
        management_action=action, management_account_id=aid,
        management_account_ids=[int(value) for value in selected_ids],
        module_selection_module=module,
        ui_chat_id=c.message.chat.id, ui_message_id=c.message.message_id,
        return_callback=f"tool:module:{module}" if module else f"tool:management:account:{aid}",
    )
    await state.set_state(Flow.management_input)
    await edit(c, prompt + "\n\nОтмена — кнопкой ниже.", CANCEL)


@router.callback_query(F.data.startswith("tool:mg:"))
async def management_action_start(c: CallbackQuery, state: FSMContext):
    if not await check_cb(c): return
    _, _, action, aid_raw = c.data.split(":")
    aid = int(aid_raw)
    prompts = {
        "addcontact": "Отправьте номер телефона, имя и фамилию через |\nПример: +79990000000 | Иван | Иванов",
        "importcontacts": "Отправьте TXT или CSV (до 1000 контактов) либо вставьте строки формата: телефон | имя | фамилия. Для CSV используйте столбцы phone, first_name, last_name.",
        "delcontact": "Отправьте Telegram user ID контакта, которого нужно удалить.",
        "chatinfo": "Отправьте @username или публичную ссылку t.me/…",
        "parsechats": "Формат: all | 200, groups | 200 или channels | 200. Можно отправить только тип (all/groups/channels) или число. Результат будет отправлен в TXT и CSV.",
        "joinbatch": "Создание задачи подписки. Отправьте now | ссылки или дату YYYY-MM-DD HH:MM UTC | ссылки, по одной на строку. Можно загрузить TXT/CSV. Максимум 500 ссылок; задача попадёт в очередь, её можно приостановить или отменить.",
        "leavebatch": "Создание задачи отписки. Отправьте now | ссылки или дату YYYY-MM-DD HH:MM UTC | ссылки, по одной на строку. Можно загрузить TXT/CSV. Максимум 500 ссылок; задача попадёт в очередь, её можно приостановить или отменить.",
        "joinrepeat": "Повторяющаяся подписка. Первая строка: старт (now или YYYY-MM-DD HH:MM UTC) | интервал в часах (1–8760) | число запусков (1–365) | окончание YYYY-MM-DD HH:MM UTC или -. Далее ссылки, по одной на строку. Можно загрузить TXT/CSV.",
        "leaverepeat": "Повторяющаяся отписка. Первая строка: старт (now или YYYY-MM-DD HH:MM UTC) | интервал в часах (1–8760) | число запусков (1–365) | окончание YYYY-MM-DD HH:MM UTC или -. Далее ссылки, по одной на строку. Можно загрузить TXT/CSV.",
        "parsechannel": "Формат: @чат или all | сообщений на чат (1–500) | дни (0 = все) | ключевые слова через запятую/; или - | комментарии да/нет | задержка между сообщениями (сек.) | задержка между чатами (сек.).\nПример: all | 100 | 7 | впн, VPN, випиэн | да | 2 | 5. Собираются сообщения, публичные ссылки из текста и комментарии, если Telegram даёт к ним доступ. all обрабатывает доступные группы и каналы выбранного аккаунта. TXT и CSV.",
        "parseparticipants": "Формат: @группа | лимит | без ботов | без удалённых | только username | только фото | только Premium | активны за N дней | без scam | исключить заблокированных этим аккаунтом.\nПример: @mygroup | 500 | да | да | нет | да | нет | 7 | да | да. Нужны права администратора. Telegram не раскрывает, кто заблокировал сам аккаунт; фильтр учитывает только собственный список блокировки.",
        "join": "Отправьте @username или публичную ссылку t.me/… для вступления.",
        "leave": "Отправьте @username или публичную ссылку t.me/… для выхода.",
        "reaction": "Отправьте данные через |\nФормат: @username | ID_публикации | 👍",
        "schedreaction": "Формат: @username | ID_публикации | emoji | now либо YYYY-MM-DD HH:MM UTC. Пример: @mychannel | 123 | 🔥 | 2026-11-03 18:00. Это одна реакция на одну выбранную публикацию, без накрутки.",

        "editprofile": "Одинаковые данные всем выбранным аккаунтам: имя | фамилия | BIO | username. Разные данные: отдельная строка для каждого аккаунта в формате ID аккаунта | имя | фамилия | BIO | username. Для пропуска поля укажите -.",
        "autoresponder": "Формат: ВКЛ/ВЫКЛ | ключевые слова через запятую или - для всех сообщений | текст ответа | пауза в секундах.\nПример: ВКЛ | привет, цена, тариф | Привет! Чем помочь? | 60\nРаботает только в личных сообщениях и только пока ИИ выключен именно на этом аккаунте.",
        "campaign": "Формат: broadcast, invite или group_broadcast | now либо YYYY-MM-DD HH:MM UTC | @получатель1,@получатель2. Выберите конкретных получателей; максимум 500 за задачу.",
        "broadcastlimits": "Формат: интервал в секундах | максимум за 24 часа | тихо с HH:MM UTC или - | тихо до HH:MM UTC или -. Пример: 60 | 40 | 23:00 | 08:00.",
        "invitelimits": "Формат: интервал в секундах | максимум за 24 часа | тихо с HH:MM UTC или - | тихо до HH:MM UTC или -. Пример: 120 | 15 | 22:00 | 07:00.",
        "global-broadcast": "Глобальные значения для рассылок: интервал | максимум за 24 часа | тихо с | тихо до (UTC). Пример: 60 | 40 | 23:00 | 08:00.",
        "global-invite": "Глобальные значения для приглашений: интервал | максимум за 24 часа | тихо с | тихо до (UTC). Пример: 120 | 15 | 22:00 | 07:00.",
        "subscriptionlimits": "Лимиты вступления/выхода: интервал в секундах | максимум операций за 24 часа | тихо с | тихо до (UTC). Пример: 10 | 20 | 23:00 | 07:00.",
        "reactionlimits": "Лимиты реакций: интервал в секундах | максимум за 24 часа | тихо с | тихо до (UTC). Пример: 30 | 50 | - | -.",
        "global-subscription": "Глобальные лимиты подписок: интервал | максимум за 24 часа | тихо с | тихо до (UTC). Пример: 10 | 20 | 23:00 | 07:00.",
        "global-reaction": "Глобальные лимиты реакций: интервал | максимум за 24 часа | тихо с | тихо до (UTC). Пример: 30 | 50 | - | -.",
        "schedule": "Создание повторяющейся кампании. Формат: broadcast, invite или group_broadcast | старт now либо YYYY-MM-DD HH:MM UTC | интервал в часах (не менее 24) | число запусков (1–365) | @user1,@user2. Каждый запуск повторно проверяет доступность получателей в Telegram.",
        "limits": "Формат: интервал_секунд | максимум_сообщений_за_24ч | тихо_с | тихо_до (UTC). Пример: 30 | 50 | 23:00 | 08:00. Без тихих часов: 30 | 50 | - | -.",
        "global-limits": "Глобальные значения по умолчанию для аккаунтов без индивидуального переопределения. Формат: интервал_секунд | максимум_сообщений_за_24ч | тихо_с | тихо_до (UTC). Пример: 30 | 50 | 23:00 | 08:00. Без тихих часов: 30 | 50 | - | -.",
    }
    if action == "contactlog":
        selected_ids = [int(value) for value in (await state.get_data()).get("management_account_ids") or [aid]]
        lines = ["📜 Журнал операций с контактами", ""]
        async with SessionFactory() as db:
            for account_id in selected_ids:
                row = await db.get(AppSetting, f"contacts.log.{account_id}")
                lines.append(f"Аккаунт #{account_id}")
                try:
                    entries = json.loads(row.value) if row and row.value else []
                except (TypeError, ValueError):
                    entries = []
                for entry in entries[-20:]:
                    lines.append(f"• {entry.get('at', '—')} · {entry.get('action', 'операция')}: {entry.get('details', '')}")
                if not entries:
                    lines.append("Операций пока нет.")
                lines.append("")
        await edit(c, "\\n".join(lines)[:3900], kb([[("↩️ К модулю", "tool:module:contacts")]]))
        return
    if action == "dedupecontacts":
        selected_ids = [int(value) for value in (await state.get_data()).get("management_account_ids") or [aid]]
        results = []
        for account_id in selected_ids:
            try:
                contacts = await telegram_management.list_contacts(account_id, limit=500)
                seen = set()
                duplicate_ids = []
                for contact in contacts:
                    phone = re.sub(r"[^0-9+]", "", str(contact.get("phone") or ""))
                    if not phone:
                        continue
                    if phone in seen:
                        duplicate_ids.append(int(contact["id"]))
                    else:
                        seen.add(phone)
                details = f"проверено {len(contacts)}, найдено повторных контактов по номеру {len(duplicate_ids)}"
                if duplicate_ids:
                    details += "; ID: " + ", ".join(str(value) for value in duplicate_ids[:30])
                await record_contact_operation(account_id, "duplicate_check", details)
                results.append(f"✅ #{account_id}: {details}. Ничего не удалялось.")
            except Exception as exc:
                results.append(f"❌ #{account_id}: {type(exc).__name__}: {str(exc)[:140]}")
        await edit(c, "\\n".join(results)[:3900], kb([[("↩️ К модулю", "tool:module:contacts")]]))
        return
    if action == "exportcontacts":
        selected_ids = [int(value) for value in (await state.get_data()).get("management_account_ids") or [aid]]
        sent = 0
        for account_id in selected_ids:
            try:
                items = await telegram_management.list_contacts(account_id, limit=500)
                output = io.StringIO()
                writer = csv.writer(output)
                writer.writerow(["telegram_id", "username", "first_name", "last_name", "phone"])
                for item in items:
                    writer.writerow([item["id"], item["username"] or "", item["first_name"], item["last_name"], item["phone"]])
                document = BufferedInputFile(output.getvalue().encode("utf-8-sig"), filename=f"contacts-account-{account_id}.csv")
                await c.message.answer_document(document, caption=f"Экспорт контактов аккаунта #{account_id}: {len(items)} записей")
                await record_contact_operation(account_id, "export", f"экспортировано {len(items)} контактов")
                sent += 1
            except Exception as exc:
                await c.message.answer(f"❌ Аккаунт #{account_id}: {type(exc).__name__}: {str(exc)[:150]}")
        await c.answer(f"Экспортировано файлов: {sent}/{len(selected_ids)}.")
        return
    if action == "resetlimits":
        try:
            await telegram_automation.reset_account_limits(aid)
            await edit(c, "Индивидуальные лимиты удалены. Теперь аккаунт использует глобальные значения по умолчанию.", kb([
                [("↩️ К инструментам аккаунта", f"tool:management:account:{aid}")],
            ]))
        except Exception as exc:
            await edit(c, f"Не удалось сбросить лимиты: {type(exc).__name__}: {str(exc)[:250]}", back_home())
        return
    if action == "reactions":
        selected_ids = [int(value) for value in (await state.get_data()).get("management_account_ids") or [aid]]
        rows = []
        for account_id in selected_ids:
            items = await telegram_automation.list_scheduled_reactions(account_id, limit=20)
            rows.extend([(
                f"#{item['id']} · аккаунт #{account_id} · {item['emoji']} · {item['status']}",
                f"tool:reaction:status:{item['id']}",
            )] for item in items)
        rows.append([("↩️ К модулю", "tool:module:reaction")])
        await edit(c, "📆 Запланированные реакции\n\nВыберите запись." if rows[:-1] else "Запланированных реакций пока нет.", kb(rows))
        return
    if action == "schedules":
        selected_ids = [int(value) for value in (await state.get_data()).get("management_account_ids") or [aid]]
        rows = []
        for account_id in selected_ids:
            items = await telegram_automation.list_schedules(account_id, limit=20)
            rows.extend([(
                f"#{item['id']} · аккаунт #{account_id} · {item['kind']} · {item['status']}",
                f"tool:schedule:status:{item['id']}",
            )] for item in items)
        rows.append([("↩️ К модулю", f"tool:module:{(await state.get_data()).get('module_selection_module') or 'broadcast'}")])
        await edit(c, "🗓 Повторяющиеся расписания\n\nВыберите расписание." if rows[:-1] else "Расписаний пока нет.", kb(rows))
        return
    if action == "campaigns":
        selected_ids = [int(value) for value in (await state.get_data()).get("management_account_ids") or [aid]]
        rows = []
        for account_id in selected_ids:
            items = await telegram_automation.list_campaigns(account_id, limit=20)
            for item in items:
                rows.append([(f"#{item['id']} · аккаунт #{account_id} · {item['kind']} · {item['status']}", f"tool:campaign:status:{item['id']}")])
        rows.append([("↩️ К модулю", f"tool:module:{(await state.get_data()).get('module_selection_module') or 'broadcast'}")])
        await edit(c, "📋 Очередь и история задач\n\nВыберите задачу для просмотра статуса и управления." if rows[:-1] else "Задач пока нет.", kb(rows))
        return
    if action == "profile":
        selected_ids = [int(value) for value in (await state.get_data()).get("management_account_ids") or [aid]]
        lines = ["👤 Профили выбранных аккаунтов", ""]
        for account_id in selected_ids:
            try:
                profile_data = await telegram_management.get_profile(account_id)
                lines.extend([
                    f"Аккаунт #{account_id}",
                    f"Имя: {profile_data['first_name']} {profile_data['last_name']}",
                    f"Username: @{profile_data['username'] or 'не задан'}",
                    f"BIO: {profile_data['about'] or 'не задано'}", "",
                ])
            except Exception as exc:
                lines.append(f"❌ #{account_id}: {type(exc).__name__}: {str(exc)[:180]}")
        await edit(c, "\\n".join(lines)[:3900], kb([[("↩️ К модулю", "tool:module:profile")]]))
        return
    if action == "contacts":
        selected_ids = [int(value) for value in (await state.get_data()).get("management_account_ids") or [aid]]
        lines = ["📇 Контакты выбранных аккаунтов", ""]
        for account_id in selected_ids:
            try:
                items = await telegram_management.list_contacts(account_id, limit=60)
                lines.append(f"Аккаунт #{account_id} · показано {len(items)}")
                lines.extend(
                    f"{i}. {item['first_name']} {item['last_name']} · "
                    f"@{item['username'] or '—'} · ID {item['id']}"
                    for i, item in enumerate(items, 1)
                )
                lines.append("")
            except Exception as exc:
                lines.append(f"❌ #{account_id}: {type(exc).__name__}: {str(exc)[:150]}")
        await edit(c, "\\n".join(lines)[:3900], kb([[("↩️ К модулю", "tool:module:contacts")]]))
        return
    if action in {"broadcastlimits", "invitelimits", "subscriptionlimits", "reactionlimits", "global-broadcast", "global-invite", "global-subscription", "global-reaction"}:
        await state.clear()
        await state.update_data(
            management_action=action, management_account_id=aid,
            ui_chat_id=c.message.chat.id, ui_message_id=c.message.message_id,
            return_callback="tool:management" if action.startswith("global-") else f"tool:management:account:{aid}",
        )
        await state.set_state(Flow.management_input)
        await edit(c, prompts[action], CANCEL)
        return
    if action == "global-limits":
        await state.clear()
        await state.update_data(
            management_action="global-limits", management_account_id=0,
            ui_chat_id=c.message.chat.id, ui_message_id=c.message.message_id,
            return_callback="tool:management",
        )
        await state.set_state(Flow.management_input)
        await edit(c, prompts[action], CANCEL)
        return
    if action in {"photo", "photosequential"}:
        previous = await state.get_data()
        selected_ids = previous.get("management_account_ids") or [aid]
        module = previous.get("module_selection_module")
        await state.clear()
        await state.update_data(
            management_action="photo", management_account_id=aid,
            management_account_ids=[int(value) for value in selected_ids],
            photo_per_account=(action == "photosequential"),
            photo_account_index=0, photo_results=[],
            module_selection_module=module,
            ui_chat_id=c.message.chat.id, ui_message_id=c.message.message_id,
            return_callback=f"tool:module:{module}" if module else f"tool:management:account:{aid}",
        )
        await state.set_state(Flow.management_photo)
        if action == "photosequential":
            await edit(c, f"Отправьте фотографию для аккаунта #{int(selected_ids[0])}. После каждого фото бот попросит следующее. Поддерживается фото или изображение-документ до 10 МБ.", CANCEL)
        else:
            await edit(c, "Отправьте одно фото, которое нужно установить на все выбранные аккаунты. Поддерживается фото или изображение-документ до 10 МБ.", CANCEL)
        return
    if action in {"campaign", "schedule"}:
        mode = "once" if action == "campaign" else "recurring"
        await state.clear()
        await state.update_data(
            campaign_mode=mode, management_account_id=aid,
            ui_chat_id=c.message.chat.id, ui_message_id=c.message.message_id,
            return_callback=f"tool:management:account:{aid}",
        )
        title = "Выберите тип задачи" if mode == "once" else "Выберите тип повторяющейся задачи"
        type_rows = [
            [("📨 Личное сообщение", f"tool:campaign:start:{aid}:broadcast:{mode}")],
            [("🔗 Отправить ссылку-приглашение", f"tool:campaign:start:{aid}:invite:{mode}")],
        ]
        if mode == "once":
            type_rows.append([("👥 Добавить выбранных пользователей в группу", f"tool:campaign:start:{aid}:direct_invite:{mode}")])
        type_rows.extend([
            [("📣 Опубликовать в своей группе", f"tool:campaign:start:{aid}:group_broadcast:{mode}")],
            [("↩️ Назад", f"tool:management:account:{aid}")],
        ])
        await edit(c, title + "\n\nВыберите конкретных получателей. Доступность личных сообщений и требования Stars определяются самим Telegram; прямое добавление дополнительно проверяет приватность и право invite_users.",
            kb(type_rows))
        return
    if action not in prompts:
        await c.answer("Неизвестное действие.", show_alert=True); return
    await management_prompt(c, state, action, aid, prompts[action])


@router.callback_query(F.data.startswith("tool:schedule:list:"))
async def management_schedule_list(c: CallbackQuery):
    if not await check_cb(c):
        return
    aid = int(c.data.rsplit(":", 1)[1])
    items = await telegram_automation.list_schedules(aid)
    rows = [[(f"#{item['id']} · {item['kind']} · {item['status']} · запусков {item['runs_count']}/{item['max_runs']}", f"tool:schedule:status:{item['id']}")] for item in items]
    rows.append([("↩️ К инструментам аккаунта", f"tool:management:account:{aid}")])
    await edit(c, "🗓 Повторяющиеся расписания\n\nВыберите расписание.", kb(rows))


@router.callback_query(F.data.startswith("tool:schedule:status:"))
async def management_schedule_status(c: CallbackQuery):
    if not await check_cb(c):
        return
    schedule_id = int(c.data.rsplit(":", 1)[1])
    async with SessionFactory() as db:
        from app.storage.models import AutomationSchedule
        row = await db.get(AutomationSchedule, schedule_id)
        if row:
            aid = row.account_id
            snapshot = {"id": row.id, "kind": row.kind, "status": row.status,
                        "next_run_at": row.next_run_at.isoformat(), "interval_seconds": row.interval_seconds,
                        "runs_count": row.runs_count, "max_runs": row.max_runs, "last_error": row.last_error}
        else:
            snapshot = None
    if not snapshot:
        await c.answer("Расписание не найдено.", show_alert=True)
        return
    hours = snapshot["interval_seconds"] // 3600
    rows = []
    if snapshot["status"] == "active":
        rows.append([("⏸ Приостановить", f"tool:schedule:change:{schedule_id}:paused")])
    elif snapshot["status"] == "paused":
        rows.append([("▶️ Возобновить", f"tool:schedule:change:{schedule_id}:active")])
    if snapshot["status"] not in {"completed", "cancelled"}:
        rows.append([("⛔ Отменить", f"tool:schedule:change:{schedule_id}:cancelled")])
    rows.append([("↩️ К расписаниям", f"tool:schedule:list:{aid}")])
    detail = (f"🗓 Расписание #{schedule_id}\nТип: {snapshot['kind']}\nСтатус: {snapshot['status']}\n"
              f"Следующий запуск UTC: {snapshot['next_run_at']}\nИнтервал: {hours} ч.\n"
              f"Запусков: {snapshot['runs_count']}/{snapshot['max_runs']}")
    if snapshot["last_error"]:
        detail += f"\nОшибка: {snapshot['last_error'][:500]}"
    await edit(c, detail, kb(rows))


@router.callback_query(F.data.startswith("tool:schedule:change:"))
async def management_schedule_change(c: CallbackQuery):
    if not await check_cb(c):
        return
    _, _, _, schedule_raw, status = c.data.split(":")
    try:
        await telegram_automation.set_schedule_status(int(schedule_raw), status)
        await c.answer("Расписание обновлено.")
        await management_schedule_status(c)
    except Exception as exc:
        await c.answer(str(exc)[:180], show_alert=True)




@router.callback_query(F.data.startswith("tool:campaign:start:"))
async def management_campaign_wizard_start(c: CallbackQuery, state: FSMContext):
    if not await check_cb(c):
        return
    parts = c.data.split(":")
    if len(parts) != 6:
        await c.answer("Некорректный сценарий.", show_alert=True)
        return
    aid, kind, mode = int(parts[3]), parts[4], parts[5]
    if kind not in {"broadcast", "invite", "direct_invite", "group_broadcast"} or mode not in {"once", "recurring"}:
        await c.answer("Неизвестный тип задачи.", show_alert=True)
        return
    async with SessionFactory() as db:
        account = await db.get(Account, aid)
        if not account or not account.enabled:
            await c.answer("Аккаунт не найден или выключен.", show_alert=True)
            return
    previous_data = await state.get_data()
    selected_account_ids = previous_data.get("campaign_account_ids") or [aid]
    selected_account_ids = [int(value) for value in selected_account_ids]
    await state.clear()
    await state.update_data(
        management_action="campaign_time", management_account_id=aid,
        campaign_account_ids=selected_account_ids,
        campaign_kind=kind, campaign_mode=mode,
        campaign_schedule_interval_seconds=0, campaign_schedule_max_runs=0,
        ui_chat_id=c.message.chat.id, ui_message_id=c.message.message_id,
        return_callback=f"tool:module:{previous_data.get('module_selection_module') or 'broadcast'}",
    )
    await state.set_state(Flow.management_input)
    await edit(c,
        f"Шаг 1 из {'5' if mode == 'recurring' else '3'} · Время запуска\n\n"
        "Отправьте now для немедленного запуска или дату и время в UTC: YYYY-MM-DD HH:MM.\n"
        "Пример: 2026-11-05 18:30.",
        CANCEL,
    )


@router.callback_query(F.data.startswith("tool:campaign:status:"))
async def management_campaign_status(c: CallbackQuery):
    if not await check_cb(c):
        return
    task_id = int(c.data.rsplit(":", 1)[1])
    try:
        item = await telegram_automation.campaign_status(task_id)
        counts = ", ".join(f"{key}: {value}" for key, value in item["recipients"].items()) or "нет"
        text_value = (
            f"📨 Задача #{task_id}\nТип: {item['kind']}\nСтатус: {item['status']}\n"
            f"Запуск UTC: {item['scheduled_at']}\nПолучатели: {counts}"
        )
        if item["kind"] == "direct_invite":
            text_value += f"\nЦелевая группа: {item.get('target_group_label') or 'не указана'}\nДоступность добавления определяется настройками приватности Telegram."
        if item["last_error"]:
            text_value += f"\nПоследняя информация: {item['last_error'][:500]}"
        log_lines = [
            f"{entry['peer_label']} · {entry['status']} · попыток: {entry['attempts']}"
            + (f" · {entry['last_error'][:120]}" if entry["last_error"] else "")
            for entry in item.get("delivery_log", [])[:12]
        ]
        if log_lines:
            text_value += "\n\nЖурнал доставок:\n" + "\n".join(log_lines)
        rows = []
        uncertain = [entry for entry in item.get("delivery_log", []) if entry["status"] == "uncertain"]
        for entry in uncertain[:8]:
            rows.append([(f"⚠️ {entry['peer_label'][:22]} · отметить доставленным", f"tool:campaign:resolve:{task_id}:{entry['peer_id']}:sent")])
            rows.append([(f"↻ {entry['peer_label'][:22]} · разрешить повтор", f"tool:campaign:resolve:{task_id}:{entry['peer_id']}:retry")])
        failed_count = int(item["recipients"].get("failed", 0) or 0)
        pending_count = int(item["recipients"].get("pending", 0) or 0)
        if item["status"] in {"completed", "failed", "needs_review"} and (
            failed_count > 0 or (item["status"] == "failed" and pending_count > 0)
        ):
            rows.append([("🔁 Повторить неудачные доставки", f"tool:campaign:retry:{task_id}")])
        if item["status"] in {"queued", "running", "needs_review"}:
            rows.append([("⏸ Приостановить", f"tool:campaign:pause:{task_id}")])
            rows.append([("⛔ Отменить", f"tool:campaign:cancel-task:{task_id}")])
        elif item["status"] == "paused":
            rows.append([("▶️ Возобновить", f"tool:campaign:resume:{task_id}")])
            rows.append([("⛔ Отменить", f"tool:campaign:cancel-task:{task_id}")])
        rows.append([("↩️ К задачам", f"tool:campaign:list:{item['account_id']}")])
        await edit(c, text_value, kb(rows))
    except Exception as exc:
        await edit(c, f"Не удалось получить задачу: {type(exc).__name__}: {str(exc)[:250]}", back_home())


@router.callback_query(F.data.startswith("tool:reaction:status:"))
async def management_reaction_status(c: CallbackQuery):
    if not await check_cb(c):
        return
    reaction_id = int(c.data.rsplit(":", 1)[1])
    async with SessionFactory() as db:
        from app.storage.models import ScheduledReaction
        row = await db.get(ScheduledReaction, reaction_id)
        if row:
            aid = row.account_id
            snapshot = {"id": row.id, "chat_ref": row.chat_ref, "message_id": row.message_id,
                        "emoji": row.emoji, "run_at": row.run_at.isoformat(),
                        "status": row.status, "last_error": row.last_error}
        else:
            snapshot = None
    if not snapshot:
        await c.answer("Реакция не найдена.", show_alert=True)
        return
    rows = []
    if snapshot["status"] == "queued":
        rows.append([("⏸ Приостановить", f"tool:reaction:pause:{reaction_id}")])
        rows.append([("⛔ Отменить", f"tool:reaction:cancel:{reaction_id}")])
    elif snapshot["status"] == "paused":
        rows.append([("▶️ Возобновить", f"tool:reaction:resume:{reaction_id}")])
        rows.append([("⛔ Отменить", f"tool:reaction:cancel:{reaction_id}")])
    elif snapshot["status"] == "uncertain":
        rows.append([("✅ Проверил: реакция есть", f"tool:reaction:resolve:{reaction_id}:sent")])
        rows.append([("↻ Проверил: реакции нет, повторить", f"tool:reaction:resolve:{reaction_id}:retry")])
    rows.append([("↩️ К реакциям", f"tool:mg:reactions:{aid}")])
    detail = (f"❤️ Реакция #{reaction_id}\nЧат: {snapshot['chat_ref']}\nПубликация: {snapshot['message_id']}\n"
              f"Emoji: {snapshot['emoji']}\nВремя UTC: {snapshot['run_at']}\nСтатус: {snapshot['status']}")
    if snapshot["last_error"]:
        detail += f"\nИнформация: {snapshot['last_error'][:500]}"
    await edit(c, detail, kb(rows))


@router.callback_query(F.data.startswith("tool:reaction:pause:"))
async def management_reaction_pause(c: CallbackQuery):
    if not await check_cb(c):
        return
    reaction_id = int(c.data.rsplit(":", 1)[1])
    try:
        await telegram_automation.set_scheduled_reaction_status(reaction_id, "paused")
        await management_reaction_status(c)
    except Exception as exc:
        await c.answer(str(exc)[:180], show_alert=True)


@router.callback_query(F.data.startswith("tool:reaction:resume:"))
async def management_reaction_resume(c: CallbackQuery):
    if not await check_cb(c):
        return
    reaction_id = int(c.data.rsplit(":", 1)[1])
    try:
        await telegram_automation.set_scheduled_reaction_status(reaction_id, "queued")
        await management_reaction_status(c)
    except Exception as exc:
        await c.answer(str(exc)[:180], show_alert=True)


@router.callback_query(F.data.startswith("tool:reaction:cancel:"))
async def management_reaction_cancel(c: CallbackQuery):
    if not await check_cb(c):
        return
    reaction_id = int(c.data.rsplit(":", 1)[1])
    try:
        await telegram_automation.cancel_scheduled_reaction(reaction_id)
        await c.answer("Реакция отменена.")
        await management_reaction_status(c)
    except Exception as exc:
        await c.answer(str(exc)[:180], show_alert=True)




@router.callback_query(F.data.startswith("tool:reaction:resolve:"))
async def management_reaction_resolve(c: CallbackQuery):
    if not await check_cb(c):
        return
    _, _, _, reaction_raw, decision = c.data.split(":")
    try:
        await telegram_automation.resolve_scheduled_reaction(
            int(reaction_raw), delivered=(decision == "sent")
        )
        await c.answer("Результат реакции обновлён.")
        await management_reaction_status(c)
    except Exception as exc:
        await c.answer(str(exc)[:180], show_alert=True)


@router.callback_query(F.data.startswith("tool:campaign:resolve:"))
async def management_campaign_resolve(c: CallbackQuery):
    if not await check_cb(c):
        return
    _, _, _, task_raw, peer_raw, decision = c.data.split(":")
    try:
        await telegram_automation.resolve_uncertain_recipient(
            int(task_raw), int(peer_raw), delivered=(decision == "sent")
        )
        await c.answer("Результат доставки обновлён.")
        await management_campaign_status(c)
    except Exception as exc:
        await c.answer(str(exc)[:180], show_alert=True)


@router.callback_query(F.data.startswith("tool:campaign:pause:"))
@router.callback_query(F.data.startswith("tool:campaign:resume:"))
@router.callback_query(F.data.startswith("tool:campaign:cancel-task:"))
async def management_campaign_change(c: CallbackQuery):
    if not await check_cb(c):
        return
    task_id = int(c.data.rsplit(":", 1)[1])
    status = {"pause": "paused", "resume": "queued", "cancel-task": "cancelled"}[c.data.split(":")[2]]
    try:
        await telegram_automation.set_campaign_status(task_id, status)
        await management_campaign_status(c)
    except Exception as exc:
        await c.answer(f"Не удалось изменить задачу: {str(exc)[:180]}", show_alert=True)


@router.callback_query(F.data.startswith("tool:campaign:retry:"))
async def management_campaign_retry(c: CallbackQuery):
    if not await check_cb(c):
        return
    task_id = int(c.data.rsplit(":", 1)[1])
    try:
        count = await telegram_automation.retry_failed_campaign(task_id)
        await c.answer(f"В очередь возвращено: {count}")
        await management_campaign_status(c)
    except Exception as exc:
        await c.answer(f"Не удалось повторить: {str(exc)[:180]}", show_alert=True)


@router.callback_query(F.data.startswith("tool:campaign:list:"))
async def management_campaign_list(c: CallbackQuery):
    if not await check_cb(c):
        return
    aid = int(c.data.rsplit(":", 1)[1])
    await management_campaigns_render(c, aid)


async def management_campaigns_render(c: CallbackQuery, aid: int):
    items = await telegram_automation.list_campaigns(aid, limit=12)
    rows = [[(f"#{item['id']} · {item['kind']} · {item['status']}", f"tool:campaign:status:{item['id']}")] for item in items]
    rows.append([("↩️ К инструментам аккаунта", f"tool:management:account:{aid}")])
    await edit(c, "📋 Очередь и история задач\n\nВыберите задачу.", kb(rows))


@router.message(Flow.management_input, F.document)
async def management_input_document_received(m: Message, state: FSMContext):
    if not await check_msg(m) or not await private(m):
        return
    data = await state.get_data()
    action = data.get("management_action")
    if action not in {"campaign_recipients", "importcontacts", "joinbatch", "leavebatch", "joinrepeat", "leaverepeat"}:
        await m.answer("В этом шаге нужен текстовый ввод. TXT/CSV принимаются для получателей рассылки, подписок и импорта контактов.")
        return
    document = m.document
    if not document or document.file_size and document.file_size > 1_000_000:
        await m.answer("Файл не должен превышать 1 МБ.")
        return
    filename = (document.file_name or "").lower()
    if not filename.endswith((".txt", ".csv")):
        await m.answer("Поддерживаются только TXT и CSV.")
        return
    try:
        remote = await m.bot.get_file(document.file_id)
        stream = await m.bot.download_file(remote.file_path)
        raw_bytes = stream.read()
        content = raw_bytes.decode("utf-8-sig")
        if not content.strip():
            raise ValueError("Файл пуст.")
        if action == "campaign_recipients" and filename.endswith(".csv"):
            rows = list(csv.reader(io.StringIO(content)))
            if not rows:
                raise ValueError("CSV не содержит получателей.")
            header = [str(value).strip().lower() for value in rows[0]]
            column_name = next((name for name in ("username", "user", "peer_ref", "recipient") if name in header), "")
            column = header.index(column_name) if column_name else 0
            if column_name:
                rows = rows[1:]
            content = "\\n".join(
                str(row[column]).strip() for row in rows
                if len(row) > column and str(row[column]).strip()
            )
        copied = m.model_copy(update={"text": content, "document": None})
        await management_input_received(copied, state)
    except UnicodeDecodeError:
        await m.answer("Не удалось прочитать файл. Сохраните TXT/CSV в UTF-8 и отправьте повторно.")
    except Exception as exc:
        logging.exception("Could not process management input file")
        await m.answer(f"Не удалось обработать файл: {type(exc).__name__}: {str(exc)[:180]}")


@router.message(Flow.management_input)
async def management_input_received(m: Message, state: FSMContext):
    if not await check_msg(m) or not await private(m): return
    data = await state.get_data()
    action = data.get("management_action")
    aid = int(data.get("management_account_id") or 0)
    raw = (m.text or "").strip()
    await delete_msg(m)
    try:
        if action == "autoresponder":
            parts = [x.strip() for x in raw.split("|", 3)]
            if len(parts) != 4:
                raise ValueError("Формат: ВКЛ/ВЫКЛ | ключевые слова или - | текст ответа | пауза в секундах.")
            enabled = parts[0].lower() in {"вкл", "включить", "on", "true", "1"}
            if parts[0].lower() not in {"вкл", "включить", "on", "true", "1", "выкл", "выключить", "off", "false", "0"}:
                raise ValueError("Первое поле должно быть ВКЛ или ВЫКЛ.")
            keywords = "" if parts[1] in {"-", "все", "all"} else parts[1]
            response = parts[2].strip()
            cooldown = int(parts[3])
            if enabled and not response:
                raise ValueError("Для включённого автоответчика укажите текст ответа.")
            if not 0 <= cooldown <= 86400:
                raise ValueError("Пауза должна быть от 0 до 86400 секунд.")
            account_ids = [int(value) for value in (data.get("management_account_ids") or [aid])]
            async with SessionFactory() as db:
                for account_id in account_ids:
                    settings = {
                        f"autoresponder.{account_id}.enabled": "true" if enabled else "false",
                        f"autoresponder.{account_id}.keywords": keywords,
                        f"autoresponder.{account_id}.response": response,
                        f"autoresponder.{account_id}.cooldown_seconds": str(cooldown),
                    }
                    for key, value in settings.items():
                        row = await db.get(AppSetting, key)
                        if row:
                            row.value = value
                        else:
                            db.add(AppSetting(key=key, value=value))
                await db.commit()
            message = (
                f"Автоответчик включён для {len(account_ids)} аккаунтов. Он отвечает только в личных чатах и только при выключенном ИИ каждого аккаунта."
                if enabled else f"Автоответчик выключен для {len(account_ids)} аккаунтов."
            )
        elif action and action.startswith("policy-field-"):
            _, module, field = action.split("-", 3)
            if field not in POLICY_FIELDS:
                raise ValueError("Неизвестный параметр настроек.")
            _label, unit, minimum, maximum = POLICY_FIELDS[field]
            if not raw.isdigit() or not minimum <= int(raw) <= maximum:
                raise ValueError(f"Введите целое число от {minimum} до {maximum}.")
            await telegram_automation.set_module_policy(module, **{field: int(raw)})
            message = f"Настройка модуля сохранена: {int(raw)} {unit}."
        elif action in {"broadcastlimits", "invitelimits", "subscriptionlimits", "reactionlimits", "global-broadcast", "global-invite", "global-subscription", "global-reaction"}:
            parts = [x.strip() for x in raw.split("|")]
            if len(parts) != 4:
                raise ValueError("Формат: интервал | максимум за 24 часа | тихо с | тихо до. Для отключения тихих часов укажите - в обоих последних полях.")
            operation = (
                "invite" if "invite" in action else
                "subscription" if "subscription" in action else
                "reaction" if "reaction" in action else "broadcast"
            )
            scoped_account = None if action.startswith("global-") else aid
            await telegram_automation.set_operation_limits(
                operation, interval_seconds=int(parts[0]), daily_limit=int(parts[1]),
                account_id=scoped_account, quiet_start_utc=parts[2], quiet_end_utc=parts[3],
            )
            operation_label = {"invite": "приглашений", "broadcast": "рассылок", "subscription": "подписок", "reaction": "реакций"}[operation]
            message = f"Отдельные лимиты {'глобально' if scoped_account is None else f'аккаунта #{aid}'} для {operation_label} сохранены."
        elif action == "global-limits":
            parts = [x.strip() for x in raw.split("|")]
            if len(parts) != 4:
                raise ValueError("Формат: интервал | лимит за 24 часа | тихо с | тихо до.")
            await telegram_automation.set_global_automation_limits(
                interval_seconds=int(parts[0]), daily_limit=int(parts[1]),
                quiet_start_utc=parts[2], quiet_end_utc=parts[3],
            )
            message = "Глобальные лимиты и тихие часы по умолчанию сохранены. Индивидуальные настройки аккаунтов не изменены."
        elif action == "limits":
            parts = [x.strip() for x in raw.split("|")]
            if len(parts) != 4:
                raise ValueError("Формат: интервал | лимит за 24 часа | тихо с | тихо до.")
            await telegram_automation.set_account_limits(
                aid, interval_seconds=int(parts[0]), daily_limit=int(parts[1]),
                quiet_start_utc=parts[2], quiet_end_utc=parts[3],
            )
            message = "Лимиты и тихие часы аккаунта сохранены."
        elif action == "campaign_time":
            scheduled = datetime.now(timezone.utc)
            if raw.lower() != "now":
                try:
                    scheduled = datetime.strptime(raw, "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
                except ValueError as exc:
                    raise ValueError("Отправьте now или дату в формате YYYY-MM-DD HH:MM UTC.") from exc
            await state.update_data(campaign_scheduled_at=scheduled.isoformat())
            if data.get("campaign_mode") == "recurring":
                await state.update_data(management_action="campaign_interval")
                await prompt_message(m, state, "Шаг 2 из 5 · Интервал\n\nЧерез сколько часов повторять отправку? Минимум 1 час. Частота также ограничивается дневным лимитом аккаунта и FloodWait Telegram. Пример: 24.")
            else:
                if data.get("campaign_kind") == "direct_invite":
                    await state.update_data(management_action="campaign_target_group")
                    await prompt_message(m, state, "Шаг 2 из 3 · Целевая группа\n\nУкажите общую группу через @username/peer ID либо отдельные строки для каждого аккаунта: ID аккаунта | @username группы. Каждый аккаунт должен быть администратором с правом приглашать участников.")
                else:
                    await state.update_data(management_action="campaign_recipients")
                    await prompt_message(m, state, "Шаг 2 из 3 · Получатели\n\nОтправьте @username или ссылку каждого получателя через запятую или с новой строки. Укажите конкретных получателей через @username. Для публикации в группе укажите группы, где аккаунт администратор (можно peer ID из списка групп).")
            return
        elif action == "campaign_interval":
            if not raw.isdigit() or not 1 <= int(raw) <= 8760:
                raise ValueError("Интервал должен быть от 1 до 8760 часов.")
            await state.update_data(
                campaign_schedule_interval_seconds=int(raw) * 3600,
                management_action="campaign_runs",
            )
            await prompt_message(m, state, "Шаг 3 из 5 · Количество запусков\n\nУкажите число повторений от 1 до 365.")
            return
        elif action == "campaign_runs":
            if not raw.isdigit() or not 1 <= int(raw) <= 365:
                raise ValueError("Количество запусков должно быть от 1 до 365.")
            await state.update_data(
                campaign_schedule_max_runs=int(raw),
                management_action="campaign_end_at",
            )
            await prompt_message(m, state, "Окончание расписания\n\nУкажите дату и время окончания в UTC: YYYY-MM-DD HH:MM, либо - без даты окончания. Расписание также завершится после заданного числа запусков.")
            return
        elif action == "campaign_end_at":
            end_at = None
            if raw not in {"-", "нет", "без даты"}:
                try:
                    end_at = datetime.strptime(raw, "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
                except ValueError as exc:
                    raise ValueError("Укажите окончание как YYYY-MM-DD HH:MM UTC или -.") from exc
            await state.update_data(
                campaign_schedule_ends_at=end_at.isoformat() if end_at else "",
                management_action="campaign_target_group" if data.get("campaign_kind") == "direct_invite" else "campaign_recipients",
            )
            if data.get("campaign_kind") == "direct_invite":
                await prompt_message(m, state, "Целевая группа\n\nУкажите общую группу через @username/peer ID либо отдельные строки для каждого аккаунта: ID аккаунта | @username группы. Каждый аккаунт должен быть администратором с правом приглашать участников.")
            else:
                await prompt_message(m, state, "Получатели\n\nОтправьте @username или ссылку каждого получателя через запятую или с новой строки. Укажите конкретных получателей через @username.")
            return
        elif action == "campaign_target_group":
            selected_ids = [int(value) for value in (data.get("campaign_account_ids") or [aid])]
            group_inputs = {}
            lines = [line.strip() for line in raw.splitlines() if line.strip()]
            if len(lines) > 1 or (lines and "|" in lines[0]):
                for line in lines:
                    fields = [value.strip() for value in line.split("|", 1)]
                    if len(fields) != 2 or not fields[0].isdigit() or not fields[1]:
                        raise ValueError("Для разных групп используйте строки: ID аккаунта | @username группы.")
                    account_id = int(fields[0])
                    if account_id not in selected_ids or account_id in group_inputs:
                        raise ValueError("Укажите каждый выбранный аккаунт один раз.")
                    group_inputs[account_id] = fields[1]
                if set(group_inputs) != set(selected_ids):
                    raise ValueError("Укажите целевую группу для каждого выбранного аккаунта.")
            else:
                if not raw:
                    raise ValueError("Укажите @username группы или peer ID.")
                group_inputs = {account_id: raw for account_id in selected_ids}
            target_groups = {}
            for account_id in selected_ids:
                group = await telegram_management.resolve_group_for_direct_invite(account_id, group_inputs[account_id])
                if not group.get("is_megagroup"):
                    raise ValueError(f"Аккаунт #{account_id}: прямые приглашения доступны только в супергруппы.")
                target_groups[str(account_id)] = {
                    "target_group_id": int(group["peer_id"]),
                    "target_group_ref": str(group["peer_ref"]),
                    "target_group_label": str(group["peer_label"]),
                }
            await state.update_data(
                campaign_target_groups=target_groups,
                campaign_target_group_id=target_groups[str(aid)]["target_group_id"],
                campaign_target_group_ref=target_groups[str(aid)]["target_group_ref"],
                campaign_target_group_label=target_groups[str(aid)]["target_group_label"],
                management_action="campaign_recipients",
            )
            step = "5 из 5" if data.get("campaign_mode") == "recurring" else "3 из 3"
            await prompt_message(m, state, f"Шаг {step} · Получатели\n\nОтправьте @username выбранных пользователей через запятую или с новой строки. Для прямого добавления указывайте пользователей явно; ALL не используется.")
            return
        elif action == "campaign_recipients":
            kind = str(data.get("campaign_kind") or "")
            if raw.strip().lower() in {"all", "все"}:
                raise ValueError("Укажите конкретных получателей через @username. Массовый выбор всех диалогов не запускается автоматически.")
            refs = [item.strip() for item in raw.replace(",", "\n").splitlines() if item.strip()]
            if not refs:
                raise ValueError("Добавьте хотя бы одного получателя через @username.")
            peers = (
                await telegram_management.resolve_groups(aid, refs)
                if kind == "group_broadcast"
                else await telegram_management.resolve_peers(aid, refs)
            )
            if kind == "direct_invite":
                payload = {
                    "target_group_id": int(data["campaign_target_group_id"]),
                    "target_group_ref": str(data.get("campaign_target_group_ref") or ""),
                    "target_group_label": str(data.get("campaign_target_group_label") or ""),
                }
                await state.update_data(
                    campaign_recipients=[peer["peer_id"] for peer in peers],
                    campaign_recipient_labels=[peer["peer_label"] for peer in peers],
                    campaign_recipient_refs=[peer["peer_ref"] for peer in peers],
                    campaign_payload=payload,
                    management_action="campaign_content",
                )
                await state.set_state(Flow.management_campaign_confirm)
                preview = (
                    f"👥 Предпросмотр прямого приглашения\n\n"
                    f"Группа: {payload['target_group_label']}\n"
                    f"Получателей: {len(peers)}\n"
                    f"Пользователи: {', '.join(peer['peer_label'] for peer in peers[:12])}"
                    + (" и другие" if len(peers) > 12 else "")
                    + "\n\nTelegram проверит настройки приватности каждого пользователя и право аккаунта приглашать участников. Если добавление запрещено, отказ будет записан в журнале, а очередь продолжится."
                )
                keyboard = kb([
                    [("✅ Подтвердить приглашения", "tool:campaign:confirm")],
                    [("❌ Отменить", "tool:campaign:cancel")],
                ])
                anchor = await state.get_data()
                if not await edit_anchor(m.bot, anchor, preview, keyboard):
                    await m.answer(preview, reply_markup=keyboard)
                return
            await state.update_data(
                campaign_recipients=[peer["peer_id"] for peer in peers],
                campaign_recipient_labels=[peer["peer_label"] for peer in peers],
                campaign_recipient_refs=[peer.get("peer_ref", "") for peer in peers],
                management_action="campaign_content",
            )
            await state.set_state(Flow.management_campaign_content)
            step = "5 из 5" if data.get("campaign_mode") == "recurring" else "3 из 3"
            await prompt_message(m, state, f"Шаг {step} · Сообщение или файл\n\nПолучателей проверено: {len(peers)}. Отправьте текст или прикрепите файл. Перед постановкой в очередь появится предпросмотр.")
            return
        elif action in {"campaign_header", "schedule_header"}:
            parts = [x.strip() for x in raw.split("|", 4 if action == "schedule_header" else 2)]
            expected = 5 if action == "schedule_header" else 3
            if len(parts) != expected:
                raise ValueError("Формат рассылки: тип | время UTC или now | @получатели. Для расписания: тип | старт | интервал часов | число запусков | @получатели.")
            kind = parts[0].lower()
            if kind not in {"broadcast", "invite", "direct_invite", "group_broadcast"}:
                raise ValueError("Тип задачи: broadcast, invite, direct_invite или group_broadcast.")
            scheduled = datetime.now(timezone.utc)
            if parts[1].lower() != "now":
                try:
                    scheduled = datetime.strptime(parts[1], "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
                except ValueError as exc:
                    raise ValueError("Время должно быть YYYY-MM-DD HH:MM UTC.") from exc
            interval_seconds = 0
            max_runs = 0
            refs_raw = parts[2]
            if action == "schedule_header":
                if not parts[2].isdigit() or not parts[3].isdigit():
                    raise ValueError("Интервал и число запусков должны быть целыми числами.")
                interval_seconds = int(parts[2]) * 3600
                max_runs = int(parts[3])
                if interval_seconds < 86400 or not 1 <= max_runs <= 365:
                    raise ValueError("Интервал — минимум 24 часа; число запусков — от 1 до 365.")
                refs_raw = parts[4]
            refs = [item.strip() for item in refs_raw.split(",") if item.strip()]
            peers = (
                await telegram_management.resolve_groups(aid, refs)
                if kind == "group_broadcast"
                else await telegram_management.resolve_peers(aid, refs)
            )
            await state.update_data(
                management_action="campaign_content", management_account_id=aid,
                campaign_kind=kind, campaign_scheduled_at=scheduled.isoformat(),
                campaign_recipients=[peer["peer_id"] for peer in peers],
                campaign_recipient_labels=[peer["peer_label"] for peer in peers],
                campaign_recipient_refs=[peer.get("peer_ref", "") for peer in peers],
                campaign_schedule_interval_seconds=interval_seconds,
                campaign_schedule_max_runs=max_runs,
            )
            await state.set_state(Flow.management_campaign_content)
            await prompt_message(m, state, f"Получателей найдено: {len(peers)}. Следующим сообщением отправьте текст или прикрепите документ.\nПеред запуском будет предпросмотр. Для invite добавьте ссылку t.me.")
            return
        if action == "importcontacts":
            contacts = []
            if "," in raw.splitlines()[0] and "|" not in raw:
                reader = csv.DictReader(io.StringIO(raw))
                for index, row in enumerate(reader, 2):
                    normalized = {str(key or "").strip().lower(): str(value or "").strip() for key, value in row.items()}
                    phone = normalized.get("phone") or normalized.get("phone_number") or normalized.get("номер")
                    first_name = normalized.get("first_name") or normalized.get("firstname") or normalized.get("имя")
                    last_name = normalized.get("last_name") or normalized.get("lastname") or normalized.get("фамилия") or ""
                    if not phone or not first_name:
                        raise ValueError(f"Строка {index}: CSV должен содержать phone и first_name.")
                    contacts.append({"phone": phone, "first_name": first_name, "last_name": last_name})
            else:
                for index, line in enumerate(raw.splitlines(), 1):
                    if not line.strip():
                        continue
                    parts = [item.strip() for item in line.split("|", 2)]
                    if len(parts) not in {2, 3}:
                        raise ValueError(f"Строка {index}: формат телефон | имя | фамилия.")
                    contacts.append({
                        "phone": parts[0], "first_name": parts[1],
                        "last_name": parts[2] if len(parts) == 3 else "",
                    })
            if not contacts or len(contacts) > 1000:
                raise ValueError("В файле должно быть от 1 до 1000 контактов.")
            deduplicated = []
            seen_phones = set()
            for contact in contacts:
                normalized_phone = re.sub(r"[^0-9+]", "", contact["phone"])
                if not normalized_phone or normalized_phone in seen_phones:
                    continue
                seen_phones.add(normalized_phone)
                deduplicated.append({**contact, "phone": normalized_phone})
            contacts = deduplicated
            if not contacts:
                raise ValueError("После удаления дубликатов не осталось корректных номеров.")
            account_ids = [int(value) for value in (data.get("management_account_ids") or [aid])]
            results = []
            for account_id in account_ids:
                try:
                    existing = await telegram_management.list_contacts(account_id, limit=500)
                    existing_phones = {
                        re.sub(r"[^0-9+]", "", str(item.get("phone") or ""))
                        for item in existing if item.get("phone")
                    }
                    account_contacts = [
                        item for item in contacts
                        if re.sub(r"[^0-9+]", "", item["phone"]) not in existing_phones
                    ]
                    imported = users = retry_contacts = 0
                    for offset in range(0, len(account_contacts), 100):
                        result = await telegram_management.import_contacts(account_id, account_contacts[offset:offset + 100])
                        imported += result["imported"]
                        users += result["users"]
                        retry_contacts += result["retry_contacts"]
                    skipped = len(contacts) - len(account_contacts)
                    details = f"импортировано {imported}, найдено {users}, повторно {retry_contacts}, уже были {skipped}"
                    await record_contact_operation(account_id, "import", details)
                    results.append(f"✅ #{account_id}: {details}.")
                except Exception as exc:
                    results.append(f"❌ #{account_id}: {type(exc).__name__}: {str(exc)[:120]}")
            message = "\\n".join(results)
        elif action == "addcontact":
            parts = [x.strip() for x in raw.split("|")]
            if len(parts) not in {2, 3}: raise ValueError("Формат: телефон | имя | фамилия.")
            account_ids = [int(value) for value in (data.get("management_account_ids") or [aid])]
            results = []
            for account_id in account_ids:
                try:
                    count = await telegram_management.add_contact(
                        account_id, phone=parts[0], first_name=parts[1], last_name=parts[2] if len(parts) == 3 else ""
                    )
                    await record_contact_operation(account_id, "add", f"номер {parts[0]} · добавлено {count}")
                    results.append(f"✅ #{account_id}: Telegram подтвердил добавление ({count}).")
                except Exception as exc:
                    results.append(f"❌ #{account_id}: {type(exc).__name__}: {str(exc)[:120]}")
            message = "\\n".join(results)
        elif action == "delcontact":
            account_ids = [int(value) for value in (data.get("management_account_ids") or [aid])]
            results = []
            for account_id in account_ids:
                try:
                    await telegram_management.delete_contact(account_id, int(raw))
                    await record_contact_operation(account_id, "delete", f"Telegram ID {int(raw)}")
                    results.append(f"✅ #{account_id}: контакт удалён.")
                except Exception as exc:
                    results.append(f"❌ #{account_id}: {type(exc).__name__}: {str(exc)[:120]}")
            message = "\\n".join(results)
        elif action == "parsechats":
            parts = [item.strip().lower() for item in raw.split("|") if item.strip()]
            kind = "all"
            limit = 200
            if len(parts) == 1:
                if parts[0] in {"all", "groups", "channels"}:
                    kind = parts[0]
                else:
                    limit = int(parts[0])
            elif len(parts) == 2:
                kind = parts[0]
                limit = int(parts[1])
            else:
                raise ValueError("Формат: all/groups/channels | лимит.")
            if kind not in {"all", "groups", "channels"}:
                raise ValueError("Тип должен быть all, groups или channels.")
            items = await telegram_management.list_account_dialogs(aid, limit=limit, kind=kind)
            output = io.StringIO()
            writer = csv.writer(output)
            writer.writerow(["telegram_peer_id", "entity_id", "type", "title", "username", "participants_count", "unread_count"])
            for item in items:
                writer.writerow([item["peer_id"], item["id"], item["kind"], item["title"], item["username"], item["participants_count"] or "", item["unread_count"]])
            txt_lines = [f"ГРУППЫ И КАНАЛЫ АККАУНТА #{aid}", f"Всего: {len(items)}", ""]
            for index, item in enumerate(items, 1):
                txt_lines.extend([
                    f"{index}. {item['title']}",
                    f"   Тип: {item['kind']}",
                    f"   ID: {item['id']} · Peer ID: {item['peer_id']}",
                    f"   Username: @{item['username']}" if item["username"] else "   Username: —",
                    f"   Участников: {item['participants_count'] if item['participants_count'] is not None else 'нет данных'}",
                    f"   Непрочитано: {item['unread_count']}",
                    "",
                ])
            await m.answer_document(BufferedInputFile("\n".join(txt_lines).encode("utf-8"), filename=f"dialogs-account-{aid}.txt"), caption=f"Группы и каналы аккаунта #{aid}: {len(items)} записей.")
            document = BufferedInputFile(output.getvalue().encode("utf-8-sig"), filename=f"dialogs-account-{aid}.csv")
            await m.answer_document(document, caption="Дополнительно: таблица CSV.")
            message = f"Сформированы TXT и CSV. Найдено групп и каналов: {len(items)}."
        elif action in {"joinbatch", "leavebatch", "joinrepeat", "leaverepeat"}:
            repeating = action in {"joinrepeat", "leaverepeat"}
            operation = "join" if action in {"joinbatch", "joinrepeat"} else "leave"
            account_ids = [int(value) for value in (data.get("management_account_ids") or [aid])]
            if repeating:
                lines = [line.strip() for line in raw.splitlines() if line.strip()]
                if len(lines) < 2:
                    raise ValueError("Нужна первая строка с параметрами расписания и хотя бы одна ссылка.")
                header = [value.strip() for value in lines[0].split("|")]
                if len(header) != 4 or not header[1].isdigit() or not header[2].isdigit():
                    raise ValueError("Первая строка: старт | интервал часов | число запусков | окончание или -.")
                starts_at = datetime.now(timezone.utc) if header[0].lower() == "now" else datetime.strptime(header[0], "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
                interval_seconds = int(header[1]) * 3600
                max_runs = int(header[2])
                ends_at = None if header[3] in {"-", "нет"} else datetime.strptime(header[3], "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
                links = list(dict.fromkeys(line for line in lines[1:] if line))
                if not 1 <= interval_seconds <= 31536000 or not 1 <= max_runs <= 365:
                    raise ValueError("Интервал должен быть 1–8760 часов, число запусков — 1–365.")
                if not links or len(links) > 500:
                    raise ValueError("Укажите от 1 до 500 ссылок.")
                recipients = {}
                for link in links:
                    peer_id = int.from_bytes(hashlib.sha256(link.casefold().encode("utf-8")).digest()[:8], "big") & ((1 << 63) - 1)
                    recipients[peer_id or 1] = link
                created, failed = [], []
                for account_id in account_ids:
                    try:
                        task_id = await telegram_automation.create_schedule(
                            account_id,
                            kind="subscription_join" if operation == "join" else "subscription_leave",
                            recipients=list(recipients),
                            payload={"action": operation},
                            starts_at=starts_at, ends_at=ends_at,
                            interval_seconds=interval_seconds, max_runs=max_runs,
                            recipient_refs=recipients, recipient_labels=recipients,
                        )
                        created.append((account_id, task_id))
                    except Exception as exc:
                        failed.append((account_id, f"{type(exc).__name__}: {str(exc)[:160]}"))
                message = "\\n".join(
                    [f"✅ Аккаунт #{account_id}: расписание #{task_id}, ссылок: {len(links)}" for account_id, task_id in created]
                    + [f"❌ Аккаунт #{account_id}: {error}" for account_id, error in failed]
                    + [f"Старт UTC: {starts_at.isoformat()} · интервал: {interval_seconds // 3600} ч. · запусков: {max_runs}."]
                )
            else:
                schedule_at = datetime.now(timezone.utc)
                link_block = raw
                if "|" in raw:
                    time_part, link_block = raw.split("|", 1)
                    time_part = time_part.strip()
                    if time_part.lower() != "now":
                        try:
                            schedule_at = datetime.strptime(time_part, "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
                        except ValueError as exc:
                            raise ValueError("Время должно быть now или YYYY-MM-DD HH:MM UTC.") from exc
                links = [line.strip() for line in link_block.replace(",", "\\n").splitlines() if line.strip()]
                created, failed = [], []
                for account_id in account_ids:
                    try:
                        task_id = await telegram_automation.create_subscription_task(
                            account_id, links, action=operation, scheduled_at=schedule_at,
                        )
                        created.append((account_id, task_id))
                    except Exception as exc:
                        failed.append((account_id, f"{type(exc).__name__}: {str(exc)[:160]}"))
                message = "\\n".join(
                    [f"✅ Аккаунт #{account_id}: задача #{task_id}, ссылок: {len(set(links))}" for account_id, task_id in created]
                    + [f"❌ Аккаунт #{account_id}: {error}" for account_id, error in failed]
                    + [f"Старт UTC: {schedule_at.isoformat()}. Очередь и журнал — в этом же модуле."]
                )
        elif action == "chatinfo":
            item = await telegram_management.get_chat_overview(aid, raw)
            message = (
                f"🔎 {item.title}\nID: {item.chat_id}\n"
                f"Username: @{item.username or '—'}\n"
                f"Участников: {item.participants_count if item.participants_count is not None else 'нет данных'}\n"
                f"Тип: {'канал' if item.is_channel else 'группа' if item.is_group else 'чат/другой'}"
            )
        elif action == "parsechannel":
            parts = [x.strip() for x in raw.split("|")]
            if not 1 <= len(parts) <= 7:
                raise ValueError("Формат: @чат или all | сообщений на чат | дни | ключевые слова | комментарии да/нет | задержка сообщений | задержка чатов.")
            target = parts[0]
            limit = int(parts[1]) if len(parts) > 1 and parts[1] else 20
            days = int(parts[2]) if len(parts) > 2 and parts[2] else 0
            keywords = [] if len(parts) < 4 or parts[3] in {"", "-"} else [x.strip() for x in re.split(r"[,;]", parts[3]) if x.strip()]
            include_comments = len(parts) > 4 and parts[4].lower() in {"да", "yes", "true", "1", "on"}
            message_delay = int(parts[5]) if len(parts) > 5 and parts[5] else 0
            chat_delay = int(parts[6]) if len(parts) > 6 and parts[6] else 0
            if target.lower() in {"all", "все", "все чаты"}:
                account_result = await telegram_management.parse_account_history(
                    aid, limit_per_chat=limit, days=days, keywords=keywords,
                    include_comments=include_comments,
                    message_delay_seconds=message_delay,
                    chat_delay_seconds=chat_delay,
                )
                results = account_result["results"]
                errors = account_result["errors"]
                total_messages = account_result["messages_count"]
                scanned_chats = account_result["chats_scanned"]
            else:
                result = await telegram_management.parse_chat_history(
                    aid, target, limit=limit, days=days, keywords=keywords,
                    include_comments=include_comments,
                    message_delay_seconds=message_delay,
                )
                results = [result]
                errors = []
                total_messages = len(result["posts"])
                scanned_chats = 1
            output = io.StringIO()
            writer = csv.writer(output)
            writer.writerow(["chat_title", "chat_id", "message_id", "date", "views", "forwards", "replies", "reactions", "has_media", "text", "links", "comments"])
            txt_lines = [
                f"ИСТОРИЯ АККАУНТА #{aid}",
                f"Чатов обработано: {len(results)} из {scanned_chats}",
                f"Сообщений выгружено: {total_messages}",
                f"Фильтры: дней={days or 'все'} · ключевые слова={', '.join(keywords) or 'нет'} · комментарии={'да' if include_comments else 'нет'} · задержка сообщений={message_delay} сек. · задержка чатов={chat_delay} сек.",
                "",
            ]
            for result in results:
                txt_lines.extend([
                    f"ЧАТ: {result['title']}",
                    f"Chat ID: {result['channel_id']}",
                    f"Username: @{result['username']}" if result["username"] else "Username: —",
                    f"Сообщений: {len(result['posts'])} из просмотренных {result.get('scanned', len(result['posts']))}",
                    "",
                ])
                for post in result["posts"]:
                    reactions = ", ".join(f"{item['emoji']}:{item['count']}" for item in post["reactions"])
                    comments = " || ".join(f"{item['date']} · {item['sender_id']}: {item['text']}" for item in post.get("comments", []))
                    writer.writerow([
                        result["title"], result["channel_id"], post["id"], post["date"],
                        post["views"], post["forwards"], post["replies"], reactions,
                        post["has_media"], post["text"], " ".join(post.get("links", [])), comments,
                    ])
                    txt_lines.extend([
                        f"Публикация #{post['id']}", f"Дата: {post['date'] or '—'}",
                        f"Просмотры: {post['views']} · Пересылки: {post['forwards']} · Ответы: {post['replies']}",
                        f"Реакции: {reactions or 'нет'}",
                        f"Медиа: {'да' if post['has_media'] else 'нет'}",
                        f"Ссылки: {', '.join(post.get('links', [])) or 'нет'}",
                        "Текст:", post["text"] or "[без текста]",
                    ])
                    if post.get("comments"):
                        txt_lines.append("Комментарии:")
                        for comment in post["comments"]:
                            txt_lines.append(f"  [{comment['date'] or '—'}] ID {comment['sender_id']}: {comment['text']}")
                    txt_lines.extend(["", "─" * 48, ""])
            if errors:
                txt_lines.extend(["ЧАТЫ С ОШИБКАМИ:"])
                txt_lines.extend(f"- {item['title']} (ID {item['chat_id']}): {item['error']}" for item in errors)
            await m.answer_document(
                BufferedInputFile("\n".join(txt_lines).encode("utf-8"), filename=f"history-account-{aid}.txt"),
                caption=f"История аккаунта #{aid}: {total_messages} сообщений из {len(results)} чатов.",
            )
            await m.answer_document(
                BufferedInputFile(output.getvalue().encode("utf-8-sig"), filename=f"history-account-{aid}.csv"),
                caption="Дополнительно: таблица CSV.",
            )
            message = (
                f"🔍 Парсинг завершён · аккаунт #{aid}\n"
                f"Чатов обработано: {len(results)}/{scanned_chats} · сообщений: {total_messages}\n"
                f"Чатов с ошибками: {len(errors)}. Результаты отправлены в TXT и CSV."
            )
        elif action == "parseparticipants":
            parts = [x.strip() for x in raw.split("|")]
            if not 1 <= len(parts) <= 10:
                raise ValueError("Формат: группа | лимит | без ботов | без удалённых | username | фото | Premium | активность дней | без scam | исключить заблокированных.")
            def yes(index):
                return len(parts) > index and parts[index].lower() in {"да", "yes", "true", "1", "on"}
            limit = int(parts[1]) if len(parts) > 1 and parts[1] else 200
            active_days = int(parts[7]) if len(parts) > 7 and parts[7] else 0
            result = await telegram_management.parse_chat_participants(
                aid, parts[0], limit=limit,
                exclude_bots=yes(2), exclude_deleted=yes(3),
                username_only=yes(4), photo_only=yes(5), premium_only=yes(6),
                active_days=active_days, exclude_scam=yes(8), exclude_blocked=yes(9),
            )
            output = io.StringIO()
            writer = csv.writer(output)
            writer.writerow(["telegram_id", "username", "first_name", "last_name", "is_bot", "is_deleted", "is_scam", "is_blocked", "has_photo", "is_premium", "last_seen"])
            for person in result["participants"]:
                writer.writerow([
                    person["id"], person["username"], person["first_name"],
                    person["last_name"], person["is_bot"], person["is_deleted"], person["is_scam"], person["is_blocked"],
                    person["has_photo"], person["is_premium"], person["last_seen"],
                ])
            txt_lines = [
                f"УЧАСТНИКИ ГРУППЫ: {result['title']}",
                f"Аккаунт: #{aid}", f"Chat ID: {result['chat_id']}",
                f"Username: @{result['username']}" if result["username"] else "Username: —",
                f"Получено после фильтров: {result['visible_count']} из просмотренных {result['scanned_count']} (лимит {result['requested_limit']})",
                f"Фильтры: без ботов={result['filters']['exclude_bots']} · без удалённых={result['filters']['exclude_deleted']} · без scam={result['filters']['exclude_scam']} · без заблокированных={result['filters']['exclude_blocked']} · username={result['filters']['username_only']} · фото={result['filters']['photo_only']} · Premium={result['filters']['premium_only']} · активность дней={result['filters']['active_days']}",
                f"Активность скрыта Telegram у: {result['activity_unknown_count']} профилей", "",
            ]
            for index, person in enumerate(result["participants"], 1):
                full_name = " ".join(filter(None, [person["first_name"], person["last_name"]])) or "Без имени"
                txt_lines.append(
                    f"{index}. {full_name} · ID: {person['id']} · "
                    f"Username: @{person['username']}" if person["username"] else
                    f"{index}. {full_name} · ID: {person['id']} · Username: —"
                )
                txt_lines.append(f"   Бот: {'да' if person['is_bot'] else 'нет'} · Удалён: {'да' if person['is_deleted'] else 'нет'}")
            await m.answer_document(BufferedInputFile("\n".join(txt_lines).encode("utf-8"), filename=f"participants-account-{aid}-{result['chat_id']}.txt"), caption=f"Участники: {result['title']} · {result['visible_count']} записей в TXT.")
            document = BufferedInputFile(output.getvalue().encode("utf-8-sig"), filename=f"participants-account-{aid}-{result['chat_id']}.csv")
            await m.answer_document(document, caption="Дополнительно: таблица CSV. Выгружены только публичные поля, доступные аккаунту.")
            message = f"Сформированы TXT и CSV. Доступно участников: {result['visible_count']}."
        elif action in {"join", "leave"}:
            account_ids = [int(value) for value in (data.get("management_account_ids") or [aid])]
            results = []
            for account_id in account_ids:
                try:
                    if action == "join":
                        await telegram_management.join_chat(account_id, raw)
                        results.append(f"✅ #{account_id}: вступил в чат.")
                    else:
                        await telegram_management.leave_chat(account_id, raw)
                        results.append(f"✅ #{account_id}: вышел из чата.")
                except Exception as exc:
                    results.append(f"❌ #{account_id}: {type(exc).__name__}: {str(exc)[:120]}")
            message = "\\n".join(results)
        elif action == "schedreaction":
            parts = [x.strip() for x in raw.split("|")]
            if len(parts) != 4:
                raise ValueError("Формат: чат | ID публикации | emoji | время UTC или now.")
            run_at = datetime.now(timezone.utc)
            if parts[3].lower() != "now":
                try:
                    run_at = datetime.strptime(parts[3], "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
                except ValueError as exc:
                    raise ValueError("Время должно быть YYYY-MM-DD HH:MM UTC.") from exc
            account_ids = [int(value) for value in (data.get("management_account_ids") or [aid])]
            results = []
            for account_id in account_ids:
                try:
                    reaction_id = await telegram_automation.schedule_reaction(
                        account_id, chat_ref=parts[0], message_id=int(parts[1]), emoji=parts[2], run_at=run_at,
                    )
                    results.append(f"✅ #{account_id}: реакция #{reaction_id}")
                except Exception as exc:
                    results.append(f"❌ #{account_id}: {type(exc).__name__}: {str(exc)[:120]}")
            message = "\\n".join(results) + f"\\nВремя UTC: {run_at.isoformat()}."
        elif action == "reaction":
            parts = [x.strip() for x in raw.split("|")]
            if len(parts) != 3: raise ValueError("Формат: чат | ID публикации | emoji.")
            account_ids = [int(value) for value in (data.get("management_account_ids") or [aid])]
            results = []
            for account_id in account_ids:
                try:
                    await telegram_management.react_to_message(
                        account_id, chat=parts[0], message_id=int(parts[1]), emoji=parts[2]
                    )
                    results.append(f"✅ #{account_id}: реакция отправлена")
                except Exception as exc:
                    results.append(f"❌ #{account_id}: {type(exc).__name__}: {str(exc)[:120]}")
            message = "\\n".join(results)
        elif action == "editprofile":
            account_ids = [int(value) for value in (data.get("management_account_ids") or [aid])]
            results = []
            lines = [line.strip() for line in raw.splitlines() if line.strip()]
            if len(lines) > 1 or (lines and len(lines[0].split("|")) == 5):
                values_by_account = {}
                for line in lines:
                    fields = [value.strip() for value in line.split("|")]
                    if len(fields) != 5 or not fields[0].isdigit():
                        raise ValueError("Для разных профилей используйте строки: ID аккаунта | имя | фамилия | BIO | username. Для пропуска поля укажите -.")
                    account_id = int(fields[0])
                    if account_id not in account_ids or account_id in values_by_account:
                        raise ValueError("Укажите каждый выбранный аккаунт не более одного раза.")
                    values_by_account[account_id] = [None if value == "-" else value for value in fields[1:]]
                if set(values_by_account) != set(account_ids):
                    raise ValueError("Укажите отдельную строку для каждого выбранного аккаунта.")
            else:
                parts = [x.strip() for x in raw.split("|")]
                if len(parts) != 4:
                    raise ValueError("Для одинаковых данных: имя | фамилия | BIO | username.")
                values = [None if x == "-" else x for x in parts]
                values_by_account = {account_id: values for account_id in account_ids}
            for account_id in account_ids:
                try:
                    values = values_by_account[account_id]
                    await telegram_management.update_profile(
                        account_id, first_name=values[0], last_name=values[1], about=values[2], username=values[3]
                    )
                    results.append(f"✅ #{account_id}: профиль обновлён")
                except Exception as exc:
                    results.append(f"❌ #{account_id}: {type(exc).__name__}: {str(exc)[:120]}")
            message = "\\n".join(results)
        else:
            raise ValueError("Неизвестное действие.")
        module = data.get("module_selection_module")
        if action and action.startswith("policy-field-"):
            back_callback, back_label = f"tool:policy:{action.split('-', 3)[2]}", "↩️ К настройкам модуля"
        elif module:
            back_callback, back_label = f"tool:module:{module}", "↩️ К модулю"
        else:
            back_callback = "tool:management" if action == "global-limits" or action.startswith("global-") else f"tool:management:account:{aid}"
            back_label = "↩️ К управлению аккаунтами" if action == "global-limits" or action.startswith("global-") else "↩️ К инструментам аккаунта"
        await finish_prompt(m, state, message, kb([
            [(back_label, back_callback)],
            [("🏠 Главное меню", "nav:home")],
        ]))
    except telegram_management.FloodWaitError as exc:
        await finish_prompt(
            m, state,
            f"⏳ Telegram установил FloodWait: {int(exc.seconds)} сек.\n"
            "Операция не повторялась автоматически. Повторите её после указанного времени.",
            kb([[("↩️ К инструментам аккаунта", f"tool:management:account:{aid}")]]),
        )
    except Exception as exc:
        await prompt_message(m, state, f"❌ {type(exc).__name__}: {str(exc)[:500]}\n\nПроверьте ввод и попробуйте ещё раз.")

@router.message(Flow.management_photo)
async def management_photo_received(m: Message, state: FSMContext):
    if not await check_msg(m) or not await private(m):
        return
    photo = m.photo[-1] if m.photo else None
    if not photo and m.document and (m.document.mime_type or "").startswith("image/"):
        photo = m.document
    if not photo:
        await prompt_message(m, state, "Отправьте изображение как фото или файл.")
        return
    if int(getattr(photo, "file_size", 0) or 0) > 10 * 1024 * 1024:
        await prompt_message(m, state, "Изображение больше 10 МБ. Отправьте файл меньшего размера.")
        return
    data = await state.get_data()
    aid = int(data.get("management_account_id") or 0)
    account_ids = [int(value) for value in (data.get("management_account_ids") or [aid])]
    module = data.get("module_selection_module") or "profile"
    original_name = getattr(photo, "file_name", None) or "profile.jpg"
    extension = os.path.splitext(original_name)[1].lower()
    if extension not in {".jpg", ".jpeg", ".png"}:
        extension = ".jpg"
    path = os.path.join(
        tempfile.gettempdir(),
        f"atciv-profile-{aid}-{secrets.token_hex(8)}{extension}",
    )
    try:
        await m.bot.download(photo, destination=path)
        if data.get("photo_per_account"):
            index = int(data.get("photo_account_index") or 0)
            if index >= len(account_ids):
                raise ValueError("Список аккаунтов для фотографий уже завершён.")
            account_id = account_ids[index]
            results = list(data.get("photo_results") or [])
            try:
                await telegram_management.set_profile_photo(account_id, path)
                results.append(f"✅ #{account_id}: фото обновлено")
            except Exception as exc:
                results.append(f"❌ #{account_id}: {type(exc).__name__}: {str(exc)[:120]}")
            next_index = index + 1
            if next_index < len(account_ids):
                await state.update_data(photo_account_index=next_index, photo_results=results)
                await prompt_message(m, state, f"Фото для аккаунта #{account_id} обработано. Теперь отправьте отдельное фото для аккаунта #{account_ids[next_index]}.")
                return
        else:
            results = []
            for account_id in account_ids:
                try:
                    await telegram_management.set_profile_photo(account_id, path)
                    results.append(f"✅ #{account_id}: фото обновлено")
                except Exception as exc:
                    results.append(f"❌ #{account_id}: {type(exc).__name__}: {str(exc)[:120]}")
        await finish_prompt(m, state, "\\n".join(results), kb([
            [("↩️ К модулю", f"tool:module:{module}")],
            [("🏠 Главное меню", "nav:home")],
        ]))
    except telegram_management.FloodWaitError as exc:
        await finish_prompt(m, state, f"Telegram установил FloodWait: {int(exc.seconds)} сек. Фото не загружено.", kb([
            [("↩️ К инструментам аккаунта", f"tool:management:account:{aid}")],
        ]))
    except Exception as exc:
        await prompt_message(m, state, f"Не удалось обновить фото: {type(exc).__name__}: {str(exc)[:350]}")
    finally:
        try:
            if os.path.exists(path):
                os.remove(path)
        except OSError:
            logging.exception("Could not remove temporary profile photo %s", path)


@router.message(Flow.management_campaign_content)
async def management_campaign_content_received(m: Message, state: FSMContext):
    if not await check_msg(m) or not await private(m):
        return
    data = await state.get_data()
    kind = data.get("campaign_kind")
    payload = {"text": "", "file_path": "", "caption": ""}
    attachment = m.document or (m.photo[-1] if m.photo else None) or m.video or m.audio
    if attachment:
        if int(getattr(attachment, "file_size", 0) or 0) > 20 * 1024 * 1024:
            await prompt_message(m, state, "Файл больше 20 МБ. Отправьте файл меньшего размера.")
            return
        upload_dir = os.path.join(str(cfg.data_dir), "automation_uploads")
        os.makedirs(upload_dir, mode=0o700, exist_ok=True)
        original_name = getattr(attachment, "file_name", None) or ("photo.jpg" if m.photo else "file.bin")
        filename = secrets.token_hex(16) + "_" + re.sub(r"[^a-zA-Z0-9._-]", "_", original_name)[:80]
        path = os.path.join(upload_dir, filename)
        await m.bot.download(attachment, destination=path)
        payload["file_path"] = path
        payload["caption"] = (m.caption or "")[:4096]
    else:
        payload["text"] = (m.text or "").strip()
        if not payload["text"]:
            await prompt_message(m, state, "Отправьте текст или прикрепите документ.")
            return
    if kind == "invite" and not re.search(r"https?://t\.me/|t\.me/", payload["text"] or payload["caption"], re.I):
        await prompt_message(m, state, "В тексте приглашения должна быть ссылка t.me.")
        return
    await state.update_data(campaign_payload=payload)
    await state.set_state(Flow.management_campaign_confirm)
    recipients = data.get("campaign_recipient_labels", [])
    preview = (
        f"📨 Предпросмотр задачи\n\nТип: {kind}\nРежим: {'повторяющееся расписание' if data.get('campaign_mode') == 'recurring' else 'один запуск'}\nПолучателей: {len(recipients)}\n"
        f"Запуск (UTC): {data.get('campaign_scheduled_at')}\nПолучатели: {', '.join(recipients[:12])}"
        + (" и другие" if len(recipients) > 12 else "")
        + f"\n\nТекст: {(payload['text'] or payload['caption'] or 'Файл без подписи')[:700]}\n\n"
        + ("Для прямого добавления Telegram проверит приватность и invite_users; " if kind == "direct_invite" else "Проверьте список выбранных получателей перед запуском. ")
        + ("Для group_broadcast аккаунт должен быть администратором каждой группы." if kind == "group_broadcast" else "")
    )
    await delete_msg(m)
    keyboard = kb([
        [("✅ Подтвердить и поставить в очередь", "tool:campaign:confirm")],
        [("❌ Отменить", "tool:campaign:cancel")],
    ])
    anchor = await state.get_data()
    if not await edit_anchor(m.bot, anchor, preview, keyboard):
        await m.answer(preview, reply_markup=keyboard)


@router.callback_query(F.data == "tool:campaign:confirm")
async def management_campaign_confirm(c: CallbackQuery, state: FSMContext):
    if not await check_cb(c):
        return
    data = await state.get_data()
    if data.get("management_action") != "campaign_content":
        await c.answer("Предпросмотр устарел. Создайте задачу заново.", show_alert=True)
        return
    account_ids = [int(value) for value in (data.get("campaign_account_ids") or [data["management_account_id"]])]
    recipient_refs = {
        int(peer_id): str(peer_ref)
        for peer_id, peer_ref in zip(data.get("campaign_recipients", []), data.get("campaign_recipient_refs", []))
    }
    recipient_labels = {
        int(peer_id): str(label)
        for peer_id, label in zip(data.get("campaign_recipients", []), data.get("campaign_recipient_labels", []))
    }
    succeeded = []
    failed = []
    try:
        for account_id in account_ids:
            try:
                account_payload = dict(data["campaign_payload"])
                if data.get("campaign_kind") == "direct_invite":
                    account_payload.update((data.get("campaign_target_groups") or {}).get(str(account_id), {}))
                if int(data.get("campaign_schedule_interval_seconds") or 0):
                    task_id = await telegram_automation.create_schedule(
                        account_id, kind=data["campaign_kind"],
                        recipients=data["campaign_recipients"], payload=account_payload,
                        starts_at=datetime.fromisoformat(data["campaign_scheduled_at"]),
                        ends_at=datetime.fromisoformat(data["campaign_schedule_ends_at"]) if data.get("campaign_schedule_ends_at") else None,
                        interval_seconds=int(data["campaign_schedule_interval_seconds"]),
                        max_runs=int(data.get("campaign_schedule_max_runs") or 1),
                        recipient_refs=recipient_refs, recipient_labels=recipient_labels,
                    )
                    succeeded.append((account_id, task_id, "расписание"))
                else:
                    task_id = await telegram_automation.create_campaign(
                        account_id, kind=data["campaign_kind"],
                        recipients=data["campaign_recipients"], payload=account_payload,
                        scheduled_at=datetime.fromisoformat(data["campaign_scheduled_at"]),
                        recipient_refs=recipient_refs, recipient_labels=recipient_labels,
                    )
                    succeeded.append((account_id, task_id, "задача"))
            except Exception as exc:
                logging.exception("Could not enqueue campaign for account %s", account_id)
                failed.append((account_id, f"{type(exc).__name__}: {str(exc)[:180]}"))
        await state.clear()
        if not succeeded:
            await edit(c, "Не удалось создать задачи ни для одного аккаунта:\n" + "\n".join(
                f"#{aid}: {error}" for aid, error in failed
            ), kb([[("↩️ К инструментам", "tool:menu")]]))
            return
        lines = [
            f"✅ Аккаунт #{aid}: {kind} #{task_id}"
            for aid, task_id, kind in succeeded
        ]
        if failed:
            lines.extend(["", "⚠️ Не удалось поставить в очередь:"] + [
                f"#{aid}: {error}" for aid, error in failed
            ])
        lines.extend([
            "",
            "Каждый аккаунт получил отдельную задачу и выполняет её в собственной изолированной сессии.",
            "Ограничения Telegram, приватность, права доступа и FloodWait остаются обязательными.",
        ])
        module = data.get("module_selection_module") or "broadcast"
        await edit(c, "\n".join(lines), kb([
            [("↩️ К модулю", f"tool:module:{module}")],
            [("🏠 Главное меню", "nav:home")],
        ]))
    except Exception as exc:
        await c.answer(f"Не удалось создать задачу: {str(exc)[:180]}", show_alert=True)


@router.callback_query(F.data == "tool:campaign:cancel")
async def management_campaign_cancel(c: CallbackQuery, state: FSMContext):
    if not await check_cb(c):
        return
    data = await state.get_data()
    file_path = str((data.get("campaign_payload") or {}).get("file_path") or "")
    upload_root = os.path.realpath(os.path.join(str(cfg.data_dir), "automation_uploads"))
    if file_path and os.path.commonpath([upload_root, os.path.realpath(file_path)]) == upload_root:
        try:
            os.remove(file_path)
        except OSError:
            logging.exception("Could not remove cancelled campaign upload %s", file_path)
    await state.clear()
    await edit(c, "Создание задачи отменено.", kb([ [("↩️ К инструментам", "tool:management")] ]))


def trigger_back_callback(scope: str, account_id: int | None = None) -> str:
    return f"acc:view:{account_id}" if scope == "account" else "tool:menu"


def trigger_rule_title(rule: TriggerRule) -> str:
    terms = ", ".join(parse_trigger_terms(rule.terms))
    return (terms[:42] + "…") if len(terms) > 43 else terms


@router.callback_query(F.data == "trigger:global")
async def trigger_global_menu(c: CallbackQuery):
    await trigger_screen(c, "global")


@router.callback_query(F.data.startswith("trigger:account:"))
async def trigger_account_menu(c: CallbackQuery):
    if not await check_cb(c): return
    await trigger_screen(c, "account", int(c.data.split(":")[2]))


async def trigger_screen(c: CallbackQuery, scope: str, account_id: int | None = None):
    if not await check_cb(c): return
    if scope not in {"account", "global"}:
        await c.answer("Неизвестный раздел.", show_alert=True)
        return
    async with SessionFactory() as db:
        if scope == "account":
            account = await db.get(Account, account_id)
            if not account:
                await c.answer("Аккаунт не найден.", show_alert=True)
                return
            rules = list((await db.scalars(
                select(TriggerRule).where(TriggerRule.account_id == account_id).order_by(TriggerRule.id)
            )).all())
            title = f"🎯 Триггеры аккаунта #{account_id}"
            detail = "Локальные триггеры аккаунта имеют приоритет над глобальными."
        else:
            rules = list((await db.scalars(
                select(TriggerRule).where(TriggerRule.account_id.is_(None)).order_by(TriggerRule.id)
            )).all())
            title = "🎯 Глобальные триггеры"
            detail = (
                "Глобальные триггеры применяются по выбранному режиму. "
                "Если совпал локальный триггер аккаунта, он всегда важнее глобального."
            )
    lines = [title, "", detail, ""]
    rows = []
    if not rules:
        lines.append("Триггеры пока не добавлены.")
    for rule in rules:
        state = "🟢" if rule.enabled else "⚪"
        apply_text = ""
        if scope == "global":
            apply_text = " · все аккаунты" if rule.apply_mode == "all" else " · без локальных"
        lines.append(
            f"{state} #{rule.id} · {trigger_rule_title(rule)}{apply_text}\n"
            f"Пауза триггера: {format_trigger_duration(rule.cooldown_seconds)}\n"
            f"Пауза между сообщениями: {format_trigger_duration(rule.message_cooldown_seconds)}"
        )
        rows.append([(f"{state} {trigger_rule_title(rule)}", f"trigger:view:{rule.id}")])
    if scope == "account":
        rows.append([("➕ Добавить триггер", f"trigger:add:account:{account_id}")])
    else:
        rows.append([("➕ Добавить глобальный триггер", "trigger:global:add")])
    rows.append([("⬅️ Назад", trigger_back_callback(scope, account_id))])
    await edit(c, "\n".join(lines), kb(rows))


@router.callback_query(F.data == "trigger:global:add")
async def trigger_global_add(c: CallbackQuery):
    if not await check_cb(c): return
    await edit(c, "Как распространить новый глобальный триггер?", kb([
        [("Добавить аккаунтам без своих триггеров", "trigger:global:empty")],
        [("Добавить всем аккаунтам без исключения", "trigger:global:all")],
        [("⬅️ К глобальным триггерам", "trigger:global")],
    ]))


@router.callback_query(F.data == "trigger:global:all")
async def trigger_global_all_confirm(c: CallbackQuery):
    if not await check_cb(c): return
    await edit(c,
        "⚠️ Подтвердите добавление глобального триггера для всех аккаунтов.\n\n"
        "Глобальное правило станет доступно каждому аккаунту. При совпадении локального триггера "
        "аккаунта локальное правило всё равно будет иметь приоритет. Продолжить?",
        kb([
            [("✅ Да, для всех аккаунтов", "trigger:global:allconfirm")],
            [("❌ Отмена", "trigger:global")],
        ]),
    )


async def trigger_add_start(c: CallbackQuery, state: FSMContext, scope: str, account_id: int | None, apply_mode: str):
    await state.clear()
    await state.update_data(
        trigger_scope=scope, trigger_account_id=account_id, trigger_apply_mode=apply_mode,
        ui_chat_id=c.message.chat.id, ui_message_id=c.message.message_id,
        return_callback=(f"trigger:account:{account_id}" if scope == "account" else "trigger:global"),
    )
    await state.set_state(Flow.trigger_terms)
    await edit(c,
        "Отправьте слова или фразы для одного триггера.\n\n"
        "Одиночные слова можно перечислить через пробел, запятую или с новой строки. "
        "Фразы из нескольких слов разделяйте запятыми или переносами строк.\n"
        "Например: впн VPN Випиэн\n"
        "Другой триггер: коровы, продажа коров, амбары.",
        CANCEL,
    )


@router.callback_query(F.data.startswith("trigger:add:account:"))
async def trigger_account_add(c: CallbackQuery, state: FSMContext):
    if not await check_cb(c): return
    aid = int(c.data.split(":")[3])
    await trigger_add_start(c, state, "account", aid, "account")


@router.callback_query(F.data == "trigger:global:empty")
async def trigger_global_empty_add(c: CallbackQuery, state: FSMContext):
    if not await check_cb(c): return
    await trigger_add_start(c, state, "global", None, "empty_only")


@router.callback_query(F.data == "trigger:global:allconfirm")
async def trigger_global_all_add(c: CallbackQuery, state: FSMContext):
    if not await check_cb(c): return
    await trigger_add_start(c, state, "global", None, "all")


@router.message(Flow.trigger_terms)
async def trigger_terms_received(m: Message, state: FSMContext):
    if not await check_msg(m) or not await private(m): return
    terms = parse_trigger_terms(m.text or "")
    if not terms or len(terms) > 30:
        await prompt_message(m, state, "Добавьте от 1 до 30 слов или фраз. Слова можно разделять пробелами, фразы — запятыми или переносами строк.")
        return
    await delete_msg(m)
    await state.update_data(trigger_terms="\n".join(terms))
    await state.set_state(Flow.trigger_response)
    await prompt_message(m, state,
        "Теперь отправьте инструкцию для ИИ по этому триггеру.\n\n"
        "Она будет добавляться к персоне, примерам, правилам аккаунта и контексту автообучения. "
        "Можно отправить /skip, чтобы использовать стандартную инструкцию."
    )


@router.message(Flow.trigger_response)
async def trigger_response_received(m: Message, state: FSMContext):
    if not await check_msg(m) or not await private(m): return
    value = (m.text or "").strip()
    if value == "/skip": value = ""
    if len(value) > 3500:
        await prompt_message(m, state, "Максимум 3500 символов. Отправьте более короткую инструкцию.")
        return
    await delete_msg(m)
    await state.update_data(trigger_response=value)
    await state.set_state(Flow.trigger_cooldown)
    await prompt_message(m, state,
        "Введите паузу до следующего ответа по этому триггеру в секундах.\n"
        "Например, 900 = 00:00:15:00 (15 минут). Можно указать 0, чтобы отключить."
    )


async def trigger_seconds_received(m: Message, state: FSMContext, *, field: str, next_state, next_prompt: str):
    raw = (m.text or "").strip()
    if not raw.isdigit() or int(raw) > 31536000:
        await prompt_message(m, state, "Введите целое число секунд от 0 до 31536000 (не более 365 дней).")
        return
    await delete_msg(m)
    await state.update_data(**{field: int(raw)})
    await state.set_state(next_state)
    await prompt_message(m, state, next_prompt)


@router.message(Flow.trigger_cooldown)
async def trigger_cooldown_received(m: Message, state: FSMContext):
    if not await check_msg(m) or not await private(m): return
    await trigger_seconds_received(
        m, state, field="trigger_cooldown_seconds", next_state=Flow.trigger_message_cooldown,
        next_prompt="Введите паузу между ответами на любые триггеры этого аккаунта в секундах.\n"
        "Например, 60 = 00:00:01:00. Укажите 0, если общая пауза не нужна.",
    )


@router.message(Flow.trigger_message_cooldown)
async def trigger_message_cooldown_received(m: Message, state: FSMContext):
    if not await check_msg(m) or not await private(m): return
    raw = (m.text or "").strip()
    if not raw.isdigit() or int(raw) > 31536000:
        await prompt_message(m, state, "Введите целое число секунд от 0 до 31536000.")
        return
    await delete_msg(m)
    data = await state.get_data()
    async with SessionFactory() as db:
        rule = TriggerRule(
            account_id=data.get("trigger_account_id"),
            apply_mode=data.get("trigger_apply_mode") or "empty_only",
            terms=data["trigger_terms"],
            response_instructions=data.get("trigger_response", ""),
            cooldown_seconds=int(data.get("trigger_cooldown_seconds", 900)),
            message_cooldown_seconds=int(raw),
            enabled=True,
        )
        db.add(rule)
        await db.commit()
    scope = data["trigger_scope"]
    account_id = data.get("trigger_account_id")
    await finish_prompt(
        m, state, "✅ Триггер сохранён. Подключённые аккаунты начнут учитывать его сразу.",
        kb([[("🎯 К списку триггеров", f"trigger:account:{account_id}" if scope == "account" else "trigger:global")]]),
    )


async def trigger_rule_screen(c: CallbackQuery, rule_id: int):
    if not await check_cb(c): return
    async with SessionFactory() as db:
        rule = await db.get(TriggerRule, rule_id)
        if not rule:
            await c.answer("Триггер не найден.", show_alert=True)
            return
        scope = "global" if rule.account_id is None else "account"
        account_id = rule.account_id
        terms = parse_trigger_terms(rule.terms)
        title = "Глобальный триггер" if scope == "global" else f"Триггер аккаунта #{account_id}"
        apply_mode = "для всех аккаунтов" if rule.apply_mode == "all" else "только для аккаунтов без локальных триггеров"
        text = (
            f"🎯 {title} · #{rule.id}\n\n"
            f"Искомые слова/фразы:\n{', '.join(terms)}\n\n"
            f"Инструкция для ИИ:\n{rule.response_instructions or 'Стандартная инструкция'}\n\n"
            f"Пауза этого триггера: {format_trigger_duration(rule.cooldown_seconds)}\n"
            f"Пауза между сообщениями: {format_trigger_duration(rule.message_cooldown_seconds)}\n"
            f"Статус: {'включён' if rule.enabled else 'выключен'}"
        )
        if scope == "global":
            text += f"\nПрименение: {apply_mode}"
    back = f"trigger:account:{account_id}" if scope == "account" else "trigger:global"
    rows = [
        [("✏️ Изменить слова", f"trigger:edit:terms:{rule_id}")],
        [("✏️ Изменить инструкцию ИИ", f"trigger:edit:response:{rule_id}")],
        [("⏱ Пауза триггера", f"trigger:edit:cooldown_seconds:{rule_id}")],
        [("⏱ Пауза между сообщениями", f"trigger:edit:message_cooldown_seconds:{rule_id}")],
        [("🔴 Выключить" if rule.enabled else "🟢 Включить", f"trigger:toggle:{rule_id}")],
        [("🗑 Удалить", f"trigger:delete:{rule_id}")],
        [("⬅️ Назад", back)],
    ]
    await edit(c, text, kb(rows))


@router.callback_query(F.data.startswith("trigger:view:"))
async def trigger_view(c: CallbackQuery):
    if not await check_cb(c): return
    await trigger_rule_screen(c, int(c.data.split(":")[2]))


@router.callback_query(F.data.startswith("trigger:edit:"))
async def trigger_edit_start(c: CallbackQuery, state: FSMContext):
    if not await check_cb(c): return
    _, _, field, rule_raw = c.data.split(":")
    if field not in {"terms", "response", "cooldown_seconds", "message_cooldown_seconds"}:
        await c.answer("Неизвестное поле.", show_alert=True)
        return
    async with SessionFactory() as db:
        rule = await db.get(TriggerRule, int(rule_raw))
        if not rule:
            await c.answer("Триггер не найден.", show_alert=True)
            return
        scope = "global" if rule.account_id is None else "account"
        back = f"trigger:account:{rule.account_id}" if scope == "account" else "trigger:global"
    await state.clear()
    await state.update_data(trigger_edit_id=int(rule_raw), trigger_edit_field=field,
        return_callback=back, ui_chat_id=c.message.chat.id, ui_message_id=c.message.message_id)
    await state.set_state(Flow.trigger_edit_value)
    hints = {
        "terms": "Отправьте слова через пробел, а фразы из нескольких слов разделяйте запятыми или переносами строк.",
        "response": "Отправьте новую инструкцию ИИ. Для очистки отправьте /clear.",
        "cooldown_seconds": "Введите новую паузу этого триггера в секундах (0–31536000).",
        "message_cooldown_seconds": "Введите общую паузу между триггерами аккаунта в секундах (0–31536000).",
    }
    await edit(c, hints[field], CANCEL)


@router.message(Flow.trigger_edit_value)
async def trigger_edit_value_received(m: Message, state: FSMContext):
    if not await check_msg(m) or not await private(m): return
    data = await state.get_data()
    field = data["trigger_edit_field"]
    raw = (m.text or "").strip()
    if field == "terms":
        terms = parse_trigger_terms(raw)
        if not terms or len(terms) > 30:
            await prompt_message(m, state, "Укажите от 1 до 30 слов/фраз: слова через пробел, фразы — через запятую или с новой строки.")
            return
        value = "\n".join(terms)
    elif field == "response":
        value = "" if raw == "/clear" else raw
        if len(value) > 3500:
            await prompt_message(m, state, "Максимум 3500 символов.")
            return
    else:
        if not raw.isdigit() or int(raw) > 31536000:
            await prompt_message(m, state, "Введите целое число секунд от 0 до 31536000.")
            return
        value = int(raw)
    async with SessionFactory() as db:
        rule = await db.get(TriggerRule, data["trigger_edit_id"])
        if not rule:
            await finish_prompt(m, state, "Триггер не найден.")
            return
        attr = {
            "terms": "terms", "response": "response_instructions",
            "cooldown_seconds": "cooldown_seconds",
            "message_cooldown_seconds": "message_cooldown_seconds",
        }[field]
        setattr(rule, attr, value)
        await db.commit()
    rule_id = data["trigger_edit_id"]
    await finish_prompt(m, state, "✅ Настройка триггера обновлена.", kb([
        [("🎯 Открыть триггер", f"trigger:view:{rule_id}")],
    ]))


@router.callback_query(F.data.startswith("trigger:toggle:"))
async def trigger_toggle(c: CallbackQuery):
    if not await check_cb(c): return
    rule_id = int(c.data.split(":")[2])
    async with SessionFactory() as db:
        rule = await db.get(TriggerRule, rule_id)
        if not rule:
            await c.answer("Триггер не найден.", show_alert=True)
            return
        rule.enabled = not rule.enabled
        await db.commit()
    await trigger_rule_screen(c, rule_id)


@router.callback_query(F.data.startswith("trigger:delete:"))
async def trigger_delete_confirm(c: CallbackQuery):
    if not await check_cb(c): return
    rule_id = int(c.data.split(":")[2])
    async with SessionFactory() as db:
        rule = await db.get(TriggerRule, rule_id)
        if not rule:
            await c.answer("Триггер не найден.", show_alert=True)
            return
        title = trigger_rule_title(rule)
    await edit(c, f"Удалить триггер «{title}»?", kb([
        [("🗑 Да, удалить", f"trigger:deleteconfirm:{rule_id}")],
        [("⬅️ Отмена", f"trigger:view:{rule_id}")],
    ]))


@router.callback_query(F.data.startswith("trigger:deleteconfirm:"))
async def trigger_delete(c: CallbackQuery):
    if not await check_cb(c): return
    rule_id = int(c.data.split(":")[2])
    async with SessionFactory() as db:
        rule = await db.get(TriggerRule, rule_id)
        if not rule:
            await c.answer("Триггер уже удалён.", show_alert=True)
            return
        back = f"trigger:account:{rule.account_id}" if rule.account_id is not None else "trigger:global"
        await db.delete(rule)
        await db.commit()
    if back.startswith("trigger:account:"):
        await trigger_screen(c, "account", int(back.split(":")[2]))
    else:
        await trigger_screen(c, "global")


@router.callback_query(F.data=="nav:settings")
async def settings_cb(c: CallbackQuery):
    if not await check_cb(c): return
    text = (
        "⚙️ Настройки и справочник\n\n"
        "👤 Аккаунты — подключение по QR или номеру, статус, пауза, прокси, чаты и обучение.\n"
        "🤖 ИИ-агенты — провайдеры, API-ключи, модели, глобальный ИИ и назначение агента аккаунтам.\n"
        "🎭 Персоны — общие стили ответов; изменения применяются ко всем назначенным аккаунтам.\n"
        "💡 Примеры — образцы входящих сообщений и желаемых ответов для стиля.\n"

        "📡 Каналы — управление каналами внутри карточки каждого аккаунта.\n"
        "Ответы в ручном режиме показываются в уведомлении и не сохраняются.\n\n"
        "В карточке аккаунта: «Пауза» временно останавливает работу; «ИИ» включает генерацию; «Режим» выбирает ручную или автоматическую отправку; «Агент» и «Персона» задают модель и стиль; «Прокси» меняет сетевое подключение; «Автообучение» обновляет контекст.\n\n"
        "API ID/hash, токен бота и ключ шифрования задаются в .env на сервере. Сообщения с кодом и паролем входа удаляются ботом по возможности; не используйте бота, которому не доверяете."
    )
    await edit(c, text, kb([
        [("🎚 Вероятность ответов", "runtime:menu")],
        [("⏳ Поведение при FloodWait", "joinpolicy:menu")],
        [("🧾 Логи", "logs:menu")],
        [("⬅️ Главное меню", "nav:home")],
    ]))


@router.callback_query(F.data == "joinpolicy:menu")
async def join_floodwait_settings(c: CallbackQuery):
    if not await check_cb(c):
        return
    mode = await get_join_floodwait_mode()
    current = "Ждать и продолжать" if mode == "wait" else "Остановить вступление"
    await edit(c,
        "⏳ Поведение при ограничении Telegram\n\n"
        f"Сейчас: {current}\n\n"
        "Если Telegram временно ограничит вступление (FloodWait):\n"
        "• «Ждать и продолжать» — бот дождётся указанного Telegram времени и повторит текущее вступление. Если ограничение повторится, снова подождёт.\n"
        "• «Остановить вступление» — бот сразу прекратит текущую попытку и покажет длительность ограничения.\n\n"
        "Настройка общая для вступления в группы и каналы. Каждое вступление по-прежнему требует отдельного подтверждения.",
        kb([
            [("⏳ Ждать и продолжать" + (" ✓" if mode == "wait" else ""), "joinpolicy:set:wait")],
            [("⛔ Остановить вступление" + (" ✓" if mode == "stop" else ""), "joinpolicy:set:stop")],
            [("⬅️ Настройки", "nav:settings")],
        ]),
    )


@router.callback_query(F.data.startswith("joinpolicy:set:"))
async def join_floodwait_settings_save(c: CallbackQuery):
    if not await check_cb(c):
        return
    mode = c.data.rsplit(":", 1)[-1]
    if mode not in {"wait", "stop"}:
        await c.answer("Неизвестный режим.", show_alert=True)
        return
    async with SessionFactory() as db:
        setting = await db.get(AppSetting, "join_floodwait_mode")
        if setting:
            setting.value = mode
        else:
            db.add(AppSetting(key="join_floodwait_mode", value=mode))
        await db.commit()
    await c.answer("Настройка сохранена.")
    await join_floodwait_settings(c)

LOG_CATEGORIES = {
    "main": "📌 Основные логи",
    "replies": "💬 Ответы ИИ",
    "errors": "🚨 Ошибки",
    "learning": "🧠 Обучение",
    "channels": "📡 Каналы и обсуждения",
}

def log_destinations_from_value(value: str | None) -> list[tuple[int, int | None]]:
    """Parse legacy chat IDs and chat_id:message_thread_id destinations."""
    result: list[tuple[int, int | None]] = []
    for raw in (value or "").replace(";", ",").split(","):
        parts = raw.strip().split(":")
        try:
            chat_id = int(parts[0])
            thread_id = int(parts[1]) if len(parts) == 2 else None
        except (TypeError, ValueError):
            continue
        if len(parts) > 2 or chat_id == 0 or (thread_id is not None and thread_id <= 0):
            continue
        destination = (chat_id, thread_id)
        if destination not in result:
            result.append(destination)
    return result


def log_ids_from_value(value: str | None) -> list[int]:
    """Backward-compatible helper for code/tests that only need chat IDs."""
    return list(dict.fromkeys(chat_id for chat_id, _ in log_destinations_from_value(value)))


async def logs_menu_text():
    async with SessionFactory() as db:
        rows = {}
        for category in LOG_CATEGORIES:
            setting = await db.get(AppSetting, f"log_chat_ids_{category}")
            destinations = log_destinations_from_value(setting.value if setting else "")
            if category == "main" and setting is None and cfg.log_chat_id:
                destinations = [(cfg.log_chat_id, None)]
            rows[category] = destinations
    lines = ["🧾 Настройка логов", "", "Для каждого типа событий можно задать несколько получателей. Формат: ID чата или ID чата:ID темы форума. Например: -1004351902275:108.", ""]
    for category, label in LOG_CATEGORIES.items():
        destinations = rows[category]
        shown = ", ".join(f"{chat_id}" + (f" · тема {thread_id}" if thread_id is not None else "") for chat_id, thread_id in destinations) if destinations else "не настроено"
        lines.append(f"{label}: {shown}")
    lines += ["", "Основные логи по умолчанию используют LOG_CHAT_ID из .env, если отдельный получатель не добавлен."]
    return "\n".join(lines)


@router.callback_query(F.data == "logs:menu")
async def logs_menu(c: CallbackQuery):
    if not await check_cb(c): return
    rows = [[(label, f"logs:category:{category}")] for category, label in LOG_CATEGORIES.items()]
    rows += [[("⬅️ Настройки", "nav:settings"), ("🏠 Главное меню", "nav:home")]]
    await edit(c, await logs_menu_text(), kb(rows))


@router.callback_query(F.data.startswith("logs:category:"))
async def logs_category(c: CallbackQuery, category_override: str | None = None):
    if not await check_cb(c): return
    category = category_override or c.data.split(":")[2]
    if category not in LOG_CATEGORIES:
        await c.answer("Неизвестная категория.", show_alert=True); return
    async with SessionFactory() as db:
        setting = await db.get(AppSetting, f"log_chat_ids_{category}")
        destinations = log_destinations_from_value(setting.value if setting else "")
    if category == "main" and setting is None and cfg.log_chat_id:
        destinations = [(cfg.log_chat_id, None)]
    lines = [LOG_CATEGORIES[category], "", "Получатели этой категории:"]
    lines += [f"• {chat_id}" + (f" · тема {thread_id}" if thread_id is not None else " · общий чат") for chat_id, thread_id in destinations] if destinations else ["• пока не добавлены"]
    rows = [[("➕ Добавить чат или тему", f"logs:add:{category}")]]
    rows += [[(f"➖ Удалить {chat_id}" + (f" · тема {thread_id}" if thread_id is not None else ""), f"logs:remove:{category}:{chat_id}:{thread_id or 0}")] for chat_id, thread_id in destinations]
    rows += [[("⬅️ К списку логов", "logs:menu")]]
    await edit(c, "\n".join(lines), kb(rows))


@router.callback_query(F.data.startswith("logs:add:"))
async def logs_add(c: CallbackQuery, state: FSMContext):
    if not await check_cb(c): return
    category = c.data.split(":")[2]
    if category not in LOG_CATEGORIES:
        await c.answer("Неизвестная категория.", show_alert=True); return
    await state.clear()
    await state.update_data(log_category=category, ui_chat_id=c.message.chat.id, ui_message_id=c.message.message_id, return_callback=cancel_return_for(c.data))
    await state.set_state(Flow.log_chat_id)
    await edit(c, f"{LOG_CATEGORIES[category]}\n\nОтправьте ID чата или супергруппы. Чтобы направлять логи в тему форума, добавьте через двоеточие ID темы.\n\nПримеры:\n-1004351902275 — общий чат\n-1004351902275:108 — тема с ID 108.", CANCEL)


@router.message(Flow.log_chat_id)
async def logs_add_save(m: Message, state: FSMContext):
    if not await check_msg(m) or not await private(m): return
    data = await state.get_data()
    category = data.get("log_category")
    parsed = log_destinations_from_value(m.text or "")
    if len(parsed) != 1:
        await prompt_message(m, state, "Неверный формат. Отправьте ID чата или пару ID_чата:ID_темы, например -1004351902275:108.")
        return
    chat_id, thread_id = parsed[0]
    await delete_msg(m)
    try:
        kwargs = {"message_thread_id": thread_id} if thread_id is not None else {}
        await m.bot.send_message(chat_id, "✅ Проверка связи: этот получатель добавлен для логов AtcivPerson.", **kwargs)
    except Exception as exc:
        target = f"{chat_id}:{thread_id}" if thread_id is not None else str(chat_id)
        await finish_prompt(m, state, f"Не удалось отправить тестовое сообщение получателю {target}. Проверьте ID, ID темы и права бота.\nОшибка: {type(exc).__name__}: {str(exc)[:300]}", kb([[("↩️ К категории", f"logs:category:{category}")], [("🧾 Все логи", "logs:menu")]]))
        return
    async with SessionFactory() as db:
        key = f"log_chat_ids_{category}"
        setting = await db.get(AppSetting, key)
        destinations = log_destinations_from_value(setting.value if setting else "")
        if category == "main" and setting is None and cfg.log_chat_id:
            destinations = [(cfg.log_chat_id, None)]
        if (chat_id, thread_id) not in destinations:
            destinations.append((chat_id, thread_id))
        value = ",".join(f"{target_chat}:{target_thread}" if target_thread is not None else str(target_chat) for target_chat, target_thread in destinations)
        if setting: setting.value = value
        else: db.add(AppSetting(key=key, value=value))
        await db.commit()
    target = f"{chat_id} · тема {thread_id}" if thread_id is not None else str(chat_id)
    await finish_prompt(m, state, f"✅ Получатель {target} добавлен в категорию «{LOG_CATEGORIES[category]}».", kb([[("↩️ К категории", f"logs:category:{category}")], [("🧾 Все логи", "logs:menu")]]))


@router.callback_query(F.data.startswith("logs:remove:"))
async def logs_remove(c: CallbackQuery):
    if not await check_cb(c): return
    parts = c.data.split(":")
    if len(parts) != 5:
        await c.answer("Некорректная команда.", show_alert=True); return
    _, _, category, chat_raw, thread_raw = parts
    if category not in LOG_CATEGORIES:
        await c.answer("Неизвестная категория.", show_alert=True); return
    try:
        destination = (int(chat_raw), int(thread_raw) or None)
    except ValueError:
        await c.answer("Некорректный получатель.", show_alert=True); return
    async with SessionFactory() as db:
        key = f"log_chat_ids_{category}"
        setting = await db.get(AppSetting, key)
        destinations = log_destinations_from_value(setting.value if setting else "")
        destinations = [item for item in destinations if item != destination]
        value = ",".join(f"{chat_id}:{thread_id}" if thread_id is not None else str(chat_id) for chat_id, thread_id in destinations)
        if setting:
            setting.value = value
        else:
            db.add(AppSetting(key=key, value=value))
        await db.commit()
    await logs_category(c)

RUNTIME_KEYS = {
    "mention_reply_probability": ("📣 Ответ на упоминания", "%", 0, 100),
    "dm_reply_probability": ("💬 Ответ в личке", "%", 0, 100),
    "proactive_reply_probability": ("🗣 Самостоятельное участие", "%", 0, 100),
    "reply_cooldown_seconds": ("⏱ Пауза между ответами", "сек.", 0, 2147483647),
}

AGENT_CHANNEL_RUNTIME_KEYS = {
    "channel_reply_probability": ("📡 Ответы в комментариях", "%", 0, 100, "channel_reply_probability_override"),
    "channel_reply_cooldown_seconds": ("⏳ Пауза в комментариях", "сек.", 0, 2147483647, "channel_reply_cooldown_seconds_override"),
}

def format_duration(seconds: int) -> str:
    value = max(0, int(seconds))
    hours, remainder = divmod(value, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"

async def read_global_runtime(db):
    values = {}
    for key, (_, _, _, _) in RUNTIME_KEYS.items():
        row = await db.get(AppSetting, key)
        fallback = 0 if key in {"proactive_reply_probability", "reply_cooldown_seconds"} else 100
        try:
            value = int(row.value) if row and row.value.isdigit() else fallback
        except (TypeError, ValueError):
            value = fallback
        values[key] = value
    return values

async def runtime_settings_text():
    async with SessionFactory() as db:
        values = await read_global_runtime(db)
    lines = ["⚙️ Глобальные настройки ответов", ""]
    for key, (label, unit, _, _) in RUNTIME_KEYS.items():
        shown = format_duration(values[key]) if key.endswith("_seconds") else f"{values[key]}{unit}"
        lines.append(f"{label}: {shown}")
    lines += [
        "",
        "Эти значения используются всеми агентами, у которых для конкретного параметра не задано индивидуальное значение.",
        "При изменении глобального значения бот спросит, перезаписать ли настройки всех агентов или изменить только значение по умолчанию для тех, кто наследует глобальные настройки.",
        "",
        "Самостоятельное участие включайте только для аккаунтов, которые явно обозначены как управляемые ИИ. Для таких аккаунтов также включите отметку в карточке аккаунта.",
    ]
    return "\n".join(lines)

@router.callback_query(F.data == "runtime:menu")
async def runtime_settings_menu(c: CallbackQuery):
    if not await check_cb(c): return
    async with SessionFactory() as db:
        values = await read_global_runtime(db)
    buttons = []
    for key, (label, unit, _, _) in RUNTIME_KEYS.items():
        shown = format_duration(values[key]) if key.endswith("_seconds") else f"{values[key]}{unit}"
        buttons.append([(f"✏️ {label}: {shown}", f"runtime:input:{key}")])
    buttons.append([("⬅️ Настройки", "nav:settings"), ("🏠 Главное меню", "nav:home")])
    await edit(c, await runtime_settings_text(), kb(buttons))

@router.callback_query(F.data == "runtime:noop")
async def runtime_settings_noop(c: CallbackQuery):
    if not await check_cb(c): return
    await c.answer("Выберите значение в кнопках ниже.")

@router.callback_query(F.data.startswith("runtime:input:"))
async def runtime_settings_input(c: CallbackQuery, state: FSMContext):
    if not await check_cb(c): return
    key = c.data.split(":")[2]
    if key not in RUNTIME_KEYS:
        await c.answer("Неизвестная настройка.", show_alert=True); return
    label, unit, minimum, maximum = RUNTIME_KEYS[key]
    await state.clear()
    await state.update_data(runtime_key=key, runtime_scope="global", ui_chat_id=c.message.chat.id, ui_message_id=c.message.message_id, return_callback=cancel_return_for(c.data))
    await state.set_state(Flow.runtime_value)
    hint = f"Введите число секунд. Например: 3600 = 01:00:00 (60 минут). Максимум: {maximum} сек. ({format_duration(maximum)})." if unit == "сек." else f"Введите процент от {minimum}% до {maximum}%."
    await edit(c, f"{label}\n{hint}", CANCEL)

@router.message(Flow.runtime_value)
async def runtime_value_save(m: Message, state: FSMContext):
    if not await check_msg(m) or not await private(m): return
    data = await state.get_data()
    key = data.get("runtime_key")
    runtime_scope = data.get("runtime_scope")
    if runtime_scope == "agent_channel":
        meta = AGENT_CHANNEL_RUNTIME_KEYS.get(key)
        if not meta:
            await finish_prompt(m, state, "Настройка не найдена.")
            return
        label, unit, minimum, maximum, attr = meta
    else:
        if key not in RUNTIME_KEYS:
            await finish_prompt(m, state, "Настройка не найдена.")
            return
        label, unit, minimum, maximum = RUNTIME_KEYS[key]
        attr = key + "_override"
    raw = (m.text or "").strip()
    if not raw.isdigit():
        await prompt_message(m, state, "Нужно ввести целое число.")
        return
    value = int(raw)
    if not minimum <= value <= maximum:
        await prompt_message(m, state, f"Допустимый диапазон для «{label}»: {minimum}–{maximum}.")
        return
    await delete_msg(m)
    shown = format_duration(value) if unit == "сек." else f"{value}{unit}"
    if runtime_scope in {"agent", "agent_channel"}:
        aid = int(data["runtime_aid"])
        async with SessionFactory() as db:
            agent = await db.get(AIAgent, aid)
            if not agent:
                await finish_prompt(m, state, "ИИ-агент не найден.")
                return
            setattr(agent, attr, value)
            await db.commit()
        await finish_prompt(m, state, f"✅ {label}: {shown}", kb([[("⬅️ К настройкам агента", f"agent:runtime:menu:{aid}")], [("🏠 Главное меню", "nav:home")]]))
        return
    async with SessionFactory() as db:
        setting = await db.get(AppSetting, key)
        if setting:
            setting.value = str(value)
        else:
            db.add(AppSetting(key=key, value=str(value)))
        await db.commit()
    await finish_prompt(m, state, f"Глобальное значение «{label}» установлено: {shown}.\nКак применить его к ИИ-агентам?", kb([
        [("🌐 Применить ко всем агентам без исключения", f"runtime:apply:all:{key}:{value}")],
        [("🧩 Только агентам без собственного значения", f"runtime:apply:inherit:{key}:{value}")],
        [("⬅️ К настройкам", "runtime:menu")],
    ]))

@router.callback_query(F.data.startswith("runtime:apply:"))
async def runtime_settings_apply(c: CallbackQuery):
    if not await check_cb(c): return
    parts = c.data.split(":")
    if len(parts) != 5 or parts[2] not in {"all", "inherit"} or parts[3] not in RUNTIME_KEYS:
        await c.answer("Неизвестное действие.", show_alert=True); return
    scope, key = parts[2], parts[3]
    try:
        value = int(parts[4])
    except ValueError:
        await c.answer("Некорректное значение.", show_alert=True); return
    if not RUNTIME_KEYS[key][2] <= value <= RUNTIME_KEYS[key][3]:
        await c.answer("Недопустимое значение.", show_alert=True); return
    attr = key + "_override"
    async with SessionFactory() as db:
        setting = await db.get(AppSetting, key)
        if setting:
            setting.value = str(value)
        else:
            db.add(AppSetting(key=key, value=str(value)))
        if scope == "all":
            for agent in (await db.scalars(select(AIAgent))).all():
                setattr(agent, attr, value)
        await db.commit()
    label, unit, _, _ = RUNTIME_KEYS[key]
    suffix = "Значение записано во все агенты." if scope == "all" else "Индивидуальные значения агентов сохранены; обновлено глобальное значение для наследующих агентов."
    shown = format_duration(value) if unit == "сек." else f"{value}{unit}"
    await edit(c, f"✅ {label}: {shown}\n{suffix}", kb([
        [("⚙️ К настройкам ответов", "runtime:menu")],
        [("🤖 К ИИ-агентам", "agent:menu"), ("🏠 Главное меню", "nav:home")],
    ]))

@router.callback_query(F.data == "agent:runtime:noop")
async def agent_runtime_noop(c: CallbackQuery):
    if not await check_cb(c): return
    await c.answer("Выберите значение ниже.")

async def agent_runtime_menu(c: CallbackQuery, aid: int):
    async with SessionFactory() as db:
        agent = await db.get(AIAgent, aid)
        global_values = await read_global_runtime(db)
    if not agent:
        await c.answer("ИИ-агент не найден.", show_alert=True); return
    buttons = []
    lines = [f"🎚 Индивидуальные настройки · {agent.name}", "", "«Наследует» означает, что используется глобальное значение."]
    for key, (label, unit, _minimum, _maximum) in RUNTIME_KEYS.items():
        override = getattr(agent, key + "_override")
        shown = (format_duration(override) if key.endswith("_seconds") else f"{override}{unit}") if override is not None else (f"Наследует ({format_duration(global_values[key])})" if key.endswith("_seconds") else f"Наследует ({global_values[key]}{unit})")
        lines.append(f"{label}: {shown}")
        buttons.append([(f"✏️ {label}: {shown}", f"agent:rt:input:{aid}:{key}")])
        if override is not None:
            buttons.append([("↩️ Наследовать глобальное значение", f"agent:rt:inherit:{aid}:{key}")])
    for key, (label, unit, _minimum, _maximum, attr) in AGENT_CHANNEL_RUNTIME_KEYS.items():
        override = getattr(agent, attr)
        shown = (format_duration(override) if unit == "сек." else f"{override}{unit}") if override is not None else "Наследует настройки аккаунта"
        lines.append(f"{label}: {shown}")
        buttons.append([(f"✏️ {label}: {shown}", f"agent:channelrt:input:{aid}:{key}")])
        if override is not None:
            buttons.append([("↩️ Наследовать настройки аккаунта", f"agent:channelrt:inherit:{aid}:{key}")])
    buttons += [[("⬅️ К агенту", f"agent:view:{aid}"), ("🏠 Главное меню", "nav:home")]]
    await edit(c, "\n".join(lines), kb(buttons))

@router.callback_query(F.data.startswith("agent:runtime:menu:"))
async def agent_runtime_menu_cb(c: CallbackQuery):
    if not await check_cb(c): return
    await agent_runtime_menu(c, int(c.data.split(":")[3]))

@router.callback_query(F.data.startswith("agent:rt:input:"))
async def agent_runtime_input(c: CallbackQuery, state: FSMContext):
    if not await check_cb(c): return
    parts = c.data.split(":")
    aid, key = int(parts[3]), parts[4]
    if key not in RUNTIME_KEYS:
        await c.answer("Неизвестная настройка.", show_alert=True); return
    label, unit, minimum, maximum = RUNTIME_KEYS[key]
    await state.clear()
    await state.update_data(runtime_key=key, runtime_scope="agent", runtime_aid=aid, ui_chat_id=c.message.chat.id, ui_message_id=c.message.message_id, return_callback=cancel_return_for(c.data))
    await state.set_state(Flow.runtime_value)
    hint = f"Введите число секунд. Например: 3600 = 01:00:00. Максимум: {maximum} сек. ({format_duration(maximum)})." if unit == "сек." else f"Введите процент от {minimum}% до {maximum}%."
    await edit(c, f"{label}\n{hint}", CANCEL)

@router.callback_query(F.data.startswith("agent:channelrt:input:"))
async def agent_channel_runtime_input(c: CallbackQuery, state: FSMContext):
    if not await check_cb(c): return
    parts = c.data.split(":")
    aid, key = int(parts[3]), parts[4]
    if key not in AGENT_CHANNEL_RUNTIME_KEYS:
        await c.answer("Неизвестная настройка.", show_alert=True)
        return
    label, unit, minimum, maximum, _ = AGENT_CHANNEL_RUNTIME_KEYS[key]
    await state.clear()
    await state.update_data(runtime_key=key, runtime_scope="agent_channel", runtime_aid=aid, ui_chat_id=c.message.chat.id, ui_message_id=c.message.message_id, return_callback=cancel_return_for(c.data))
    await state.set_state(Flow.runtime_value)
    hint = f"Введите целое число секунд. Например: 3600 = 01:00:00. Максимум: {maximum} сек. ({format_duration(maximum)})." if unit == "сек." else f"Введите процент от {minimum}% до {maximum}%."
    await edit(c, f"{label}\n{hint}", CANCEL)

@router.callback_query(F.data.startswith("agent:channelrt:inherit:"))
async def agent_channel_runtime_inherit(c: CallbackQuery):
    if not await check_cb(c): return
    parts = c.data.split(":")
    aid, key = int(parts[3]), parts[4]
    if key not in AGENT_CHANNEL_RUNTIME_KEYS:
        await c.answer("Неизвестная настройка.", show_alert=True)
        return
    attr = AGENT_CHANNEL_RUNTIME_KEYS[key][4]
    async with SessionFactory() as db:
        agent = await db.get(AIAgent, aid)
        if not agent:
            await c.answer("ИИ-агент не найден.", show_alert=True)
            return
        setattr(agent, attr, None)
        await db.commit()
    await agent_runtime_menu(c, aid)

@router.callback_query(F.data.startswith("agent:rt:inherit:"))
async def agent_runtime_inherit(c: CallbackQuery):
    if not await check_cb(c): return
    parts = c.data.split(":")
    if len(parts) != 5 or parts[4] not in RUNTIME_KEYS:
        await c.answer("Неизвестная настройка.", show_alert=True); return
    aid, key = int(parts[3]), parts[4]
    async with SessionFactory() as db:
        agent = await db.get(AIAgent, aid)
        if not agent:
            await c.answer("ИИ-агент не найден.", show_alert=True); return
        setattr(agent, key + "_override", None)
        await db.commit()
    await agent_runtime_menu(c, aid)

# Account menu and QR login
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

@router.callback_query(F.data=="acc:add")
async def acc_add(c, state: FSMContext):
    if not await check_cb(c): return
    if not c.message or c.message.chat.type!="private":
        await c.answer("Откройте личный чат с ботом.",show_alert=True); return
    if not cfg.telegram_api_id or not cfg.telegram_api_hash:
        await c.answer("Заполните TELEGRAM_API_ID и TELEGRAM_API_HASH в .env.",show_alert=True); return
    await state.clear()
    await edit(c, "Выберите способ подключения:", kb([
        [("📷 QR · без прокси", "acc:add:direct")],
        [("🌐 QR · через прокси", "acc:add:proxy")],
        [("📱 Номер телефона · без прокси", "acc:add:phone:direct")],
        [("📱 Номер телефона · через прокси", "acc:add:phone:proxy")],
        [("⬅️ Назад", "acc:list")],
    ]))

async def start_qr_login(bot, uid, state: FSMContext, proxy_config=None):
    ui_data = await state.get_data()
    if ui_data.get("login_method") == "phone":
        await start_phone_login_prompt(bot, uid, state, proxy_config)
        return
    if uid in LOGIN_TASKS and not LOGIN_TASKS[uid].done():
        await bot.send_message(uid, "Вход уже запущен. Отмените текущий процесс перед новым.")
        return
    ui_data = await state.get_data()
    try:
        encrypted = encrypt_proxy(proxy_config) if proxy_config else None
    except ProxyConfigurationError as exc:
        if not await edit_anchor(bot, ui_data, str(exc), MAIN):
            await bot.send_message(uid, str(exc), reply_markup=MAIN)
        return
    await state.clear()
    if not await edit_anchor(bot, ui_data, "Готовлю QR-вход…", kb([[("❌ Отменить подключение", "nav:cancel")]])):
        await bot.send_message(uid, "Готовлю QR-вход…", reply_markup=kb([[("❌ Отменить подключение", "nav:cancel")]]))
    task = asyncio.create_task(qr_login(bot, uid, proxy_config, encrypted, ui_data, state))
    LOGIN_TASKS[uid] = task

@router.callback_query(F.data=="acc:add:direct")
async def acc_add_direct(c, state: FSMContext):
    if not await check_cb(c): return
    if not c.message or c.message.chat.type != "private":
        await c.answer("Откройте личный чат с ботом.", show_alert=True); return
    if not cfg.telegram_api_id or not cfg.telegram_api_hash:
        await c.answer("Заполните TELEGRAM_API_ID и TELEGRAM_API_HASH в .env.", show_alert=True); return
    await state.clear()
    await state.update_data(ui_chat_id=c.message.chat.id, ui_message_id=c.message.message_id, return_callback=cancel_return_for(c.data))
    await c.answer()
    await start_qr_login(c.message.bot, c.from_user.id, state)

@router.callback_query(F.data=="acc:add:proxy")
async def acc_add_proxy(c, state: FSMContext):
    if not await check_cb(c): return
    if not c.message or c.message.chat.type != "private":
        await c.answer("Откройте личный чат с ботом.", show_alert=True); return
    if not cfg.telegram_api_id or not cfg.telegram_api_hash:
        await c.answer("Заполните TELEGRAM_API_ID и TELEGRAM_API_HASH в .env.", show_alert=True); return
    if not cfg.proxy_encryption_key.strip():
        await c.answer("Сначала задайте PROXY_ENCRYPTION_KEY в .env и перезапустите приложение.", show_alert=True); return
    await state.clear()
    await state.update_data(ui_chat_id=c.message.chat.id, ui_message_id=c.message.message_id, return_callback=cancel_return_for(c.data), login_method="qr")
    await edit(c, "Выберите тип прокси:", kb([[("SOCKS5", "proxy:type:socks5"), ("HTTP", "proxy:type:http")], [("⬅️ Назад", "acc:add")]]))

@router.callback_query(F.data=="acc:add:phone:direct")
async def acc_add_phone_direct(c: CallbackQuery, state: FSMContext):
    if not await check_cb(c): return
    if not c.message or c.message.chat.type != "private":
        await c.answer("Откройте личный чат с ботом.", show_alert=True); return
    if not cfg.telegram_api_id or not cfg.telegram_api_hash:
        await c.answer("Заполните TELEGRAM_API_ID и TELEGRAM_API_HASH в .env.", show_alert=True); return
    await state.clear()
    await state.update_data(ui_chat_id=c.message.chat.id, ui_message_id=c.message.message_id, return_callback=cancel_return_for(c.data), login_method="phone")
    await c.answer()
    await start_phone_login_prompt(c.message.bot, c.from_user.id, state)

@router.callback_query(F.data=="acc:add:phone:proxy")
async def acc_add_phone_proxy(c: CallbackQuery, state: FSMContext):
    if not await check_cb(c): return
    if not c.message or c.message.chat.type != "private":
        await c.answer("Откройте личный чат с ботом.", show_alert=True); return
    if not cfg.telegram_api_id or not cfg.telegram_api_hash:
        await c.answer("Заполните TELEGRAM_API_ID и TELEGRAM_API_HASH в .env.", show_alert=True); return
    if not cfg.proxy_encryption_key.strip():
        await c.answer("Сначала задайте PROXY_ENCRYPTION_KEY в .env и перезапустите приложение.", show_alert=True); return
    await state.clear()
    await state.update_data(ui_chat_id=c.message.chat.id, ui_message_id=c.message.message_id, return_callback=cancel_return_for(c.data), login_method="phone")
    await edit(c, "Выберите тип прокси:", kb([[("SOCKS5", "proxy:type:socks5"), ("HTTP", "proxy:type:http")], [("⬅️ Назад", "acc:add")]]))

@router.callback_query(F.data.startswith("proxy:type:"))
async def proxy_type(c, state: FSMContext):
    if not await check_cb(c): return
    kind = c.data.rsplit(":", 1)[-1]
    if kind not in {"socks5", "http"}:
        await c.answer("Неизвестный тип прокси.", show_alert=True); return
    existing = await state.get_data()
    await state.update_data(proxy_type=kind, ui_chat_id=c.message.chat.id, ui_message_id=c.message.message_id, return_callback=existing.get("return_callback", "acc:list"), login_method=existing.get("login_method", "qr"))
    await state.set_state(Flow.proxy_host)
    await edit(c, "Введите IP-адрес или домен прокси:", CANCEL)

@router.message(Flow.proxy_host)
async def proxy_host(m: Message, state: FSMContext):
    if not await check_msg(m) or not await private(m): return
    host = (m.text or "").strip()
    if not host or len(host) > 253 or any(ch.isspace() for ch in host) or "://" in host or "/" in host:
        await prompt_message(m, state, "Введите только IP или домен, без протокола и пути."); return
    await delete_msg(m)
    await state.update_data(proxy_host=host)
    await state.set_state(Flow.proxy_port)
    await prompt_message(m, state, "Введите порт прокси (1–65535):")

@router.message(Flow.proxy_port)
async def proxy_port(m: Message, state: FSMContext):
    if not await check_msg(m) or not await private(m): return
    value = (m.text or "").strip()
    if not value.isdigit() or not 1 <= int(value) <= 65535:
        await prompt_message(m, state, "Порт должен быть числом от 1 до 65535."); return
    await delete_msg(m)
    await state.update_data(proxy_port=int(value))
    await state.set_state(Flow.proxy_username)
    await prompt_message(m, state, "Логин прокси или «-», если авторизация не нужна:")

@router.message(Flow.proxy_username)
async def proxy_username(m: Message, state: FSMContext):
    if not await check_msg(m) or not await private(m): return
    value = (m.text or "").strip()
    await delete_msg(m)
    if value == "-":
        data = await state.get_data()
        config = {"type": data["proxy_type"], "host": data["proxy_host"], "port": data["proxy_port"], "username": "", "password": ""}
        if data.get("proxy_account_id"):
            await save_account_proxy(m, state, config)
        else:
            await start_qr_login(m.bot, m.from_user.id, state, config)
        return
    if not value or len(value) > 255:
        await prompt_message(m, state, "Логин должен быть от 1 до 255 символов или «-»."); return
    await state.update_data(proxy_username=value)
    await state.set_state(Flow.proxy_password)
    await prompt_message(m, state, "Введите пароль прокси. Сообщение будет удалено по возможности:")

@router.message(Flow.proxy_password)
async def proxy_password(m: Message, state: FSMContext):
    if not await check_msg(m) or not await private(m): return
    password = (m.text or "").strip()
    await delete_msg(m)
    if not password or len(password) > 255:
        await prompt_message(m, state, "Пароль должен быть от 1 до 255 символов."); return
    data = await state.get_data()
    config = {"type": data["proxy_type"], "host": data["proxy_host"], "port": data["proxy_port"], "username": data["proxy_username"], "password": password}
    if data.get("proxy_account_id"):
        await save_account_proxy(m, state, config)
    else:
        await start_qr_login(m.bot, m.from_user.id, state, config)

async def save_account_proxy(m: Message, state: FSMContext, config: dict):
    data = await state.get_data()
    aid = data.get("proxy_account_id")
    try:
        encrypted = encrypt_proxy(config)
    except ProxyConfigurationError as exc:
        await prompt_message(m, state, str(exc))
        return
    async with SessionFactory() as db:
        account = await db.get(Account, aid)
        if not account:
            await finish_prompt(m, state, "Аккаунт не найден.")
            return
        account.proxy_config_encrypted = encrypted
        account.connection_status = "paused" if account.manual_pause else ("connecting" if account.enabled else "disconnected")
        await db.commit()
    await finish_prompt(m, state, "Прокси сохранён зашифрованно. Worker переподключит аккаунт автоматически.", kb([
        [("🌐 Настройки прокси", f"acc:proxysettings:{aid}")],
        [("👤 К аккаунту", f"acc:view:{aid}"), ("🏠 Главное меню", "nav:home")],
    ]))

async def cleanup_phone_login(uid: int) -> None:
    data = PHONE_LOGIN_DATA.pop(uid, None)
    if not data:
        return
    client = data.get("client")
    if client:
        try:
            await client.disconnect()
        except Exception:
            pass
    if not data.get("success"):
        path = data.get("session_path")
        if path:
            for suffix in (".session", ".session-journal", ".session-wal", ".session-shm"):
                try:
                    os.unlink(str(path) + suffix)
                except FileNotFoundError:
                    pass

async def start_phone_login_prompt(bot, uid: int, state: FSMContext, proxy_config=None):
    if uid in LOGIN_TASKS and not LOGIN_TASKS[uid].done():
        await bot.send_message(uid, "Вход уже запущен. Отмените текущий процесс перед новым.")
        return
    if uid in PHONE_LOGIN_DATA:
        await bot.send_message(uid, "Вход уже запущен. Нажмите «Отмена», прежде чем начинать заново.")
        return
    ui_data = await state.get_data()
    try:
        encrypted = encrypt_proxy(proxy_config) if proxy_config else None
    except ProxyConfigurationError as exc:
        if not await edit_anchor(bot, ui_data, str(exc), MAIN):
            await bot.send_message(uid, str(exc), reply_markup=MAIN)
        return
    PHONE_LOGIN_DATA[uid] = {
        "proxy_config": proxy_config, "encrypted_proxy": encrypted, "client": None,
        "session_path": None, "ui_data": ui_data, "success": False,
    }
    await state.clear()
    await state.update_data(ui_chat_id=ui_data.get("ui_chat_id"), ui_message_id=ui_data.get("ui_message_id"), return_callback=ui_data.get("return_callback"))
    await state.set_state(Flow.phone_number)
    text = "📱 Вход по номеру телефона\n\nВведите номер в международном формате, например +79991234567. Сообщение будет удалено по возможности."
    if not await edit_anchor(bot, ui_data, text, CANCEL):
        await bot.send_message(uid, text, reply_markup=CANCEL)

@router.message(Flow.phone_number)
async def phone_number_received(m: Message, state: FSMContext):
    if not await check_msg(m) or not await private(m): return
    uid = m.from_user.id
    phone = (m.text or "").strip()
    await delete_msg(m)
    if not re.fullmatch(r"\+?[0-9]{7,16}", phone):
        await prompt_message(m, state, "Введите номер в международном формате, например +79991234567.")
        return
    data = PHONE_LOGIN_DATA.get(uid)
    if not data:
        await finish_prompt(m, state, "Сеанс входа истёк. Начните подключение заново.", MAIN)
        return
    folder = cfg.data_dir / "sessions"
    folder.mkdir(parents=True, exist_ok=True)
    os.chmod(folder, 0o700)
    session_path = folder / ("account_" + secrets.token_hex(8))
    client = TelegramClient(str(session_path), cfg.telegram_api_id, cfg.telegram_api_hash, proxy=telethon_proxy(config=data["proxy_config"]))
    configure_telethon_sqlite_session(client)
    data["client"], data["session_path"], data["phone"] = client, session_path, phone
    try:
        await client.connect()
        sent = await client.send_code_request(phone)
        data["phone_code_hash"] = sent.phone_code_hash
        await state.set_state(Flow.phone_code)
        await prompt_message(m, state, "Код отправлен Telegram. Проверьте сообщения от Telegram или SMS и введите код. Сообщение удаляется по возможности.")
    except Exception as exc:
        await cleanup_phone_login(uid)
        await finish_prompt(m, state, f"Не удалось запросить код: {type(exc).__name__}. Проверьте номер и попробуйте снова.", MAIN)

async def save_phone_account(uid: int, state: FSMContext, bot):
    data = PHONE_LOGIN_DATA.get(uid)
    if not data or not data.get("client"):
        await state.clear()
        await bot.send_message(uid, "Сеанс входа истёк. Начните подключение заново.", reply_markup=MAIN)
        return
    client = data["client"]
    me = await client.get_me()
    if not me:
        raise RuntimeError("Telegram не вернул профиль.")
    name = " ".join(x for x in [me.first_name, me.last_name] if x) or str(me.id)
    async with SessionFactory() as db:
        row = await db.scalar(select(Account).where(Account.telegram_user_id == me.id))
        if row:
            row.username, row.display_name, row.session_path, row.enabled = me.username, name, str(data["session_path"]), True
            row.manual_pause = False
            row.connection_status = "connecting"
            row.proxy_config_encrypted = data["encrypted_proxy"]
        else:
            db.add(Account(
                telegram_user_id=me.id, username=me.username, display_name=name,
                session_path=str(data["session_path"]), enabled=True, connection_status="connecting",
                reply_mode="manual", ai_enabled=False, proxy_config_encrypted=data["encrypted_proxy"],
            ))
        await db.commit()
    try:
        os.chmod(str(data["session_path"]) + ".session", 0o600)
    except OSError:
        pass
    data["success"] = True
    ui_data = data.get("ui_data") or await state.get_data()
    await state.clear()
    await cleanup_phone_login(uid)
    text = f"Аккаунт «{name}» подключён по номеру телефона. ИИ выключен до отдельного включения в настройках."
    if not await edit_anchor(bot, ui_data, text, kb([[("👤 Открыть аккаунты", "acc:list")], [("🏠 Главное меню", "nav:home")]])):
        await bot.send_message(uid, text, reply_markup=MAIN)

@router.message(Flow.phone_code)
async def phone_code_received(m: Message, state: FSMContext):
    if not await check_msg(m) or not await private(m): return
    uid = m.from_user.id
    code = (m.text or "").strip().replace(" ", "")
    await delete_msg(m)
    data = PHONE_LOGIN_DATA.get(uid)
    if not data or not data.get("client"):
        await finish_prompt(m, state, "Сеанс входа истёк. Начните подключение заново.", MAIN)
        return
    if not re.fullmatch(r"[0-9]{3,10}", code):
        await prompt_message(m, state, "Введите код Telegram цифрами.")
        return
    try:
        await data["client"].sign_in(phone=data["phone"], code=code, phone_code_hash=data["phone_code_hash"])
        await save_phone_account(uid, state, m.bot)
    except SessionPasswordNeededError:
        await state.set_state(Flow.phone_password)
        await prompt_message(
            m, state,
            "🔐 Telegram запросил пароль двухэтапной аутентификации (2FA).\n\n"
            "Важно: пароль будет доступен владельцу этого бота и сервера. Бот не записывает его в БД "
            "и удаляет сообщение по возможности, но удаление не гарантируется. Продолжайте только "
            "если доверяете этому серверу. Отправьте пароль или нажмите «Отмена»."
        )
    except Exception as exc:
        await prompt_message(m, state, f"Код не принят ({type(exc).__name__}). Проверьте код и попробуйте ещё раз.")

@router.message(Flow.phone_password)
async def phone_password_received(m: Message, state: FSMContext):
    if not await check_msg(m) or not await private(m): return
    uid = m.from_user.id
    password = m.text or ""
    await delete_msg(m)
    data = PHONE_LOGIN_DATA.get(uid)
    if not data or not data.get("client"):
        await finish_prompt(m, state, "Сеанс входа истёк. Начните подключение заново.", MAIN)
        return
    try:
        # Keep the 2FA password only in this stack frame; never log or persist it.
        await data["client"].sign_in(password=password)
        password = ""
        await save_phone_account(uid, state, m.bot)
    except Exception as exc:
        password = ""
        await prompt_message(m, state, f"Пароль 2FA не принят ({type(exc).__name__}). Попробуйте ещё раз или отмените вход.")

async def qr_login(bot, uid, proxy_config=None, encrypted_proxy=None, ui_data=None, state=None):
    folder=cfg.data_dir/"sessions"; folder.mkdir(parents=True,exist_ok=True); os.chmod(folder,0o700)
    session_path=folder/("account_"+secrets.token_hex(8))
    client=TelegramClient(str(session_path),cfg.telegram_api_id,cfg.telegram_api_hash, proxy=telethon_proxy(config=proxy_config))
    configure_telethon_sqlite_session(client)
    success = False
    keep_client = False
    qr_message_id = None
    try:
        await client.connect()
        qr=await client.qr_login()
        import qrcode
        img=qrcode.make(qr.url); stream=io.BytesIO(); img.save(stream,format="PNG")
        qr_message = await bot.send_photo(uid,BufferedInputFile(stream.getvalue(),filename="telegram-login.png"),
            caption="Отсканируйте QR в Telegram: Настройки → Устройства → Подключить устройство. Не пересылайте QR другим людям. Ожидаю подтверждение…")
        qr_message_id = qr_message.message_id
        try:
            await qr.wait(timeout=120)
        except SessionPasswordNeededError:
            if state is None:
                raise RuntimeError("Telegram запросил пароль 2FA, но состояние входа недоступно.")
            QR_LOGIN_DATA[uid] = {
                "client": client, "session_path": session_path, "proxy_config": proxy_config,
                "encrypted_proxy": encrypted_proxy, "ui_data": ui_data or {},
            }
            keep_client = True
            await state.set_state(Flow.qr_password)
            twofa_text = (
                "🔐 Telegram запросил пароль двухэтапной аутентификации (2FA).\n\n"
                "Отправьте пароль в этот личный чат. Бот не сохраняет его в БД и не выводит в логи, "
                "а сообщение удаляется по возможности. Удаление не гарантируется — используйте только "
                "собственный доверенный сервер. Нажмите «Отмена», если передумали."
            )
            if not await edit_anchor(bot, ui_data or {}, twofa_text, CANCEL):
                await bot.send_message(uid, twofa_text, reply_markup=CANCEL)
            return
        me=await client.get_me()
        if not me: raise RuntimeError("Telegram не вернул профиль.")
        name=" ".join(x for x in [me.first_name,me.last_name] if x) or str(me.id)
        async with SessionFactory() as db:
            row=await db.scalar(select(Account).where(Account.telegram_user_id==me.id))
            if row:
                row.username,row.display_name,row.session_path,row.enabled=me.username,name,str(session_path),True
                row.manual_pause=False
                row.connection_status="connecting"
                row.proxy_config_encrypted=encrypted_proxy
            else:
                db.add(Account(telegram_user_id=me.id,username=me.username,display_name=name,session_path=str(session_path),enabled=True,connection_status="connecting",reply_mode="manual",ai_enabled=False,proxy_config_encrypted=encrypted_proxy))
            await db.commit()
        success = True
        try: os.chmod(str(session_path)+".session",0o600)
        except OSError: pass
        result_text = f"Аккаунт «{name}» подключён. ИИ выключен до отдельного включения в настройках."
        if not await edit_anchor(bot, ui_data or {}, result_text, kb([[("👤 Открыть аккаунты", "acc:list")], [("🏠 Главное меню", "nav:home")]])):
            await bot.send_message(uid, result_text, reply_markup=MAIN)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        error_text = f"Не удалось подключить аккаунт: {type(exc).__name__}: {str(exc)[:220]}"
        if not await edit_anchor(bot, ui_data or {}, error_text, MAIN):
            await bot.send_message(uid, error_text, reply_markup=MAIN)
    finally:
        LOGIN_TASKS.pop(uid,None)
        if qr_message_id is not None:
            try: await bot.delete_message(uid, qr_message_id)
            except Exception: pass
        try:
            if not keep_client:
                await client.disconnect()
        finally:
            if not success and not keep_client:
                for suffix in (".session", ".session-journal", ".session-wal", ".session-shm"):
                    try:
                        os.unlink(str(session_path) + suffix)
                    except FileNotFoundError:
                        pass

async def cleanup_qr_login(uid: int) -> None:
    data = QR_LOGIN_DATA.pop(uid, None)
    if not data:
        return
    client = data.get("client")
    if client:
        try:
            await client.disconnect()
        except Exception:
            pass
    path = data.get("session_path")
    if path:
        for suffix in (".session", ".session-journal", ".session-wal", ".session-shm"):
            try:
                os.unlink(str(path) + suffix)
            except FileNotFoundError:
                pass

@router.message(Flow.qr_password)
async def qr_password_received(m: Message, state: FSMContext):
    if not await check_msg(m) or not await private(m): return
    uid = m.from_user.id
    password = m.text or ""
    await delete_msg(m)
    data = QR_LOGIN_DATA.get(uid)
    if not data or not data.get("client"):
        await state.clear()
        await finish_prompt(m, state, "Сеанс QR-входа истёк. Начните подключение заново.", MAIN)
        return
    try:
        await data["client"].sign_in(password=password)
        password = ""
        me = await data["client"].get_me()
        if not me:
            raise RuntimeError("Telegram не вернул профиль.")
        name = " ".join(x for x in [me.first_name, me.last_name] if x) or str(me.id)
        async with SessionFactory() as db:
            row = await db.scalar(select(Account).where(Account.telegram_user_id == me.id))
            if row:
                row.username, row.display_name, row.session_path, row.enabled = me.username, name, str(data["session_path"]), True
                row.manual_pause = False
                row.connection_status = "connecting"
                row.proxy_config_encrypted = data["encrypted_proxy"]
            else:
                db.add(Account(
                    telegram_user_id=me.id, username=me.username, display_name=name,
                    session_path=str(data["session_path"]), enabled=True, connection_status="connecting",
                    reply_mode="manual", ai_enabled=False, proxy_config_encrypted=data["encrypted_proxy"],
                ))
            await db.commit()
        try: os.chmod(str(data["session_path"]) + ".session", 0o600)
        except OSError: pass
        ui_data = data.get("ui_data") or await state.get_data()
        await state.clear()
        QR_LOGIN_DATA.pop(uid, None)
        try: await data["client"].disconnect()
        except Exception: pass
        result_text = f"Аккаунт «{name}» подключён. ИИ выключен до отдельного включения в настройках."
        if not await edit_anchor(m.bot, ui_data, result_text, kb([[("👤 Открыть аккаунты", "acc:list")], [("🏠 Главное меню", "nav:home")]])):
            await m.answer(result_text, reply_markup=MAIN)
    except Exception as exc:
        password = ""
        await prompt_message(m, state, f"Пароль 2FA не принят ({type(exc).__name__}). Попробуйте ещё раз или нажмите «Отмена».")

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
        [("💬📣Встпуление",f"chat:joinlink:{aid}"),
         ((("⏳ Завершение" if a.learning_status == "stopping" else "⏹ Остановить") if learning else "🧠 Автообучение"),(f"learn:stop:{aid}" if learning else f"acc:learn:{aid}"))],
        [("📡 Каналы",f"channel:account:{aid}")],
        [("🎯 Триггеры",f"trigger:account:{aid}")],
        [("⬅️ К аккаунтам","acc:list"),("🏠 Главное меню","nav:home")]]
    await edit(c,txt,kb(buttons))
@router.callback_query(F.data.startswith("acc:proxysettings:"))
async def acc_proxy_settings(c, account_id: int | None = None):
    if not await check_cb(c): return
    aid = account_id if account_id is not None else int(c.data.split(":")[2])
    async with SessionFactory() as db:
        account = await db.get(Account, aid)
    if not account: await c.answer("Аккаунт не найден", show_alert=True); return
    current = "прокси настроен" if account.proxy_config_encrypted else "прямое соединение"
    await edit(c, f"🌐 Настройки прокси аккаунта #{aid}\nТекущее подключение: {current}. Настройки сохраняются зашифрованными.", kb([
        [("🔄 Изменить прокси", f"acc:proxyset:{aid}")],
        [("🚫 Убрать прокси", f"acc:proxyclear:{aid}")],
        [("⬅️ К аккаунту", f"acc:view:{aid}"), ("🏠 Главное меню", "nav:home")],
    ]))

@router.callback_query(F.data.startswith("acc:proxyset:"))
async def acc_proxy_set(c, state: FSMContext):
    if not await check_cb(c): return
    aid = int(c.data.split(":")[2])
    if not cfg.proxy_encryption_key.strip():
        await c.answer("Сначала задайте PROXY_ENCRYPTION_KEY в .env.", show_alert=True); return
    await state.clear()
    await state.update_data(proxy_account_id=aid, ui_chat_id=c.message.chat.id, ui_message_id=c.message.message_id, return_callback=cancel_return_for(c.data))
    await edit(c, "Выберите тип прокси для существующего аккаунта:", kb([
        [("SOCKS5", "proxy:type:socks5"), ("HTTP", "proxy:type:http")],
        [("⬅️ Отмена", f"acc:proxysettings:{aid}")],
    ]))

@router.callback_query(F.data.startswith("acc:proxyclear:"))
async def acc_proxy_clear_confirm(c):
    if not await check_cb(c): return
    aid = int(c.data.split(":")[2])
    await edit(c, f"Убрать прокси у аккаунта #{aid}? После этого worker переподключит его напрямую, если аккаунт включён и не на паузе.", kb([
        [("🚫 Да, убрать прокси", f"acc:proxyclearconfirm:{aid}")],
        [("⬅️ Отмена", f"acc:proxysettings:{aid}")],
    ]))

@router.callback_query(F.data.startswith("acc:proxyclearconfirm:"))
async def acc_proxy_clear(c):
    if not await check_cb(c): return
    aid = int(c.data.split(":")[2])
    async with SessionFactory() as db:
        account = await db.get(Account, aid)
        if not account: await c.answer("Аккаунт не найден", show_alert=True); return
        account.proxy_config_encrypted = None
        account.connection_status = "paused" if account.manual_pause else ("connecting" if account.enabled else "disconnected")
        await db.commit()
    await edit(c, f"Прокси аккаунта #{aid} удалён. Worker переподключится согласно состоянию аккаунта.", kb([
        [("🌐 Настройки прокси", f"acc:proxysettings:{aid}")],
        [("👤 К аккаунту", f"acc:view:{aid}"), ("🏠 Главное меню", "nav:home")],
    ]))

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


@router.callback_query(F.data.startswith("acc:learnprompt:"))
async def acc_learning_prompt(c, account_id: int | None = None):
    if not await check_cb(c): return
    aid = account_id if account_id is not None else int(c.data.split(":")[2])
    async with SessionFactory() as db:
        account = await db.get(Account, aid)
    if not account:
        await c.answer("Аккаунт не найден", show_alert=True); return
    prompt = account.learning_prompt or ""
    shown = prompt[:3000] if prompt.strip() else "Контекст пока не сохранён. Можно запустить обучение или добавить текст вручную."
    training_active = account.learning_status == "training"
    training_stopping = account.learning_status == "stopping"
    buttons = [[("✏️ Редактировать контекст", f"acc:context:edit:{aid}")]]
    if training_active:
        buttons.append([("⏹ Остановить обучение", f"learn:stop:{aid}")])
    elif training_stopping:
        buttons.append([("⏳ Обучение завершается", f"acc:view:{aid}")])
    else:
        buttons.append([("🧠 Запустить / повторить обучение", f"acc:learn:{aid}")])
    if prompt.strip() and not training_active and not training_stopping:
        buttons.append([("🗑 Очистить контекст", f"acc:context:clear:{aid}")])
    buttons.append([("⬅️ К аккаунту", f"acc:view:{aid}"), ("🏠 Главное меню", "nav:home")])
    await edit(c, f"🧠 Контекст обучения · аккаунт #{aid}\n\n{shown}", kb(buttons))

@router.callback_query(F.data.startswith("acc:context:edit:"))
async def acc_context_edit_start(c, state: FSMContext):
    if not await check_cb(c): return
    aid = int(c.data.split(":")[3])
    async with SessionFactory() as db:
        account = await db.get(Account, aid)
    if not account:
        await c.answer("Аккаунт не найден", show_alert=True); return
    if account.learning_status in {"training", "stopping"}:
        await c.answer("Сначала завершите или остановите обучение, чтобы не потерять изменения.", show_alert=True); return
    await state.clear()
    await state.update_data(ui_chat_id=c.message.chat.id, ui_message_id=c.message.message_id, return_callback=cancel_return_for(c.data), context_account_id=aid)
    await state.set_state(Flow.learning_prompt_edit)
    await edit(c, f"Редактирование контекста аккаунта #{aid}\nОтправьте новый текст одним сообщением (до 3500 символов). Старый контекст останется, пока вы не отправите новый. Для отмены нажмите кнопку ниже.", CANCEL)

@router.message(Flow.learning_prompt_edit)
async def acc_context_edit_save(m: Message, state: FSMContext):
    if not await check_msg(m) or not await private(m): return
    value = (m.text or "").strip()
    data = await state.get_data()
    if not value or len(value) > 3500:
        await prompt_message(m, state, "Контекст должен содержать от 1 до 3500 символов. Отправьте исправленный текст или нажмите «Отмена».")
        return
    async with SessionFactory() as db:
        account = await db.get(Account, data["context_account_id"])
        if not account:
            await finish_prompt(m, state, "Аккаунт не найден.", kb([[("🏠 Главное меню", "nav:home")]])); return
        account.learning_prompt = value
        await db.commit()
    aid = data["context_account_id"]
    await finish_prompt(m, state, "Контекст сохранён. Он применяется только к этому аккаунту.", kb([
        [("📋 Открыть контекст", f"acc:learnprompt:{aid}")],
        [("👤 К аккаунту", f"acc:view:{aid}"), ("🏠 Главное меню", "nav:home")],
    ]))

@router.callback_query(F.data.startswith("acc:context:clear:"))
async def acc_context_clear_confirm(c):
    if not await check_cb(c): return
    aid = int(c.data.split(":")[3])
    async with SessionFactory() as db:
        account = await db.get(Account, aid)
    if not account:
        await c.answer("Аккаунт не найден", show_alert=True); return
    if account.learning_status in {"training", "stopping"}:
        await c.answer("Сначала завершите или остановите обучение.", show_alert=True); return
    await edit(c, f"Очистить сохранённый контекст обучения аккаунта #{aid}? Переписка в Telegram не удаляется.", kb([
        [("🗑 Да, очистить", f"acc:context:clearconfirm:{aid}")],
        [("⬅️ Отмена", f"acc:learnprompt:{aid}")],
    ]))

@router.callback_query(F.data.startswith("acc:context:clearconfirm:"))
async def acc_context_clear(c):
    if not await check_cb(c): return
    aid = int(c.data.split(":")[3])
    async with SessionFactory() as db:
        account = await db.get(Account, aid)
        if not account: await c.answer("Аккаунт не найден", show_alert=True); return
        if account.learning_status in {"training", "stopping"}:
            await c.answer("Обучение уже идёт; контекст не очищен.", show_alert=True); return
        account.learning_prompt = ""
        await db.commit()
    await edit(c, f"Контекст аккаунта #{aid} очищен.", kb([
        [("🧠 Открыть контекст", f"acc:learnprompt:{aid}")],
        [("👤 К аккаунту", f"acc:view:{aid}"), ("🏠 Главное меню", "nav:home")],
    ]))

@router.callback_query(F.data.startswith("acc:personaoverride:"))
async def acc_persona_override_start(c, state: FSMContext):
    if not await check_cb(c): return
    aid = int(c.data.split(":")[2])
    async with SessionFactory() as db:
        account = await db.get(Account, aid)
    if not account: await c.answer("Аккаунт не найден", show_alert=True); return
    await state.clear()
    await state.update_data(ui_chat_id=c.message.chat.id, ui_message_id=c.message.message_id, return_callback=cancel_return_for(c.data), persona_override_account_id=aid)
    await state.set_state(Flow.persona_override)
    current = account.persona_style_override or "не заданы"
    await edit(c, f"Индивидуальные правила аккаунта #{aid}\n\nТекущие: {current[:1200]}\n\nОтправьте новый текст до 3500 символов. Для очистки отправьте /clear.", CANCEL)

@router.message(Flow.persona_override)
async def acc_persona_override_save(m: Message, state: FSMContext):
    if not await check_msg(m) or not await private(m): return
    value = (m.text or "").strip()
    data = await state.get_data()
    if value == "/clear": value = ""
    elif not value or len(value) > 3500:
        await prompt_message(m, state, "Введите от 1 до 3500 символов или /clear для очистки.")
        return
    async with SessionFactory() as db:
        account = await db.get(Account, data["persona_override_account_id"])
        if not account:
            await finish_prompt(m, state, "Аккаунт не найден.", kb([[("🏠 Главное меню", "nav:home")]])); return
        account.persona_style_override = value
        await db.commit()
    aid = data["persona_override_account_id"]
    await finish_prompt(m, state, "Индивидуальные правила сохранены.", kb([
        [("👤 К аккаунту", f"acc:view:{aid}")],
        [("🏠 Главное меню", "nav:home")],
    ]))

@router.callback_query(F.data.startswith("acc:learn:"))
async def acc_learning_menu(c, account_id: int | None = None):
    if not await check_cb(c): return
    aid = account_id if account_id is not None else int(c.data.split(":")[2])
    async with SessionFactory() as db:
        account = await db.get(Account, aid)
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
        chat_count = sum(1 for chat_id in active_chat_ids if chat_id not in excluded_chat_ids)
        enabled_agent = await db.scalar(select(AIAgent.id).where(AIAgent.enabled.is_(True)))
        ai_on = await global_ai_enabled(db)
    if not account:
        await c.answer("Аккаунт не найден", show_alert=True); return
    learning = account.learning_status in {"training", "stopping"}
    requirements = []
    if not account.enabled: requirements.append("включите аккаунт")
    if account.manual_pause: requirements.append("возобновите работу аккаунта")
    if account.reply_mode != "manual": requirements.append("переключите режим на «Ручной»")
    if not chat_count: requirements.append("загрузите чаты и включите хотя бы один")
    if not ai_on or not enabled_agent: requirements.append("включите глобальный ИИ и настройте ИИ-агента")
    lines = [f"🧠 Автообучение · аккаунт #{aid}", "", "Контекст: сохранён" if account.learning_prompt.strip() else "Контекст: пока не задан"]
    if learning:
        lines.append("Статус: обучение выполняется." if account.learning_status == "training" else "Статус: обучение останавливается.")
    elif requirements:
        lines += ["", "Для запуска необходимо:"] + [f"• {item}" for item in requirements]
    else:
        lines += ["", "Готово к запуску. Перед стартом подтвердите право передавать сообщения выбранному ИИ-провайдеру."]
    rows = [[("🧠 Контекст обучения", f"acc:learnprompt:{aid}")]]
    if learning: rows.append([("⏹ Остановить обучение", f"learn:stop:{aid}")])
    elif not requirements: rows.append([("▶️ Запустить автообучение", f"learn:confirm:{aid}")])
    rows.append([("⬅️ К аккаунту", f"acc:view:{aid}")])
    await edit(c, "\n".join(lines), kb(rows))


@router.callback_query(F.data.startswith("learn:confirm:"))
async def acc_learning_confirm(c):
    if not await check_cb(c): return
    aid = int(c.data.split(":")[2])
    buttons = [
        [("Изучить последние 500 сообщений", f"learn:start:{aid}:500")],
        [("Изучить последние 1000 сообщений", f"learn:start:{aid}:1000")],
        [("⬅️ Назад", f"acc:view:{aid}")],
    ]
    await edit(c, f"Выберите объём выборки для аккаунта #{aid}. Будут собраны последние 500 или 1000 подходящих текстовых сообщений по всем доступным чатам. Временные данные удаляются после завершения.", kb(buttons))

@router.callback_query(F.data.startswith("learn:start:"))
async def acc_learning_start(c):
    if not await check_cb(c): return
    _, _, aid_s, limit_s = c.data.split(":")
    aid, limit = int(aid_s), int(limit_s)
    if limit not in {500, 1000}:
        await c.answer("Недопустимый лимит", show_alert=True); return
    now = datetime.now(timezone.utc)
    async with SessionFactory() as db:
        account = await db.get(Account, aid)
        chats = list((await db.scalars(select(ChatBinding).where(
            ChatBinding.account_id == aid, ChatBinding.enabled.is_(True)
        ))).all())
        discussion_ids = set((await db.scalars(select(ChannelBinding.discussion_group_id).where(
            ChannelBinding.account_id == aid, ChannelBinding.discussion_group_id.is_not(None)
        ))).all())
        channel_ids = set((await db.scalars(select(ChannelBinding.channel_id).where(
            ChannelBinding.account_id == aid
        ))).all())
        excluded_chat_ids = discussion_ids | channel_ids
        chats = [binding for binding in chats if int(binding.chat_id) not in excluded_chat_ids]
        if not account or not account.enabled:
            await c.answer("Аккаунт не найден или выключен", show_alert=True); return
        if account.manual_pause:
            await c.answer("Сначала возобновите работу аккаунта.", show_alert=True); return
        if not chats:
            await c.answer("Список диалогов ещё не загружен. Откройте «Чаты аккаунта» и повторите попытку.", show_alert=True); return
        if account.learning_status in {"training", "stopping"}:
            await c.answer("Обучение уже запущено", show_alert=True); return
        if account.reply_mode != "manual":
            await c.answer("Перед обучением переключите режим ответов на «Ручной».", show_alert=True); return
        if not await global_ai_enabled(db):
            await c.answer("Сначала включите глобальный ИИ", show_alert=True); return
        agent = await db.get(AIAgent, account.ai_agent_id) if account.ai_agent_id else None
        if not agent or not agent.enabled or not agent.api_key.strip():
            agent = await db.scalar(select(AIAgent).where(
                AIAgent.enabled.is_(True), AIAgent.is_default.is_(True)
            ).order_by(AIAgent.id))
        if not agent or not agent.enabled or not agent.api_key.strip():
            agent = await db.scalar(select(AIAgent).where(
                AIAgent.enabled.is_(True)
            ).order_by(AIAgent.id))
        if not agent or not agent.api_key.strip():
            await c.answer("Настройте включённого ИИ-агента с API-ключом", show_alert=True); return
        account.learning_status = "training"
        account.learning_started_at = now
        from datetime import timedelta
        account.learning_ends_at = now + timedelta(days=3)
        account.learning_limit = limit
        await db.commit()
    await edit(c, f"🧠 Обучение аккаунта #{aid} запущено. Лимит: {limit} сообщений. Автоостановка — через 3 дня.", kb([
        [("⏹ Закончить обучение", f"learn:stop:{aid}")],
        [("⬅️ К аккаунту", f"acc:view:{aid}")],
    ]))

@router.callback_query(F.data.startswith("learn:stop:"))
async def acc_learning_stop(c):
    if not await check_cb(c): return
    aid = int(c.data.split(":")[2])
    async with SessionFactory() as db:
        account = await db.get(Account, aid)
        if not account:
            await c.answer("Аккаунт не найден", show_alert=True); return
        if account.learning_status == "training":
            account.learning_status = "stopping"
            await db.commit()
    await edit(c, f"⏳ Обучение аккаунта #{aid} останавливается. После сводки временные сообщения будут удалены.", kb([
        [("⬅️ К аккаунту", f"acc:view:{aid}")],
        [("📚 Список аккаунтов", "acc:list")],
    ]))

# AI agent management
@router.message(F.text=="🤖 ИИ-агенты")
async def agents(m):
    if not await check_msg(m): return
    await agents_screen(m)
async def agents_screen(m):
    async with SessionFactory() as db:
        aa=list((await db.scalars(select(AIAgent).order_by(AIAgent.id))).all())
        on=await global_ai_enabled(db)
        enabled_count=len((await db.scalars(select(Account).where(Account.ai_enabled.is_(True)))).all())
        total_accounts=len((await db.scalars(select(Account))).all())
    all_accounts_ai=total_accounts > 0 and enabled_count == total_accounts
    buttons=[[(f"{'⭐ ' if a.is_default else ''}{'🟢' if a.enabled else '⚪'} {a.name}",f"agent:view:{a.id}")] for a in aa]
    buttons += [[(("🟢 Выключить ИИ для всех" if on else "🤖 Включить ИИ для всех"),"agent:global")],
                [("⏸ Выключить ИИ на аккаунтах" if all_accounts_ai else "🤖 Включить ИИ на аккаунтах","agent:accounts_toggle")],
                [("🔀 Назначить агент всем аккаунтам","agent:assignall")],
                [("➕ Добавить ИИ-агента","agent:add")], [("⬅️ Главное меню","nav:home")]]
    await m.answer(f"🤖 ИИ-агенты\nГлобально: {'включён' if on else 'выключен'}\nАккаунтов с локальным ИИ: {enabled_count}",reply_markup=kb(buttons))
@router.callback_query(F.data=="agent:menu")
async def agents_cb(c):
    if not await check_cb(c): return
    async with SessionFactory() as db:
        aa=list((await db.scalars(select(AIAgent).order_by(AIAgent.id))).all())
        on=await global_ai_enabled(db)
        enabled_count=len((await db.scalars(select(Account).where(Account.ai_enabled.is_(True)))).all())
        total_accounts=len((await db.scalars(select(Account))).all())
    all_accounts_ai=total_accounts > 0 and enabled_count == total_accounts
    buttons=[[(f"{'⭐ ' if a.is_default else ''}{'🟢' if a.enabled else '⚪'} {a.name}",f"agent:view:{a.id}")] for a in aa]
    buttons += [[(("🟢 Выключить ИИ для всех" if on else "🤖 Включить ИИ для всех"),"agent:global")],
                [("⏸ Выключить ИИ на аккаунтах" if all_accounts_ai else "🤖 Включить ИИ на аккаунтах","agent:accounts_toggle")],
                [("🔀 Назначить агент всем аккаунтам","agent:assignall")],
                [("➕ Добавить ИИ-агента","agent:add")], [("⬅️ Главное меню","nav:home")]]
    await edit(c,f"🤖 ИИ-агенты · глобально {'включён' if on else 'выключен'}",kb(buttons))
@router.callback_query(F.data=="agent:accounts_toggle")
async def accounts_ai_toggle(c):
    if not await check_cb(c): return
    async with SessionFactory() as db:
        accounts=list((await db.scalars(select(Account))).all())
        enable=not bool(accounts) or not all(a.ai_enabled for a in accounts)
        for account in accounts:
            account.ai_enabled=enable
        await db.commit()
    await agents_cb(c)

@router.callback_query(F.data=="agent:global")
async def global_toggle(c):
    if not await check_cb(c): return
    async with SessionFactory() as db:
        s=await db.get(AppSetting,"ai_global_enabled")
        if not s: s=AppSetting(key="ai_global_enabled",value="false"); db.add(s)
        s.value="false" if s.value.lower()=="true" else "true"; await db.commit()
    await agents_cb(c)
@router.callback_query(F.data=="agent:add")
async def agent_add(c,state):
    if not await check_cb(c): return
    await state.clear()
    await state.update_data(ui_chat_id=c.message.chat.id, ui_message_id=c.message.message_id, return_callback=cancel_return_for(c.data))
    await state.set_state(Flow.agent_name)
    await edit(c, "Введите название профиля, например Groq или OpenAI.", CANCEL)
@router.message(Flow.agent_name)
async def agent_name(m,state):
    if not await check_msg(m) or not await private(m): return
    name=(m.text or "").strip()
    if not name or len(name)>80: await prompt_message(m,state,"Название должно содержать от 1 до 80 символов."); return
    async with SessionFactory() as db:
        if await db.scalar(select(AIAgent.id).where(AIAgent.name==name)): await prompt_message(m,state,"Такое название уже есть."); return
    await delete_msg(m)
    await state.update_data(name=name); await state.set_state(Flow.agent_url)
    await prompt_message(m,state,"Введите Base URL API, например https://api.openai.com/v1")
@router.message(Flow.agent_url)
async def agent_url(m,state):
    if not await check_msg(m) or not await private(m): return
    v=(m.text or "").strip().rstrip("/")
    if not v.startswith("https://") and not (v.startswith("http://") and "localhost" in v): await prompt_message(m,state,"Нужен HTTPS URL (HTTP только localhost)."); return
    await delete_msg(m)
    await state.update_data(url=v); await state.set_state(Flow.agent_key); await prompt_message(m,state,"Введите API-ключ. Сообщение будет удалено по возможности.")
@router.message(Flow.agent_key)
async def agent_key(m,state):
    if not await check_msg(m) or not await private(m): return
    v=(m.text or "").strip(); await delete_msg(m)
    if len(v)<5 or len(v)>1000: await prompt_message(m,state,"Некорректная длина ключа."); return
    await state.update_data(key=v); await state.set_state(Flow.agent_model); await prompt_message(m,state,"Введите точное имя модели.")
@router.message(Flow.agent_model)
async def agent_model(m,state):
    if not await check_msg(m) or not await private(m): return
    v=(m.text or "").strip()
    if not v or len(v)>160: await prompt_message(m,state,"Некорректное имя модели."); return
    await delete_msg(m)
    await state.update_data(model=v); await state.set_state(Flow.agent_timeout); await prompt_message(m,state,"Введите тайм-аут в секундах от 10 до 180.")
@router.message(Flow.agent_timeout)
async def agent_timeout(m,state):
    if not await check_msg(m) or not await private(m): return
    v=(m.text or "").strip()
    if not v.isdigit() or not 10<=int(v)<=180: await prompt_message(m,state,"Введите число от 10 до 180."); return
    d=await state.get_data()
    async with SessionFactory() as db:
        has=bool(await db.scalar(select(AIAgent.id)))
        a=AIAgent(name=d["name"],base_url=d["url"],api_key=d["key"],model=d["model"],timeout_seconds=int(v),is_default=not has)
        db.add(a); await db.commit(); await db.refresh(a)
    await finish_prompt(m,state,f"Профиль «{a.name}» сохранён.",kb([[("🤖 К ИИ-агентам","agent:menu")],[("🏠 Главное меню","nav:home")]]))
@router.callback_query(F.data=="agent:assignall")
async def assign_all_menu(c):
    if not await check_cb(c): return
    async with SessionFactory() as db: aa=list((await db.scalars(select(AIAgent).where(AIAgent.enabled.is_(True)))).all())
    await edit(c,"Выберите агент для всех аккаунтов:",kb([[ (a.name,f"agent:assignall:{a.id}") ] for a in aa]+[[("⬅️ Назад","agent:menu")]]))
@router.callback_query(F.data.startswith("agent:assignall:"))
async def assign_all(c):
    if not await check_cb(c): return
    aid=int(c.data.split(":")[2])
    async with SessionFactory() as db:
        a=await db.get(AIAgent,aid)
        if not a or not a.enabled: await c.answer("Агент не найден или выключен",show_alert=True); return
        for account in (await db.scalars(select(Account))).all(): account.ai_agent_id=aid
        for other in (await db.scalars(select(AIAgent))).all(): other.is_default=other.id==aid
        await db.commit()
    await edit(c,f"«{a.name}» назначен всем аккаунтам. Локальные переключатели ИИ не менялись.",back_home())
@router.callback_query(F.data.startswith("agent:view:"))
async def agent_view(c, agent_id: int | None = None):
    if not await check_cb(c): return
    aid=agent_id if agent_id is not None else int(c.data.split(":")[2])
    async with SessionFactory() as db:
        a=await db.get(AIAgent,aid)
        if not a: await c.answer("Агент не найден",show_alert=True); return
        assigned=list((await db.scalars(select(Account).where(Account.ai_agent_id==aid))).all())
    masked=("•"*min(12,len(a.api_key))) if a.api_key else "не задан"
    txt=f"🤖 {a.name}\nСтатус: {'включён' if a.enabled else 'выключен'}\nПо умолчанию: {'да' if a.is_default else 'нет'}\nBase URL: {a.base_url}\nМодель: {a.model}\nAPI-ключ: {masked}\nТайм-аут: {a.timeout_seconds} сек.\nАккаунты: {', '.join('#'+str(x.id) for x in assigned) or 'нет'}"
    buttons=[[(("⏸ Выключить профиль" if a.enabled else "▶️ Включить профиль"),f"agent:toggle:{aid}")],
        [("⭐ По умолчанию",f"agent:default:{aid}")]]
    if a.is_default:
        buttons.append([("✖️ Убрать статус по умолчанию",f"agent:unsetdefault:{aid}")])
    buttons.extend([
        [("🎚 Вероятности и паузы",f"agent:runtime:menu:{aid}")],
        [("✏️ Base URL",f"agent:edit:url:{aid}"),("✏️ Модель",f"agent:edit:model:{aid}")],
        [("🔑 API-ключ",f"agent:edit:key:{aid}"),("⏱ Тайм-аут",f"agent:edit:timeout:{aid}")],
        [("🗑 Удалить профиль",f"agent:delete:{aid}")],[("⬅️ К ИИ-агентам","agent:menu")]])
    await edit(c,txt,kb(buttons))
@router.callback_query(F.data.startswith("agent:toggle:"))
async def agent_toggle(c):
    if not await check_cb(c): return
    aid=int(c.data.split(":")[2])
    async with SessionFactory() as db:
        a=await db.get(AIAgent,aid)
        if not a: await c.answer("Агент не найден",show_alert=True); return
        a.enabled=not a.enabled; await db.commit()
    await agent_view(c)
@router.callback_query(F.data.startswith("agent:default:"))
async def agent_default(c):
    if not await check_cb(c): return
    aid=int(c.data.split(":")[2])
    async with SessionFactory() as db:
        a=await db.get(AIAgent,aid)
        if not a or not a.enabled: await c.answer("Сначала включите агента",show_alert=True); return
        for x in (await db.scalars(select(AIAgent))).all(): x.is_default=x.id==aid
        await db.commit()
    await agent_view(c)
@router.callback_query(F.data.startswith("agent:unsetdefault:"))
async def agent_unset_default(c):
    if not await check_cb(c): return
    aid=int(c.data.split(":")[2])
    async with SessionFactory() as db:
        a=await db.get(AIAgent,aid)
        if not a: await c.answer("Агент не найден",show_alert=True); return
        if not a.is_default:
            await c.answer("Этот агент уже не является агентом по умолчанию",show_alert=True)
            await agent_view(c)
            return
        a.is_default=False
        await db.commit()
    await agent_view(c)

@router.callback_query(F.data.startswith("agent:edit:"))
async def agent_edit_start(c,state):
    if not await check_cb(c): return
    _,_,field,aid=c.data.split(":")
    await state.clear(); await state.update_data(edit_field=field,edit_aid=int(aid),ui_chat_id=c.message.chat.id,ui_message_id=c.message.message_id, return_callback=cancel_return_for(c.data)); await state.set_state(Flow.agent_edit)
    await edit(c, "Введите новое значение для "+field+". Для ключа сообщение будет удалено по возможности.", CANCEL)
@router.message(Flow.agent_edit)
async def agent_edit_save(m,state):
    if not await check_msg(m) or not await private(m): return
    d=await state.get_data(); field=d["edit_field"]; v=(m.text or "").strip()
    if field=="key":
        await delete_msg(m)
        if len(v)<5 or len(v)>1000: await prompt_message(m,state,"Некорректный ключ."); return
    elif field=="url":
        v=v.rstrip("/")
        if not v.startswith("https://") and not (v.startswith("http://") and "localhost" in v): await prompt_message(m,state,"Нужен HTTPS URL."); return
    elif field=="model":
        if not v or len(v)>160: await prompt_message(m,state,"Некорректная модель."); return
    elif field=="timeout":
        if not v.isdigit() or not 10<=int(v)<=180: await prompt_message(m,state,"Введите число от 10 до 180."); return
        v=int(v)
    attr={"url":"base_url","model":"model","key":"api_key","timeout":"timeout_seconds"}[field]
    async with SessionFactory() as db:
        a=await db.get(AIAgent,d["edit_aid"])
        if not a: await finish_prompt(m,state,"Агент не найден."); return
        setattr(a,attr,v); await db.commit()
    await finish_prompt(m,state,"Настройки сохранены.",kb([[("🤖 К ИИ-агентам","agent:menu")],[("🏠 Главное меню","nav:home")]]))
@router.callback_query(F.data.startswith("agent:delete:"))
async def agent_delete(c):
    if not await check_cb(c): return
    aid=int(c.data.split(":")[2])
    async with SessionFactory() as db:
        a=await db.get(AIAgent,aid)
        if not a: await c.answer("Агент не найден",show_alert=True); return
        was=a.is_default
        for acc in (await db.scalars(select(Account).where(Account.ai_agent_id==aid))).all(): acc.ai_agent_id=None; acc.ai_enabled=False
        await db.delete(a); await db.flush()
        if was:
            replacement=await db.scalar(select(AIAgent).where(AIAgent.enabled.is_(True)).order_by(AIAgent.id))
            if replacement: replacement.is_default=True
        await db.commit()
    await agents_cb(c)

# Personas
@router.message(F.text=="🎭 Персоны")
async def personas(m):
    if not await check_msg(m): return
    await persona_screen(m)
async def persona_screen(m):
    async with SessionFactory() as db: ps=list((await db.scalars(select(Persona).order_by(Persona.id))).all())
    await m.answer("🎭 Персоны\nВыберите персону." if ps else "Персон пока нет. Добавьте первую.",reply_markup=kb([[(p.name,f"persona:view:{p.id}")] for p in ps]+[[("➕ Добавить персону","persona:add")],[("⬅️ Главное меню","nav:home")]]))
@router.callback_query(F.data=="persona:menu")
async def persona_menu(c):
    if not await check_cb(c): return
    async with SessionFactory() as db: ps=list((await db.scalars(select(Persona).order_by(Persona.id))).all())
    await edit(c,"🎭 Персоны",kb([[(p.name,f"persona:view:{p.id}")] for p in ps]+[[("➕ Добавить персону","persona:add")],[("⬅️ Главное меню","nav:home")]]))
@router.callback_query(F.data=="persona:add")
async def persona_add(c,state):
    if not await check_cb(c): return
    await state.clear()
    await state.update_data(ui_chat_id=c.message.chat.id, ui_message_id=c.message.message_id, return_callback=cancel_return_for(c.data))
    await state.set_state(Flow.persona_name)
    await edit(c, "Введите название персоны.", CANCEL)
@router.message(Flow.persona_name)
async def persona_name(m,state):
    if not await check_msg(m) or not await private(m): return
    name=(m.text or "").strip()
    if not name or len(name)>80: await prompt_message(m,state,"Название должно содержать от 1 до 80 символов."); return
    async with SessionFactory() as db:
        if await db.scalar(select(Persona.id).where(Persona.name==name)): await prompt_message(m,state,"Такое имя уже есть."); return
    await delete_msg(m)
    await state.update_data(persona_name=name); await state.set_state(Flow.persona_style); await prompt_message(m,state,"Введите инструкции стиля общения.")
@router.message(Flow.persona_style)
async def persona_style(m,state):
    if not await check_msg(m) or not await private(m): return
    style=(m.text or "").strip()
    if not style or len(style)>5000: await prompt_message(m,state,"Текст должен содержать от 1 до 5000 символов."); return
    d=await state.get_data()
    async with SessionFactory() as db:
        if d.get("persona_edit_id"):
            p = await db.get(Persona, d["persona_edit_id"])
            if not p:
                await finish_prompt(m, state, "Персона не найдена.")
                return
            p.style_instructions = style
            label = "Общая персона обновлена. Изменения применены ко всем использующим её аккаунтам."
        else:
            p=Persona(name=d["persona_name"],description="Создана через бот",style_instructions=style)
            db.add(p)
            label = "Персона сохранена."
        await db.commit()
        await db.refresh(p)
    await finish_prompt(m, state, label, kb([[("🎭 К персонам","persona:menu")],[("🏠 Главное меню","nav:home")]]))
@router.callback_query(F.data.startswith("persona:view:"))
async def persona_view(c, persona_id: int | None = None):
    if not await check_cb(c): return
    pid=persona_id if persona_id is not None else int(c.data.split(":")[2])
    async with SessionFactory() as db:
        p=await db.get(Persona,pid); n=len((await db.scalars(select(Example).where(Example.persona_id==pid))).all())
    if not p: await c.answer("Персона не найдена",show_alert=True); return
    await edit(c,f"🎭 {p.name}\n\n{p.style_instructions[:2200]}\n\nПримеров: {n}",kb([[("✏️ Изменить стиль",f"persona:edit:{pid}")],[("🧩 Добавить пример",f"example:add:{pid}")],[("🗑 Удалить персону",f"persona:delete:{pid}")],[("⬅️ К персонам","persona:menu"),("🏠 Главное меню","nav:home")]]))
@router.callback_query(F.data.startswith("persona:edit:"))
async def persona_edit_start(c, state: FSMContext):
    if not await check_cb(c): return
    pid = int(c.data.split(":")[2])
    async with SessionFactory() as db:
        persona = await db.get(Persona, pid)
    if not persona: await c.answer("Персона не найдена", show_alert=True); return
    await state.clear()
    await state.update_data(persona_edit_id=pid, ui_chat_id=c.message.chat.id, ui_message_id=c.message.message_id, return_callback=cancel_return_for(c.data))
    await state.set_state(Flow.persona_style)
    await edit(c, f"Изменение общей персоны «{persona.name}». Изменения сразу повлияют на все аккаунты, которые её используют. Отправьте новый стиль (до 5000 символов).", CANCEL)

@router.callback_query(F.data.startswith("persona:delete:"))
async def persona_delete(c):
    if not await check_cb(c): return
    pid=int(c.data.split(":")[2])
    async with SessionFactory() as db:
        p=await db.get(Persona,pid)
        if p:
            for a in (await db.scalars(select(Account).where(Account.persona_id==pid))).all(): a.persona_id=None
            await db.delete(p); await db.commit()
    await persona_menu(c)

# Examples
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

# Chat discovery and joining
def _parse_chat_link(raw: str) -> tuple[str, str]:
    value = (raw or "").strip().strip("<>[](){}.,;")
    if value.startswith("@") and re.fullmatch(r"@[A-Za-z][A-Za-z0-9_]{4,31}", value):
        return "public", value[1:]
    match = re.search(r"(?i)(?:https?://)?(?:www\.)?(?:t\.me|telegram\.me)/(joinchat/[A-Za-z0-9_-]+|\+?[A-Za-z0-9_+-]+)", value)
    if not match:
        raise ValueError("Пришлите публичную ссылку t.me/username или пригласительную ссылку t.me/+…")
    path = match.group(1).split("?")[0].rstrip("/")
    if path.startswith("+"):
        return "invite", path[1:]
    if path.startswith("joinchat/"):
        return "invite", path.split("/", 1)[1]
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{4,31}", path):
        raise ValueError("Не удалось распознать ссылку. Нужна ссылка на группу Telegram.")
    return "public", path


def _group_entity(entity) -> bool:
    if getattr(entity, "broadcast", False):
        return False
    return bool(getattr(entity, "megagroup", False) or getattr(entity, "gigagroup", False) or entity.__class__.__name__ == "Chat")


def _candidate_keyboard(aid: int, candidates: list[dict], offset: int = 0):
    rows = []
    page = candidates[offset:offset + 12]
    for i, item in enumerate(page, start=offset):
        title = str(item.get("title") or "Группа")[:30]
        members = int(item.get("members") or 0)
        suffix = f" · {members:,}".replace(",", " ") if members else ""
        rows.append([(f"🔎 {title}{suffix}"[:60], f"chat:candidate:{aid}:{i}")])
    navigation = []
    if offset > 0:
        navigation.append(("⬅️ Назад", f"chat:page:{aid}:{max(0, offset - 12)}"))
    if offset + 12 < len(candidates):
        navigation.append(("➡️ Ещё результаты", f"chat:page:{aid}:{offset + 12}"))
    if navigation:
        rows.append(navigation)
    if any(item.get("verified") and item.get("link") for item in candidates):
        rows.append([("🔗 Показать ссылками", f"chat:links:{aid}")])
    rows += [[("⬅️ Инструменты", "tool:menu"), ("🏠 Главное меню", "nav:home")]]
    return kb(rows)


@asynccontextmanager
async def isolated_account_client(aid: int):
    """Open an authorized account through a temporary session snapshot."""
    if not cfg.telegram_api_id or not cfg.telegram_api_hash:
        raise RuntimeError("Не заданы TELEGRAM_API_ID и TELEGRAM_API_HASH.")
    async with SessionFactory() as db:
        account = await db.get(Account, aid)
    if not account or not account.enabled:
        raise RuntimeError("Аккаунт не найден или отключён.")
    async with isolated_telegram_client(account) as client:
        if not await client.is_user_authorized():
            raise RuntimeError("Сессия аккаунта не авторизована.")
        yield account, client

async def _verify_group_candidates_isolated(aid: int, candidates: list[dict], concurrency: int = 4):
    """Verify search results on a snapshot, never opening the worker's live session."""
    async with isolated_account_client(aid) as (_account, client):
        return await verify_group_candidates(client, candidates, concurrency=concurrency)


@router.callback_query(F.data.startswith("chat:search:"))
async def chat_search_start(c, state: FSMContext):
    if not await check_cb(c): return
    aid = int(c.data.split(":")[2])
    await state.clear()
    await state.update_data(
        ui_chat_id=c.message.chat.id,
        ui_message_id=c.message.message_id,
        return_callback="tool:menu",
        chat_search_account_id=aid,
    )
    await state.set_state(Flow.chat_search_query)
    await edit(
        c,
        f"🔎 Поиск Telegram-групп · аккаунт #{aid}\n\n"
        "Введите тему или несколько ключевых слов, например: «Python разработка», "
        "«настольные игры», «дизайн».\n\n"
        "Поиск собирает кандидатов из Telegram и нескольких поисковых систем, затем "
        "проверяет каждую ссылку через Telegram. В выдаче остаются только группы и супергруппы; "
        "каналы, профили, боты и нерабочие ссылки исключаются.",
        CANCEL,
    )


@router.message(Flow.chat_search_query)
async def chat_search_query(m: Message, state: FSMContext):
    if not await check_msg(m) or not await private(m): return
    data = await state.get_data()
    query = " ".join((m.text or "").split())
    await delete_msg(m)
    if len(query) < 2:
        await prompt_message(m, state, "Введите хотя бы два символа для поиска.")
        return
    query = query[:100]
    aid = int(data.get("chat_search_account_id", 0))
    account_ids = list(dict.fromkeys(
        int(value) for value in (data.get("chat_search_account_ids") or [aid])
        if str(value).isdigit()
    ))
    if aid not in account_ids:
        account_ids.insert(0, aid)
    anchor = {"ui_chat_id": data.get("ui_chat_id"), "ui_message_id": data.get("ui_message_id")}
    await state.clear()
    await edit_anchor(
        m.bot,
        anchor,
        f"🔎 Ищу группы по запросу «{query}»\n\n"
        f"Telegram-поиск: {len(account_ids)} аккаунт(ов)\n"
        "1/3 · Ищу через подключённые аккаунты и несколько поисковиков…\n"
        "2/3 · Объединяю результаты и проверяю ссылки через Telegram API.\n"
        "Если поисковик ограничивает запросы, остальные продолжают работу.",
        kb([[("⬅️ Инструменты", "tool:menu")]]),
    )

    try:
        async def search_one_account(search_aid: int):
            try:
                async with SessionFactory() as db:
                    search_account = await db.get(Account, search_aid)
                if not search_account or not search_account.enabled:
                    return []
                async with isolated_telegram_client(search_account) as search_client:
                    if not await search_client.is_user_authorized():
                        return []
                    return await search_public_groups(search_client, query, limit=100)
            except Exception as exc:
                logging.warning("Group discovery failed for account %s: %s", search_aid, exc)
                return []

        async def search_accounts():
            semaphore = asyncio.Semaphore(3)
            async def limited(search_aid):
                async with semaphore:
                    return await search_one_account(search_aid)
            results = await asyncio.gather(
                *(limited(search_aid) for search_aid in account_ids),
                return_exceptions=True,
            )
            return [
                item
                for batch in results if isinstance(batch, list)
                for item in batch
            ]

        native_task = asyncio.create_task(search_accounts())
        web_task = asyncio.create_task(search_web_groups(query, limit=600, pages_per_query=5))
        native_result, web_result = await asyncio.gather(
            native_task, web_task, return_exceptions=True
        )
        native_results = native_result if isinstance(native_result, list) else []
        web_results = web_result if isinstance(web_result, list) else []
        combined = {}
        for item in native_results + web_results:
            link = str(item.get("link") or "").strip()
            if not link:
                continue
            key = link.casefold()
            if key in combined:
                current = combined[key]
                current["score"] = max(
                    int(current.get("score") or 0), int(item.get("score") or 0)
                )
                current["source"] = ", ".join(dict.fromkeys(
                    (current.get("source", "") + ", " + item.get("source", "")).strip(", ").split(", ")
                ))
            else:
                combined[key] = item

        raw_candidates = list(combined.values())[:600]
        await edit_anchor(
            m.bot,
            anchor,
            f"🔎 Проверяю найденные ссылки · «{query}»\n\n"
            f"Аккаунтов в поиске: {len(account_ids)}\n"
            f"Кандидатов собрано: {len(raw_candidates)}\n"
            "Проверяю тип каждого чата непосредственно в Telegram. "
            "Каналы и нерабочие ссылки отбрасываются.",
            kb([[("⬅️ Инструменты", "tool:menu")]]),
        )
        candidates = await _verify_group_candidates_isolated(
            aid, raw_candidates, concurrency=4
        )
    except Exception as exc:
        await edit_anchor(
            m.bot,
            anchor,
            f"❌ Не удалось выполнить поиск или проверить результаты.\n\n"
            f"{type(exc).__name__}: {str(exc)[:220]}\n\n"
            "Проверьте подключение аккаунта и повторите поиск.",
            kb([
                [(
                    "🔎 Повторить всеми аккаунтами" if len(account_ids) > 1 else "🔎 Повторить поиск",
                    "tool:groups:all" if len(account_ids) > 1 else f"chat:search:{aid}",
                )],
                [("⬅️ Инструменты", "tool:menu")],
            ]),
        )
        return
    CHAT_CANDIDATES[(m.from_user.id, aid)] = candidates
    if not candidates:
        await edit_anchor(
            m.bot,
            anchor,
            f"По запросу «{query}» не удалось найти группы, которые Telegram подтвердил как группы.\n\n"
            "Это не означает, что таких групп нет: поисковики могут ограничивать выдачу, "
            "а Telegram не предоставляет полный каталог всех публичных групп. "
            "Попробуйте синонимы, более короткий запрос или другую формулировку.",
            kb([
                [(
                    "🔎 Новый поиск всеми аккаунтами" if len(account_ids) > 1 else "🔎 Новый запрос",
                    "tool:groups:all" if len(account_ids) > 1 else f"chat:search:{aid}",
                )],
                [("⬅️ Инструменты", "tool:menu")],
            ]),
        )
        return

    lines = [
        f"🔎 Найдено групп: {len(candidates)} · запрос «{query}»",
        f"Поиск выполнен через {len(account_ids)} аккаунт(ов) Telegram",
        "✓ Каждая ссылка проверена через Telegram API",
        "Каналы, боты и профили исключены.",
        "",
        f"Показываю 1–{min(12, len(candidates))}. Нажмите на группу, чтобы открыть проверку.",
    ]
    for i, item in enumerate(candidates[:12], 1):
        members = (
            f"{int(item.get('members') or 0):,}".replace(",", " ") + " участников"
            if item.get("members") else "участники: неизвестно"
        )
        kind_label = "супергруппа" if item.get("kind") == "supergroup" else (
            "приватная группа" if item.get("kind") == "invite" else "группа"
        )
        lines.append(
            f"{i}. {str(item.get('title') or 'Группа')[:75]} · {members} · {kind_label}"
        )
    await edit_anchor(
        m.bot, anchor, "\n".join(lines), _candidate_keyboard(aid, candidates, 0)
    )


@router.callback_query(F.data.startswith("chat:links:"))
async def chat_candidate_links(c):
    if not await check_cb(c): return
    aid = int(c.data.split(":")[2])
    candidates = CHAT_CANDIDATES.get((c.from_user.id, aid), [])
    links = list(dict.fromkeys(
        str(item.get("link") or "").strip()
        for item in candidates
        if item.get("verified") and item.get("link")
    ))
    if not links:
        await c.answer("Нет проверенных ссылок. Запустите поиск заново.", show_alert=True)
        return
    await c.answer("Отправляю список ссылок.")
    # Telegram message limit is 4096 chars; keep a safety margin and never split a URL.
    header = f"🔗 Проверенные ссылки на группы · всего {len(links)}\n\n"
    chunks = []
    current = []
    current_len = len(header)
    for link in links:
        if current and current_len + len(link) + 1 > 3700:
            chunks.append(header + "\n".join(current))
            current = []
            current_len = 0
        current.append(link)
        current_len += len(link) + 1
    if current:
        chunks.append((header if not chunks else "") + "\n".join(current))
    for index, chunk in enumerate(chunks, 1):
        if len(chunks) > 1:
            chunk = f"📄 Часть {index}/{len(chunks)}\n" + chunk
        await c.message.answer(chunk, disable_web_page_preview=True)


@router.callback_query(F.data.startswith("chat:discover:"))
async def chat_discover_links(c):
    if not await check_cb(c): return
    aid = int(c.data.split(":")[2])
    await c.answer("Запущено глубокое сканирование. Это может занять время.")
    anchor = {"ui_chat_id": c.message.chat.id, "ui_message_id": c.message.message_id}
    await edit_anchor(c.bot, anchor, f"🧭 Глубокий поиск ссылок · аккаунт #{aid}\n\nПросматриваю до 5 000 последних сообщений в каждой группе, где состоит аккаунт. Сканирование может занять значительное время.", kb([[("⬅️ К аккаунту", f"acc:view:{aid}")]]))
    scan_stats = {"groups": 0, "messages": 0, "links": 0}
    async def report_progress(groups, messages, links):
        scan_stats.update(groups=groups, messages=messages, links=links)
        if messages % 5000 != 0 and messages != 0:
            return
        progress_text = f"🧭 Глубокий поиск ссылок · аккаунт #{aid}\n\nГрупп обработано: {groups}\nСообщений просмотрено: {messages:,}\nУникальных ссылок найдено: {links}\n\nСканирование продолжается…".replace(",", " ")
        await edit_anchor(c.bot, anchor, progress_text, kb([[("⬅️ К аккаунту", f"acc:view:{aid}")]]))
    try:
        async with isolated_account_client(aid) as (_account, client):
            candidates = await find_invite_links_in_dialogs(client, dialog_limit=None, messages_per_dialog=5000, progress_callback=report_progress)
    except Exception as exc:
        await edit_anchor(c.bot, {"ui_chat_id": c.message.chat.id, "ui_message_id": c.message.message_id}, f"Не удалось найти ссылки: {str(exc)[:180]}", kb([[("⬅️ К аккаунту", f"acc:view:{aid}")]]))
        return
    CHAT_CANDIDATES[(c.from_user.id, aid)] = candidates
    if not candidates:
        await edit_anchor(c.bot, {"ui_chat_id": c.message.chat.id, "ui_message_id": c.message.message_id}, "🧭 Подходящих ссылок в последних сообщениях доступных групп не найдено.\n\nПоиск проверяет недавние сообщения в группах, к которым аккаунт уже имеет доступ; он не читает закрытые чаты, где аккаунт не состоит.", kb([[("🔎 Поиск публичных групп", f"chat:search:{aid}")], [("⬅️ К аккаунту", f"acc:view:{aid}")]]))
        return
    lines = [
        f"🧭 Найдено ссылок: {len(candidates)} · аккаунт #{aid}",
        f"Групп обработано: {scan_stats['groups']} · сообщений просмотрено: {scan_stats['messages']:,}".replace(",", " "),
        "Просмотрено до 5 000 последних сообщений в каждой доступной группе.",
        "",
        "Источник показывает чат, в котором была найдена ссылка. Каждая ссылка будет проверена перед вступлением.",
    ]
    for i, item in enumerate(candidates[:12], 1):
        lines.append(f"{i}. {item['link'][:75]}\n   найдено в: {item.get('source', 'сообщение')}")
    await edit_anchor(c.bot, {"ui_chat_id": c.message.chat.id, "ui_message_id": c.message.message_id}, "\n".join(lines), _candidate_keyboard(aid, candidates, 0))


@router.callback_query(F.data.startswith("chat:page:"))
async def chat_candidate_page(c):
    if not await check_cb(c): return
    _, _, aid_raw, offset_raw = c.data.split(":")
    aid, offset = int(aid_raw), max(0, int(offset_raw))
    candidates = CHAT_CANDIDATES.get((c.from_user.id, aid), [])
    if not candidates:
        await edit(c, "Результаты больше не сохранены. Запустите поиск ещё раз.", kb([[("🔎 Поиск групп", f"chat:search:{aid}")], [("⬅️ К аккаунту", f"acc:view:{aid}")]]))
        return
    page = candidates[offset:offset + 12]
    lines = [f"Найденные группы · аккаунт #{aid}", f"Показаны {offset + 1}–{min(offset + 12, len(candidates))} из {len(candidates)}", "", "Выберите результат для проверки."]
    for i, item in enumerate(page, start=offset + 1):
        lines.append(f"{i}. {str(item.get('title') or item.get('link') or 'Группа')[:70]}")
    await edit(c, "\n".join(lines), _candidate_keyboard(aid, candidates, offset))


@router.callback_query(F.data.startswith("chat:candidate:"))
async def chat_candidate_preview(c):
    if not await check_cb(c): return
    _, _, aid_raw, index_raw = c.data.split(":")
    aid, index = int(aid_raw), int(index_raw)
    candidates = CHAT_CANDIDATES.get((c.from_user.id, aid), [])
    if index < 0 or index >= len(candidates):
        await c.answer("Результаты устарели. Запустите поиск ещё раз.", show_alert=True); return
    item = candidates[index]
    link = item.get("link")
    if not link:
        await c.answer("У этой группы нет публичной ссылки.", show_alert=True); return
    try:
        kind, target = _parse_chat_link(link)
        async with isolated_account_client(aid) as (_account, client):
            already = False
            if kind == "public":
                entity = await client.get_entity(target)
                if not _group_entity(entity):
                    raise ValueError("Это канал или профиль, а не группа. Вступление отменено.")
                title = getattr(entity, "title", None) or target
                members = int(getattr(entity, "participants_count", 0) or 0)
                entity_id = utils.get_peer_id(entity)
            else:
                invite = await client(CheckChatInviteRequest(hash=target))
                if isinstance(invite, ChatInviteAlready):
                    entity = invite.chat
                    if not _group_entity(entity):
                        raise ValueError("Ссылка ведёт не на группу. Вступление отменено.")
                    title = getattr(entity, "title", None) or "Группа"
                    members = int(getattr(entity, "participants_count", 0) or 0)
                    entity_id = utils.get_peer_id(entity)
                    already = True
                else:
                    if getattr(invite, "channel", False) and not getattr(invite, "megagroup", False):
                        raise ValueError("Ссылка ведёт на канал, а не группу.")
                    title = getattr(invite, "title", None) or "Закрытая группа"
                    members = len(getattr(invite, "participants", []) or []) or int(getattr(invite, "participants_count", 0) or 0)
                    entity_id = None
    except Exception as exc:
        await edit(c, f"Не удалось проверить ссылку:\n{str(exc)[:220]}", kb([[("⬅️ К результатам", f"chat:results:{aid}")], [("⬅️ К аккаунту", f"acc:view:{aid}")]]))
        return
    CHAT_PENDING_JOIN[(c.from_user.id, aid)] = {"link": link, "kind": kind, "target": target, "title": title, "members": members, "entity_id": entity_id, "already": already}
    status = "Аккаунт уже состоит в группе." if already else "Вступление произойдёт только после вашего подтверждения."
    await edit(c, f"👥 {title}\nУчастников: {members or 'неизвестно'}\nСсылка: {link}\n\n{status}\n\nПроверьте название и ссылку перед вступлением.", kb([[("✅ Подтвердить вступление", f"chat:joinconfirm:{aid}")], [("⬅️ К результатам", f"chat:results:{aid}")], [("⬅️ К аккаунту", f"acc:view:{aid}")]]))


@router.callback_query(F.data.startswith("chat:results:"))
async def chat_results_back(c):
    if not await check_cb(c): return
    aid = int(c.data.split(":")[2])
    candidates = CHAT_CANDIDATES.get((c.from_user.id, aid), [])
    if not candidates:
        await edit(c, "Результаты больше не сохранены. Запустите поиск ещё раз.", kb([[("🔎 Поиск групп", f"chat:search:{aid}")], [("⬅️ К аккаунту", f"acc:view:{aid}")]]))
        return
    lines = [f"Найденные кандидаты · аккаунт #{aid}", "", "Выберите группу для проверки."]
    for i, item in enumerate(candidates[:12], 1):
        lines.append(f"{i}. {str(item.get('title') or item.get('link') or 'Группа')[:70]}")
    await edit(c, "\n".join(lines), _candidate_keyboard(aid, candidates, 0))


@router.callback_query(F.data.startswith("chat:joinlink:"))
async def chat_join_link_start(c, state: FSMContext):
    if not await check_cb(c): return
    aid = int(c.data.split(":")[2])
    await state.clear()
    await state.update_data(ui_chat_id=c.message.chat.id, ui_message_id=c.message.message_id, return_callback=cancel_return_for(c.data), chat_join_account_id=aid)
    await state.set_state(Flow.chat_join_link)
    await edit(c, f"🔗 Вступление в группу · аккаунт #{aid}\n\nОтправьте ссылку-приглашение t.me/+… / t.me/joinchat/… или публичную ссылку t.me/username.\n\nСначала бот проверит ссылку и покажет название группы. Само вступление будет только после отдельного подтверждения.", CANCEL)


@router.message(Flow.chat_join_link)
async def chat_join_link_input(m: Message, state: FSMContext):
    if not await check_msg(m) or not await private(m): return
    data = await state.get_data()
    raw = (m.text or "").strip()
    await delete_msg(m)
    try:
        kind, target = _parse_chat_link(raw)
    except ValueError as exc:
        await prompt_message(m, state, str(exc))
        return
    aid = int(data.get("chat_join_account_id", 0))
    anchor = {"ui_chat_id": data.get("ui_chat_id"), "ui_message_id": data.get("ui_message_id")}
    await state.clear()
    CHAT_CANDIDATES[(m.from_user.id, aid)] = [{"title": raw[:80], "link": raw, "source": "Введено вручную"}]
    # Reuse the same guarded preview flow as search results.
    CHAT_PENDING_JOIN.pop((m.from_user.id, aid), None)
    try:
        async with isolated_account_client(aid) as (_account, client):
            already = False
            if kind == "public":
                entity = await client.get_entity(target)
                if not _group_entity(entity):
                    raise ValueError("Ссылка ведёт на канал или профиль, а не группу.")
                title = getattr(entity, "title", None) or target
                members = int(getattr(entity, "participants_count", 0) or 0)
                entity_id = utils.get_peer_id(entity)
            else:
                invite = await client(CheckChatInviteRequest(hash=target))
                if isinstance(invite, ChatInviteAlready):
                    entity = invite.chat
                    if not _group_entity(entity):
                        raise ValueError("Ссылка ведёт не на группу.")
                    title = getattr(entity, "title", None) or "Группа"
                    members = int(getattr(entity, "participants_count", 0) or 0)
                    entity_id = utils.get_peer_id(entity)
                    already = True
                else:
                    if getattr(invite, "channel", False) and not getattr(invite, "megagroup", False):
                        raise ValueError("Ссылка ведёт на канал, а не группу.")
                    title = getattr(invite, "title", None) or "Закрытая группа"
                    members = len(getattr(invite, "participants", []) or []) or int(getattr(invite, "participants_count", 0) or 0)
                    entity_id = None
    except Exception as exc:
        await edit_anchor(m.bot, anchor, f"Не удалось проверить ссылку:\n{str(exc)[:220]}", kb([[("🔗 Ввести другую ссылку", f"chat:joinlink:{aid}")], [("⬅️ К аккаунту", f"acc:view:{aid}")]]))
        return
    CHAT_PENDING_JOIN[(m.from_user.id, aid)] = {"link": raw, "kind": kind, "target": target, "title": title, "members": members, "entity_id": entity_id, "already": already}
    status = "Аккаунт уже состоит в группе." if already else "Вступление произойдёт только после подтверждения."
    await edit_anchor(m.bot, anchor, f"👥 {title}\nУчастников: {members or 'неизвестно'}\nСсылка: {raw}\n\n{status}", kb([[("✅ Подтвердить вступление", f"chat:joinconfirm:{aid}")], [("🔗 Другая ссылка", f"chat:joinlink:{aid}")], [("⬅️ К аккаунту", f"acc:view:{aid}")]]))


@router.callback_query(F.data.startswith("chat:joinconfirm:"))
async def chat_join_confirm(c):
    if not await check_cb(c): return
    aid = int(c.data.split(":")[2])
    pending = CHAT_PENDING_JOIN.get((c.from_user.id, aid))
    if not pending:
        await c.answer("Предпросмотр устарел. Проверьте ссылку ещё раз.", show_alert=True); return
    try:
        async with isolated_account_client(aid) as (_account, client):
            entity = None
            if pending.get("already") and pending.get("entity_id"):
                entity = await client.get_entity(pending["entity_id"])
            elif pending["kind"] == "public":
                entity = await client.get_entity(pending["target"])
                async def report_floodwait(seconds):
                    await edit(c,
                        f"⏳ Telegram временно ограничил вступления. Жду {seconds} сек., затем повторю вступление в группу «{pending['title']}»…",
                        kb([[("⬅️ К аккаунту", f"acc:view:{aid}")]]),
                    )
                await perform_join_request(aid, client, JoinChannelRequest(entity), report_floodwait)
            else:
                async def report_floodwait(seconds):
                    await edit(c,
                        f"⏳ Telegram временно ограничил вступления. Жду {seconds} сек., затем повторю вступление в группу «{pending['title']}»…",
                        kb([[("⬅️ К аккаунту", f"acc:view:{aid}")]]),
                    )
                joined = await perform_join_request(
                    aid, client, ImportChatInviteRequest(hash=pending["target"]), report_floodwait
                )
                entity = next((x for x in joined.chats if _group_entity(x)), None)
                if entity is None:
                    raise RuntimeError("Telegram не вернул вступившую группу.")
            if entity is None:
                entity = await client.get_entity(pending["target"])
            if not _group_entity(entity):
                raise ValueError("Ссылка не ведёт на группу.")
            chat_id = int(utils.get_peer_id(entity))
            title = (getattr(entity, "title", None) or pending["title"] or "Группа")[:255]
    except Exception as exc:
        # Approval-only invite links are not equivalent to membership; explain that no binding was created.
        if isinstance(exc, FloodWaitError):
            await edit(c, f"⛔ Telegram ограничил вступления. Повторная попытка остановлена. Подождите {int(exc.seconds)} сек. и попробуйте снова.", kb([[('🔗 Другая ссылка', f'chat:joinlink:{aid}')], [('⬅️ К аккаунту', f'acc:view:{aid}')]]))
            return
        if "InviteRequestSentError" in type(exc).__name__:
            await edit(c, "Запрос на вступление отправлен администраторам группы. Аккаунт пока не добавлен в активные чаты — повторите после одобрения.", kb([[("🔗 Проверить другую ссылку", f"chat:joinlink:{aid}")], [("⬅️ К аккаунту", f"acc:view:{aid}")]]))
            return
        if "UserAlreadyParticipantError" in type(exc).__name__:
            try:
                async with isolated_account_client(aid) as (_account, client):
                    kind, target = pending["kind"], pending["target"]
                    if kind == "public":
                        entity = await client.get_entity(target)
                    else:
                        invite = await client(CheckChatInviteRequest(hash=target))
                        entity = invite.chat if isinstance(invite, ChatInviteAlready) else None
                    if entity is None or not _group_entity(entity):
                        raise RuntimeError("Не удалось определить уже доступную группу.")
                    chat_id = int(utils.get_peer_id(entity))
                    title = (getattr(entity, "title", None) or pending["title"] or "Группа")[:255]
            except Exception as nested:
                await edit(c, f"Аккаунт уже состоит в группе, но не удалось обновить её в списке: {type(nested).__name__}.", kb([[("⬅️ К аккаунту", f"acc:view:{aid}")]]))
                return
        else:
            await edit(c, f"Не удалось вступить: {type(exc).__name__}. Проверьте срок действия ссылки, лимиты Telegram и доступность группы.", kb([[("🔗 Другая ссылка", f"chat:joinlink:{aid}")], [("⬅️ К аккаунту", f"acc:view:{aid}")]]))
            return
    async with SessionFactory() as db:
        binding = await db.scalar(select(ChatBinding).where(ChatBinding.account_id == aid, ChatBinding.chat_id == chat_id))
        if binding:
            binding.title = title
            binding.enabled = True
        else:
            db.add(ChatBinding(account_id=aid, chat_id=chat_id, title=title, enabled=True, auto_discovered=False))
        await db.commit()
    CHAT_PENDING_JOIN.pop((c.from_user.id, aid), None)
    await edit(c, f"✅ Группа «{title}» добавлена в чаты аккаунта #{aid}.\n\nЧат включён для работы и обучения. Если нужно, его можно выключить в разделе «Чаты аккаунта».", kb([[("💬 Чаты аккаунта", f"chat:account:{aid}:0")], [("👤 К аккаунту", f"acc:view:{aid}")], [("🏠 Главное меню", "nav:home")]]))


# Chat management is accessed from each account card. Keep the old callback as a safe redirect.
@router.callback_query(F.data == "chat:menu")
async def chats_legacy_redirect(c):
    if not await check_cb(c): return
    await edit(c, "Управление чатами теперь находится внутри карточки аккаунта.", kb([[("👤 К аккаунтам", "acc:list")], [("🏠 Главное меню", "nav:home")]]))

async def chat_account_screen(c, aid, page=0):
    async with SessionFactory() as db:
        account = await db.get(Account, aid)
        bindings = list((await db.scalars(
            select(ChatBinding).where(ChatBinding.account_id == aid).order_by(ChatBinding.title, ChatBinding.id)
        )).all())
        discussion_ids = set((await db.scalars(
            select(ChannelBinding.discussion_group_id).where(
                ChannelBinding.account_id == aid,
                ChannelBinding.discussion_group_id.is_not(None),
            )
        )).all())
        channel_ids = set((await db.scalars(
            select(ChannelBinding.channel_id).where(ChannelBinding.account_id == aid)
        )).all())
        excluded_chat_ids = discussion_ids | channel_ids
        bindings = [row for row in bindings if int(row.chat_id) not in excluded_chat_ids]
    if not account:
        await c.answer("Аккаунт не найден.", show_alert=True)
        return
    page_size = 20
    max_page = max(0, (len(bindings) - 1) // page_size)
    page = max(0, min(page, max_page))
    visible = bindings[page * page_size:(page + 1) * page_size]
    rows = [[(f"{'🟢' if row.enabled else '⚪'} {row.title[:38]}", f"chat:select:{aid}:{row.id}:{page}")] for row in visible]
    nav = []
    if page > 0: nav.append(("⬅️ Назад", f"chat:account:{aid}:{page-1}"))
    if page < max_page: nav.append(("➡️ Далее", f"chat:account:{aid}:{page+1}"))
    if nav: rows.append(nav)
    rows += [
        [("↻ Загрузить чаты" if not bindings else "↻ Обновить список чатов", f"chat:load:{aid}:{page}")],
        [("🧭 Поиск ссылок", f"chat:discover:{aid}")],
        [("⬅️ К аккаунту", f"acc:view:{aid}")],
    ]
    hint = "Сначала нажмите «Загрузить чаты»." if not bindings else f"Всего чатов: {len(bindings)}. Нажатие на строку сразу включает или выключает чат."
    await edit(c, f"💬 Чаты · {account.display_name} · страница {page+1}/{max_page+1}\n\n{hint}\n🟢 — включён · ⚪ — выключен", kb(rows))


@router.callback_query(F.data.startswith("chat:account:"))
async def chat_account(c):
    if not await check_cb(c): return
    parts = c.data.split(":")
    aid = int(parts[2])
    page = int(parts[3]) if len(parts) > 3 and parts[3].isdigit() else 0
    await chat_account_screen(c, aid, page)


@router.callback_query(F.data.startswith("chat:load:"))
async def chat_load(c):
    if not await check_cb(c): return
    parts = c.data.split(":")
    aid = int(parts[2])
    page = int(parts[3]) if len(parts) > 3 and parts[3].isdigit() else 0
    if not cfg.telegram_api_id or not cfg.telegram_api_hash:
        await c.answer("Не заданы Telegram API credentials.", show_alert=True); return
    async with SessionFactory() as db:
        account = await db.get(Account, aid)
    if not account:
        await c.answer("Аккаунт не найден", show_alert=True); return
    try:
        async with isolated_telegram_client(account) as client:
            if not await client.is_user_authorized():
                await c.answer("Сессия не авторизована", show_alert=True)
                return
            dialogs = []
            async for dialog in client.iter_dialogs():
                entity = dialog.entity
                if (
                    getattr(entity, "deleted", False)
                    or getattr(entity, "bot", False)
                    or (getattr(entity, "broadcast", False) and not getattr(entity, "megagroup", False))
                ):
                    continue
                title = (dialog.name or str(dialog.id)).strip()
                if title:
                    dialogs.append((int(dialog.id), title[:255]))
    except Exception as exc:
        await c.answer(f"Ошибка загрузки: {type(exc).__name__}: {str(exc)[:180]}", show_alert=True)
        return
    async with SessionFactory() as db:
        discussion_ids = set((await db.scalars(
            select(ChannelBinding.discussion_group_id).where(
                ChannelBinding.account_id == aid,
                ChannelBinding.discussion_group_id.is_not(None),
            )
        )).all())
        channel_ids = set((await db.scalars(
            select(ChannelBinding.channel_id).where(ChannelBinding.account_id == aid)
        )).all())
        excluded_chat_ids = discussion_ids | channel_ids
        dialogs = [item for item in dialogs if item[0] not in excluded_chat_ids]
        existing = list((await db.scalars(select(ChatBinding).where(ChatBinding.account_id == aid))).all())
        by_chat = {row.chat_id: row for row in existing}
        added = 0
        for chat_id, title in dialogs:
            binding = by_chat.get(chat_id)
            if binding:
                binding.title = title
            else:
                db.add(ChatBinding(account_id=aid, chat_id=chat_id, **discovered_binding_values(title)))
                added += 1
        await db.commit()
    await chat_account_screen(c, aid, page)
    await c.message.answer(f"✅ Загружено чатов: {len(dialogs)} · новых добавлено: {added}")


@router.callback_query(F.data.startswith("chat:select:"))
async def chat_select(c):
    if not await check_cb(c): return
    _, _, aid_raw, binding_raw, page_raw = c.data.split(":")
    aid, binding_id, page = int(aid_raw), int(binding_raw), int(page_raw)
    async with SessionFactory() as db:
        binding = await db.get(ChatBinding, binding_id)
        if not binding or binding.account_id != aid:
            await c.answer("Чат не найден.", show_alert=True)
            return
        binding.enabled = not binding.enabled
        await db.commit()
    await chat_account_screen(c, aid, page)


async def chat_account_refresh(c, aid, page=0):
    await chat_account_screen(c, aid, page)


@router.callback_query(F.data.startswith("chat:toggle:"))
async def chat_toggle(c):
    if not await check_cb(c): return
    bid=int(c.data.split(":")[2])
    async with SessionFactory() as db:
        b=await db.get(ChatBinding,bid)
        if not b: await c.answer("Чат не найден",show_alert=True); return
        b.enabled = not b.enabled
        aid = b.account_id
        await db.commit()
    await chat_account_refresh(c,aid)


# Channels: account -> channel -> channel card -> inherited/overridden settings.
CHANNEL_SETTING_FIELDS = {
    "post_instructions": ("⚙️ Постов", "channel_post_instructions", "post_instructions_override", "text"),
    "wait_seconds": ("⏱ Время ожидания", "channel_wait_seconds", "comment_wait_seconds_override", "seconds"),
    "min_comments": ("💬 Минимум комментариев", "channel_min_comments", "min_comments_override", "min_comments"),
    "comments_instructions": ("⚙️ Комментариев", "channel_comments_instructions", "comments_instructions_override", "text"),
    "restrictions_instructions": ("⚙️ Ограничений", "channel_restrictions_instructions", "restrictions_instructions_override", "text"),
    "reply_probability": ("🎯 Вероятность ответа", "channel_reply_probability", "reply_probability_override", "percent"),
    "reply_cooldown_seconds": ("⏳ Пауза между ответами", "channel_reply_cooldown_seconds", "reply_cooldown_seconds_override", "seconds"),
    "reply_mode": ("🎚 Режим публикации", "channel_reply_mode", "reply_mode_override", "mode"),
}

def channel_setting_value(account, binding, field):
    _, account_attr, channel_attr, _ = CHANNEL_SETTING_FIELDS[field]
    if binding is not None:
        override = getattr(binding, channel_attr)
        if override is not None:
            return override
    return getattr(account, account_attr)

def channel_setting_text(field, value):
    label, _, _, kind = CHANNEL_SETTING_FIELDS[field]
    if kind == "seconds":
        return f"{label}: {format_duration(value)} ({value} сек.)"
    if kind == "percent":
        return f"{label}: {value}%"
    return label

@router.callback_query(F.data == "channel:menu")
async def channels_legacy_redirect(c):
    if not await check_cb(c): return
    async with SessionFactory() as db:
        accounts = list((await db.scalars(select(Account).order_by(Account.id))).all())
    rows = [[(f"👤 {a.display_name[:28]}", f"channel:account:{a.id}")] for a in accounts]
    rows.append([("⬅️ К аккаунтам", "acc:list")])
    await edit(c, "Управление каналами теперь находится в карточке соответствующего аккаунта.", kb(rows))


async def channel_account_screen(c, aid):
    async with SessionFactory() as db:
        account = await db.get(Account, aid)
        bindings = list((await db.scalars(select(ChannelBinding).where(
            ChannelBinding.account_id == aid
        ).order_by(ChannelBinding.title, ChannelBinding.id))).all())
    if not account:
        await c.answer("Аккаунт не найден.", show_alert=True)
        return
    rows = [[(f"{'🟢' if b.reply_enabled else '🔴'} {b.title[:42]}", f"channel:open:{aid}:{b.channel_id}")] for b in bindings[:70]]
    rows += [
        [("↻ Загрузить / обновить каналы", f"channel:sync:{aid}")],
        [("⚙️ Глобальные настройки каналов", f"channel:gsettings:{aid}")],
        [("🧾 История анализа", f"channel:history:{aid}")],
        [("⬅️ К аккаунту", f"acc:view:{aid}")],
    ]
    summary = "Каналов пока нет. Нажмите «Загрузить / обновить каналы»." if not bindings else "🟢 — ответы в комментариях включены · 🔴 — выключены.\nНажмите на канал, чтобы открыть его карточку. Нажатие не меняет его состояние."
    await edit(c, f"📡 Каналы · аккаунт #{aid} · {account.display_name}\n\n{summary}", kb(rows))

@router.callback_query(F.data.startswith("channel:account:"))
async def channel_account(c):
    if not await check_cb(c): return
    await channel_account_screen(c, int(c.data.split(":")[2]))

@router.callback_query(F.data.startswith("channel:sync:"))
async def channel_sync(c):
    if not await check_cb(c): return
    aid = int(c.data.split(":")[2])
    if not cfg.telegram_api_id or not cfg.telegram_api_hash:
        await c.answer("Не заданы Telegram API credentials.", show_alert=True)
        return
    async with SessionFactory() as db:
        account = await db.get(Account, aid)
    if not account:
        await c.answer("Аккаунт не найден.", show_alert=True)
        return
    dialogs = []
    try:
        async with isolated_telegram_client(account) as client:
            if not await client.is_user_authorized():
                await c.answer("Сессия аккаунта не авторизована.", show_alert=True)
                return
            async for dialog in client.iter_dialogs():
                entity = dialog.entity
                if not getattr(entity, "broadcast", False) or getattr(entity, "megagroup", False):
                    continue
                title = (dialog.name or str(dialog.id)).strip()
                if title:
                    dialogs.append((int(dialog.id), title[:255]))
    except Exception as exc:
        await c.answer(f"Ошибка загрузки каналов: {type(exc).__name__}: {str(exc)[:180]}", show_alert=True)
        return
    async with SessionFactory() as db:
        existing = list((await db.scalars(select(ChannelBinding).where(ChannelBinding.account_id == aid))).all())
        by_id = {row.channel_id: row for row in existing}
        added = 0
        for channel_id, title in dialogs:
            row = by_id.get(channel_id)
            if row:
                row.title = title
            else:
                db.add(ChannelBinding(account_id=aid, channel_id=channel_id, title=title, enabled=False, reply_enabled=False))
                added += 1
        await db.commit()
    await channel_account_screen(c, aid)
    await c.message.answer(f"✅ Загружено каналов: {len(dialogs)} · новых добавлено: {added}")

@router.callback_query(F.data.startswith("channel:open:"))
async def channel_open(c, *, answer_callback=True):
    if not await check_cb(c): return
    _, _, aid, channel_id = c.data.split(":")
    aid, channel_id = int(aid), int(channel_id)
    async with SessionFactory() as db:
        binding = await db.scalar(select(ChannelBinding).where(
            ChannelBinding.account_id == aid, ChannelBinding.channel_id == channel_id
        ))
    if not binding:
        await c.answer("Канал не найден. Обновите список каналов.", show_alert=True)
        return
    state = "🟢 Отвечать в комментариях включено" if binding.reply_enabled else "🔴 Отвечать в комментариях выключено"
    await edit(c, f"📡 {binding.title}\nАккаунт: #{aid}\n\n{state}\n\nИИ анализирует публикацию и комментарии как контекст аудитории. Вероятность определяет, будет ли подготовлен комментарий к исходному посту; при 100% ИИ готовит его для каждой публикации, если ответ не запрещён ограничениями. На отдельные комментарии пользователей канал-автоматизация не отвечает.", kb([
        [("🔴 Выключить ответы" if binding.reply_enabled else "🟢 Включить ответы", f"channel:reply:{binding.id}")],
        [("⚙️ Настроить", f"channel:csettings:{binding.id}")],
        [("🧾 История анализа", f"channel:history:{aid}")],
        [("⬅️ К каналам аккаунта", f"channel:account:{aid}"), ("🏠 Главное меню", "nav:home")],
    ]), answer_callback=answer_callback)

@router.callback_query(F.data.startswith("channel:reply:"))
async def channel_reply_toggle(c):
    if not await check_cb(c): return

    # Stop Telegram's callback spinner immediately; the button state itself is
    # changed only after PostgreSQL confirms the transaction.
    try:
        await c.answer()
    except Exception:
        # An expired callback must not prevent a valid database update.
        pass

    binding_id = int(c.data.split(":")[2])
    async with SessionFactory() as db:
        binding = await db.get(ChannelBinding, binding_id)
        if not binding:
            if c.message:
                await c.message.answer("⚠️ Канал не найден. Обновите список каналов.")
            return

        new_state = not binding.reply_enabled
        binding.reply_enabled = new_state
        binding.enabled = new_state
        try:
            await db.commit()
        except Exception:
            await db.rollback()
            if c.message:
                await c.message.answer(
                    "❌ Не удалось сохранить настройку. Состояние переключателя не изменено."
                )
            return

    # Render from the committed database state without answering the callback twice.
    await channel_open(c, answer_callback=False)

async def channel_settings_screen(c, scope, object_id):
    async with SessionFactory() as db:
        if scope == "global":
            account = await db.get(Account, object_id)
            binding = None
        else:
            binding = await db.get(ChannelBinding, object_id)
            account = await db.get(Account, binding.account_id) if binding else None
    if not account or (scope == "channel" and not binding):
        await c.answer("Настройки не найдены.", show_alert=True)
        return
    title = "Глобальные настройки" if scope == "global" else f"Настройки канала · {binding.title}"
    lines = [
        f"⚙️ {title}",
        "Общие значения этого аккаунта применяются ко всем его каналам. Индивидуальные настройки канала могут их переопределить.",
        "ИИ анализирует публикацию и связанные с ней комментарии. При включённом условии бот сначала ждёт заданное количество комментариев; при выключенном — начинает анализ без ожидания.",
        "",
    ]
    rows = []
    prefix = "g" if scope == "global" else "c"
    mode = effective_channel_reply_mode(account, binding) if binding is not None else (
        "automatic" if account.channel_reply_mode == "automatic" else "manual"
    )
    mode_label = "Автоматическая публикация" if mode == "automatic" else "Ручной режим · черновик"
    mode_source = "собственная настройка" if binding is not None and binding.reply_mode_override is not None else "глобальная настройка"
    lines.append(f"Режим публикации: {mode_label} ({mode_source})")
    rows.append([(f"🔄 Переключить режим публикации · {mode_label}", f"channel:mode:{prefix}:{object_id}")])

    chat_replies_enabled = (
        bool(account.channel_chat_replies_enabled)
        if binding is None
        else effective_channel_chat_replies_enabled(account, binding)
    )
    chat_replies_source = (
        "глобальная настройка аккаунта"
        if binding is None or binding.chat_replies_enabled_override is None
        else "индивидуальная настройка канала"
    )
    chat_replies_icon = "🟢" if chat_replies_enabled else "🔴"
    chat_replies_label = "включены" if chat_replies_enabled else "выключены"
    lines.append(f"Ответы коммент {chat_replies_icon}: {chat_replies_label} ({chat_replies_source})")
    rows.append([(
        f"Ответы коммент {chat_replies_icon}",
        f"channel:chatreplies:info:{prefix}:{object_id}",
    )])

    policy_enabled, policy_action, policy_minimum, policy_wait = resolve_channel_comment_policy(account, binding)
    policy_icon = "🟢" if policy_enabled else "🔴"
    lines.append(
        f"Условие по комментариям: {policy_icon} {'включено' if policy_enabled else 'выключено'} "
        f"({policy_minimum} комментария · ожидание {format_duration(policy_wait)})"
    )
    rows.append([(
        f"💬 Условие по комментариям {policy_icon}",
        f"channel:commentpolicy:open:{prefix}:{object_id}",
    )])
    if binding is not None and binding.chat_replies_enabled_override is not None:
        rows.append([(
            "↩️ Наследовать глобальную настройку ответов коммент",
            f"channel:chatreplies:inherit:{object_id}",
        )])

    explanations = {
        "post_instructions": "Как ИИ должен учитывать содержание публикаций.",
        "comments_instructions": "На что обращать внимание в обсуждении.",
        "restrictions_instructions": "Каких тем и действий избегать.",
        "reply_probability": "Вероятность того, что ИИ подготовит комментарий к публикации. При 100% комментарий готовится для каждой публикации, если ответ не запрещён ограничениями.",
        "reply_cooldown_seconds": "Минимальная пауза между публикациями ответов в этом канале.",
    }
    for field in ("post_instructions", "comments_instructions", "restrictions_instructions", "reply_probability", "reply_cooldown_seconds"):
        label, _, _, kind = CHANNEL_SETTING_FIELDS[field]
        value = channel_setting_value(account, binding, field)
        if kind == "text":
            shown = "заданы" if str(value).strip() else "не заданы"
        elif kind == "seconds":
            shown = f"{format_duration(value)} · {value} сек."
        elif kind == "percent":
            shown = f"{value}%"
        else:
            shown = str(value)
        lines.append(f"{label}: {shown}")
        lines.append(f"  {explanations[field]}")
        prefix = "g" if scope == "global" else "c"
        rows.append([(f"✏️ {label}", f"channel:edit:{prefix}:{object_id}:{field}")])
    if scope == "channel":
        learning_state = "идёт" if binding.learning_status == "training" else "ошибка — запустите повторно" if binding.learning_status == "error" else "завершено" if binding.learning_completed_at else "не запускалось"
        lines.append(f"Автообучение: {learning_state}")
        lines.append("Автообучение — анализ до 200 последних текстовых публикаций и до 100 комментариев к каждой. Пустые публикации без текста пропускаются.")
        rows.append([("🧠 Автообучение по последним 200 постам", f"channel:learn:{object_id}")])
        rows.append([("↩️ Сбросить настройки канала к глобальным", f"channel:inherit:{object_id}")])
        rows.append([("⬅️ К карточке канала", f"channel:open:{account.id}:{binding.channel_id}")])
    else:
        rows.append([("⬅️ К каналам аккаунта", f"channel:account:{account.id}")])
    rows.append([("🏠 Главное меню", "nav:home")])
    await edit(c, "\n".join(lines), kb(rows))

@router.callback_query(F.data.startswith("channel:gsettings:"))
async def channel_global_settings(c):
    if not await check_cb(c): return
    await channel_settings_screen(c, "global", int(c.data.split(":")[2]))

@router.callback_query(F.data.startswith("channel:csettings:"))
async def channel_custom_settings(c):
    if not await check_cb(c): return
    await channel_settings_screen(c, "channel", int(c.data.split(":")[2]))

async def channel_comment_policy_screen(c, scope, object_id):
    async with SessionFactory() as db:
        if scope == "g":
            account = await db.get(Account, object_id)
            binding = None
        else:
            binding = await db.get(ChannelBinding, object_id)
            account = await db.get(Account, binding.account_id) if binding else None
    if not account or (scope == "c" and not binding):
        await c.answer("Настройка не найдена.", show_alert=True)
        return

    enabled, action, minimum, wait_seconds = resolve_channel_comment_policy(account, binding)
    icon = "🟢" if enabled else "🔴"
    source = (
        "глобальная настройка аккаунта"
        if binding is None or binding.insufficient_action_override is None
        else "индивидуальная настройка канала"
    )
    action_labels = {
        "wait": "Продолжать ждать, пока не наберётся минимум",
        "skip": "После одного интервала пропустить публикацию, если минимума нет",
        "skip_empty": "Пропустить только если комментариев совсем нет; если они есть — продолжать ждать",
    }
    if enabled:
        condition_text = (
            f"Бот ждёт минимум {minimum} комментария в обсуждении новой публикации. "
            "Как только порог достигнут, ИИ анализирует публикацию и комментарии, "
            "определяет, относятся ли они к теме поста, и использует их как контекст "
            "для собственного комментария."
        )
    else:
        condition_text = (
            "Условие выключено: бот не ждёт определённого количества комментариев "
            "и передаёт публикацию на анализ сразу."
        )
    description = (
        f"💬 Условие по комментариям {icon}\n"
        f"Состояние: {'включено' if enabled else 'выключено'} · {source}.\n"
        f"Минимум: {minimum}\n"
        f"Время ожидания: {format_duration(wait_seconds)} ({wait_seconds} сек.)\n"
        f"Действие после ожидания: {action_labels[action]}.\n\n"
        f"{condition_text}\n\n"
        "Вероятность публикации комментария остаётся отдельной настройкой и работает "
        "по прежним правилам. Этот параметр её не изменяет."
    )
    prefix = "g" if scope == "g" else "c"
    rows = [[(
        "🔴 Выключить условие" if enabled else "🟢 Включить условие",
        f"channel:commentpolicy:toggle:{prefix}:{object_id}",
    )]]
    rows.append([(
        "⚙️ Настроить минимум, ожидание и действие",
        f"channel:commentpolicy:config:{prefix}:{object_id}",
    )])
    back = f"channel:gsettings:{account.id}" if binding is None else f"channel:csettings:{binding.id}"
    rows.append([("⬅️ К настройкам канала", back)])
    await edit(c, description, kb(rows))


async def channel_comment_policy_config_screen(c, scope, object_id):
    async with SessionFactory() as db:
        if scope == "g":
            account = await db.get(Account, object_id)
            binding = None
        else:
            binding = await db.get(ChannelBinding, object_id)
            account = await db.get(Account, binding.account_id) if binding else None
    if not account or (scope == "c" and not binding):
        await c.answer("Настройка не найдена.", show_alert=True)
        return

    enabled, action, minimum, wait_seconds = resolve_channel_comment_policy(account, binding)
    action_labels = {
        "wait": "Ждать дальше до достижения минимума",
        "skip": "Пропустить после одного интервала, если минимума нет",
        "skip_empty": "Пропустить, только если комментариев нет совсем",
    }
    prefix = "g" if scope == "g" else "c"
    description = (
        "⚙️ Параметры условия по комментариям\n\n"
        f"Условие: {'включено' if enabled else 'выключено'}\n"
        f"Минимум комментариев: {minimum}\n"
        f"Интервал ожидания: {format_duration(wait_seconds)} ({wait_seconds} сек.)\n"
        f"Действие по окончании интервала: {action_labels[action]}\n\n"
        "Если минимум ещё не набран, бот ждёт указанный интервал и проверяет обсуждение повторно. "
        "При достижении минимума запускается анализ. Если минимум не набран, применяется "
        "выбранное действие."
    )
    rows = [
        [(f"💬 Минимум комментариев · {minimum}", f"channel:edit:{prefix}:{object_id}:min_comments")],
        [(f"⏱ Время ожидания · {format_duration(wait_seconds)}", f"channel:edit:{prefix}:{object_id}:wait_seconds")],
        [(f"⏭ Действие после ожидания · {action_labels[action]}", f"channel:commentpolicy:action:{prefix}:{object_id}")],
        [("⬅️ Назад", f"channel:commentpolicy:open:{prefix}:{object_id}")],
    ]
    await edit(c, description, kb(rows))


@router.callback_query(F.data.startswith("channel:commentpolicy:open:"))
async def channel_comment_policy_open(c):
    if not await check_cb(c): return
    _, _, _, scope, object_raw = c.data.split(":")
    if scope not in {"g", "c"}:
        await c.answer("Неизвестная настройка.", show_alert=True)
        return
    await channel_comment_policy_screen(c, scope, int(object_raw))


@router.callback_query(F.data.startswith("channel:commentpolicy:config:"))
async def channel_comment_policy_config(c):
    if not await check_cb(c): return
    _, _, _, scope, object_raw = c.data.split(":")
    if scope not in {"g", "c"}:
        await c.answer("Неизвестная настройка.", show_alert=True)
        return
    await channel_comment_policy_config_screen(c, scope, int(object_raw))


@router.callback_query(F.data.startswith("channel:commentpolicy:toggle:"))
async def channel_comment_policy_toggle(c):
    if not await check_cb(c): return
    _, _, _, scope, object_raw = c.data.split(":")
    if scope not in {"g", "c"}:
        await c.answer("Неизвестная настройка.", show_alert=True)
        return
    object_id = int(object_raw)
    async with SessionFactory() as db:
        if scope == "g":
            account = await db.get(Account, object_id)
            if not account:
                await c.answer("Аккаунт не найден.", show_alert=True)
                return
            enabled, action = decode_channel_comment_policy_action(account.channel_insufficient_action)
            account.channel_insufficient_action = encode_channel_comment_policy_action(not enabled, action)
        else:
            binding = await db.get(ChannelBinding, object_id)
            if not binding:
                await c.answer("Канал не найден.", show_alert=True)
                return
            raw_action = (
                binding.insufficient_action_override
                if binding.insufficient_action_override is not None
                else (await db.get(Account, binding.account_id)).channel_insufficient_action
            )
            enabled, action = decode_channel_comment_policy_action(raw_action)
            binding.insufficient_action_override = encode_channel_comment_policy_action(not enabled, action)
        await db.commit()
    await channel_comment_policy_screen(c, scope, object_id)


@router.callback_query(F.data.startswith("channel:commentpolicy:action:"))
async def channel_comment_policy_action(c):
    if not await check_cb(c): return
    _, _, _, scope, object_raw = c.data.split(":")
    if scope not in {"g", "c"}:
        await c.answer("Неизвестная настройка.", show_alert=True)
        return
    object_id = int(object_raw)
    async with SessionFactory() as db:
        if scope == "g":
            account = await db.get(Account, object_id)
            if not account:
                await c.answer("Аккаунт не найден.", show_alert=True)
                return
            enabled, action = decode_channel_comment_policy_action(account.channel_insufficient_action)
            next_action = {"wait": "skip", "skip": "skip_empty", "skip_empty": "wait"}[action]
            account.channel_insufficient_action = encode_channel_comment_policy_action(enabled, next_action)
        else:
            binding = await db.get(ChannelBinding, object_id)
            if not binding:
                await c.answer("Канал не найден.", show_alert=True)
                return
            account = await db.get(Account, binding.account_id)
            if not account:
                await c.answer("Аккаунт не найден.", show_alert=True)
                return
            raw_action = (
                binding.insufficient_action_override
                if binding.insufficient_action_override is not None
                else account.channel_insufficient_action
            )
            enabled, action = decode_channel_comment_policy_action(raw_action)
            next_action = {"wait": "skip", "skip": "skip_empty", "skip_empty": "wait"}[action]
            binding.insufficient_action_override = encode_channel_comment_policy_action(enabled, next_action)
        await db.commit()
    await channel_comment_policy_config_screen(c, scope, object_id)


@router.callback_query(F.data.startswith("channel:edit:"))
async def channel_setting_edit(c, state: FSMContext):
    if not await check_cb(c): return
    _, _, scope, object_raw, field = c.data.split(":")
    if scope not in {"g", "c"} or field not in CHANNEL_SETTING_FIELDS:
        await c.answer("Неизвестная настройка.", show_alert=True)
        return
    label, _, _, kind = CHANNEL_SETTING_FIELDS[field]
    minimum, maximum = (1, 10) if kind == "min_comments" else (0, 100) if kind == "percent" else (0, 2147483647) if kind == "seconds" else (0, 4000)
    await state.clear()
    await state.update_data(channel_scope=scope, channel_object_id=int(object_raw), channel_field=field, ui_chat_id=c.message.chat.id, ui_message_id=c.message.message_id, return_callback=cancel_return_for(c.data))
    await state.set_state(Flow.channel_setting_value)
    if kind == "text":
        hint = "Отправьте текст инструкции. Можно несколько абзацев. Чтобы очистить, отправьте точку: ."
    elif kind == "seconds":
        hint = "Введите целое число секунд. Например: 3600 секунд = 01:00:00 (60 минут)."
    elif kind == "percent":
        hint = "Введите процент от 0 до 100."
    elif kind == "min_comments":
        hint = "Введите минимальное количество комментариев от 1 до 10."
    else:
        hint = "Введите число от 1 до 10."
    range_hint = f"Максимум: {maximum} сек. ({format_duration(maximum)})." if kind == "seconds" else f"Допустимый диапазон: {minimum}–{maximum}."
    await edit(c, f"{label}\n{hint}\n{range_hint}", CANCEL)

@router.message(Flow.channel_setting_value)
async def channel_setting_save(m: Message, state: FSMContext):
    if not await check_msg(m) or not await private(m): return
    data = await state.get_data()
    field, scope, object_id = data.get("channel_field"), data.get("channel_scope"), int(data.get("channel_object_id", 0))
    if field not in CHANNEL_SETTING_FIELDS or scope not in {"g", "c"}:
        await finish_prompt(m, state, "Настройка не найдена.")
        return
    label, account_attr, channel_attr, kind = CHANNEL_SETTING_FIELDS[field]
    raw = (m.text or "").strip()
    if kind == "text":
        value = "" if raw == "." else raw
        if len(value) > 4000:
            await prompt_message(m, state, "Максимальная длина инструкции — 4000 символов.")
            return
    else:
        if not raw.isdigit():
            await prompt_message(m, state, "Введите целое число.")
            return
        value = int(raw)
        minimum, maximum = (1, 10) if kind == "min_comments" else (0, 100) if kind == "percent" else (0, 2147483647)
        if not minimum <= value <= maximum:
            await prompt_message(m, state, f"Допустимый диапазон: {minimum}–{maximum}.")
            return
    await delete_msg(m)
    async with SessionFactory() as db:
        if scope == "g":
            account = await db.get(Account, object_id)
            if not account:
                await finish_prompt(m, state, "Аккаунт не найден.")
                return
            setattr(account, account_attr, value)
        else:
            binding = await db.get(ChannelBinding, object_id)
            if not binding:
                await finish_prompt(m, state, "Канал не найден.")
                return
            setattr(binding, channel_attr, value)
            if field == "wait_seconds":
                binding.comment_wait_seconds = value
        await db.commit()
    destination = (
        f"channel:commentpolicy:config:{scope}:{object_id}"
        if field in {"wait_seconds", "min_comments"}
        else f"channel:gsettings:{object_id}" if scope == "g" else f"channel:csettings:{object_id}"
    )
    shown = format_duration(value) if kind == "seconds" else f"{value}%" if kind == "percent" else str(value)
    await finish_prompt(m, state, f"✅ {label}: {shown}", kb([[("⬅️ К настройкам", destination)], [("🏠 Главное меню", "nav:home")]]))

@router.callback_query(F.data.startswith("channel:mode:"))
async def channel_mode_toggle(c):
    if not await check_cb(c): return
    _, _, scope, object_raw = c.data.split(":")
    if scope not in {"g", "c"}:
        await c.answer("Неизвестный режим.", show_alert=True)
        return
    object_id = int(object_raw)
    async with SessionFactory() as db:
        if scope == "g":
            account = await db.get(Account, object_id)
            if not account:
                await c.answer("Аккаунт не найден.", show_alert=True)
                return
            current = "automatic" if account.channel_reply_mode == "automatic" else "manual"
            account.channel_reply_mode = "manual" if current == "automatic" else "automatic"
        else:
            binding = await db.get(ChannelBinding, object_id)
            if not binding:
                await c.answer("Канал не найден.", show_alert=True)
                return
            account = await db.get(Account, binding.account_id)
            current = effective_channel_reply_mode(account, binding)
            binding.reply_mode_override = "manual" if current == "automatic" else "automatic"
        await db.commit()
    await channel_settings_screen(c, "global" if scope == "g" else "channel", object_id)


@router.callback_query(F.data.startswith("channel:chatreplies:inherit:"))
async def channel_chat_replies_inherit(c):
    if not await check_cb(c): return
    binding_id = int(c.data.split(":")[3])
    async with SessionFactory() as db:
        binding = await db.get(ChannelBinding, binding_id)
        if not binding:
            await c.answer("Канал не найден.", show_alert=True)
            return
        binding.chat_replies_enabled_override = None
        await db.commit()
    await channel_chat_replies_screen(c, "channel", binding_id)


async def channel_chat_replies_screen(c, scope, object_id):
    async with SessionFactory() as db:
        if scope == "g":
            account = await db.get(Account, object_id)
            binding = None
        else:
            binding = await db.get(ChannelBinding, object_id)
            account = await db.get(Account, binding.account_id) if binding else None
    if not account or (scope == "c" and not binding):
        await c.answer("Настройка не найдена.", show_alert=True)
        return

    enabled = (
        bool(account.channel_chat_replies_enabled)
        if binding is None
        else effective_channel_chat_replies_enabled(account, binding)
    )
    icon = "🟢" if enabled else "🔴"
    status = "включены" if enabled else "выключены"
    source = (
        "глобальная настройка аккаунта"
        if binding is None or binding.chat_replies_enabled_override is None
        else "индивидуальная настройка канала"
    )
    subject = "в обсуждениях всех каналов аккаунта" if binding is None else f"в обсуждении канала «{binding.title}»"
    description = (
        f"Ответы коммент {icon}\n"
        f"Сейчас {status} · {source}.\n\n"
        f"Эта настройка управляет дальнейшим общением {subject}. "
        "Если пользователь отвечает на сообщение, отправленное аккаунтом, "
        "бот может продолжить с ним диалог через систему «Чаты». Это относится "
        "и к комментарию аккаунта под публикацией, и к ответу аккаунта другому участнику. "
        "При отключении входящие ответы на сообщения аккаунта не передаются в «Чаты», "
        "и бот не продолжает такой диалог.\n\n"
        "За ответы и дальнейшее общение в комментариях канала отвечают настройки «Чаты»: "
        "режим ответов, ИИ-агент, персона, правила, примеры и автообучение.\n\n"
        "Эта настройка не отключает анализ публикаций и не влияет на отдельную функцию "
        "публикации одного комментария к исходному посту."
    )
    rows = [[(
        "🔴 Отключить ответы коммент" if enabled else "🟢 Включить ответы коммент",
        f"channel:chatreplies:toggle:{'g' if scope == 'g' else 'c'}:{object_id}",
    )]]
    if binding is not None and binding.chat_replies_enabled_override is not None:
        rows.append([(
            "↩️ Наследовать глобальную настройку",
            f"channel:chatreplies:inherit:{object_id}",
        )])
    back = f"channel:gsettings:{account.id}" if binding is None else f"channel:csettings:{binding.id}"
    rows.append([("⬅️ К настройкам", back)])
    await edit(c, description, kb(rows))


@router.callback_query(F.data.startswith("channel:chatreplies:info:"))
async def channel_chat_replies_info(c):
    if not await check_cb(c): return
    _, _, _, scope, object_raw = c.data.split(":")
    if scope not in {"g", "c"}:
        await c.answer("Неизвестная настройка.", show_alert=True)
        return
    await channel_chat_replies_screen(c, scope, int(object_raw))


@router.callback_query(F.data.startswith("channel:chatreplies:toggle:"))
async def channel_chat_replies_toggle(c):
    if not await check_cb(c): return
    _, _, _, scope, object_raw = c.data.split(":")
    if scope not in {"g", "c"}:
        await c.answer("Неизвестная настройка.", show_alert=True)
        return
    object_id = int(object_raw)
    async with SessionFactory() as db:
        if scope == "g":
            account = await db.get(Account, object_id)
            if not account:
                await c.answer("Аккаунт не найден.", show_alert=True)
                return
            account.channel_chat_replies_enabled = not bool(account.channel_chat_replies_enabled)
        else:
            binding = await db.get(ChannelBinding, object_id)
            if not binding:
                await c.answer("Канал не найден.", show_alert=True)
                return
            account = await db.get(Account, binding.account_id)
            if not account:
                await c.answer("Аккаунт не найден.", show_alert=True)
                return
            binding.chat_replies_enabled_override = not effective_channel_chat_replies_enabled(
                account, binding
            )
        await db.commit()
    await channel_chat_replies_screen(c, scope, object_id)


@router.callback_query(F.data.startswith("channel:action:"))
async def channel_legacy_action(c):
    if not await check_cb(c): return
    _, _, scope, object_raw = c.data.split(":")
    if scope not in {"g", "c"}:
        await c.answer("Неизвестная настройка.", show_alert=True)
        return
    object_id = int(object_raw)
    await channel_comment_policy_screen(c, scope, object_id)

@router.callback_query(F.data.startswith("channel:inherit:"))
async def channel_inherit(c):
    if not await check_cb(c): return
    binding_id = int(c.data.split(":")[2])
    async with SessionFactory() as db:
        binding = await db.get(ChannelBinding, binding_id)
        if not binding:
            await c.answer("Канал не найден.", show_alert=True)
            return
        binding.post_instructions_override = None
        binding.comments_instructions_override = None
        binding.restrictions_instructions_override = None
        binding.comment_wait_seconds_override = None
        binding.min_comments_override = None
        binding.insufficient_action_override = None
        binding.reply_probability_override = None
        binding.reply_cooldown_seconds_override = None
        binding.reply_mode_override = None
        binding.chat_replies_enabled_override = None
        await db.commit()
    await channel_settings_screen(c, "channel", binding_id)

@router.callback_query(F.data.startswith("channel:learn:"))
async def channel_learn(c):
    if not await check_cb(c): return
    try:
        await c.answer()
    except Exception:
        pass
    binding_id = int(c.data.split(":")[2])
    async with SessionFactory() as db:
        binding = await db.get(ChannelBinding, binding_id)
        account = await db.get(Account, binding.account_id) if binding else None
        if not binding or not account:
            if c.message:
                await c.message.answer("⚠️ Канал или аккаунт не найден.")
            return
        if binding.learning_status == "training":
            if c.message:
                await c.message.answer("Автообучение этого канала уже выполняется.")
            return
        binding.learning_status = "training"
        binding.learning_started_at = datetime.now(timezone.utc)
        await db.commit()
        channel_title = binding.title
    if c.message:
        await c.message.answer(
            f"🧠 Автообучение канала «{channel_title}» поставлено в работу. "
            "Задача сохранена и автоматически продолжится после перезапуска Worker."
        )

@router.callback_query(F.data.startswith("channel:history:"))
async def channel_history(c):
    if not await check_cb(c): return
    aid = int(c.data.split(":")[2])
    async with SessionFactory() as db:
        rows = list((await db.scalars(
            select(ChannelAnalysis).where(ChannelAnalysis.account_id == aid)
            .order_by(ChannelAnalysis.created_at.desc()).limit(10)
        )).all())
    if not rows:
        await edit(c, f"🧾 История анализа каналов аккаунта #{aid} пока пуста.", kb([
            [("⬅️ К каналам аккаунта", f"channel:account:{aid}")], [("🏠 Главное меню", "nav:home")],
        ]))
        return
    buttons = [[(f"{'🟢' if row.on_topic else '🟠'} {row.channel_title[:25]} · пост #{row.post_id}", f"channel:analysis:{row.id}")] for row in rows]
    buttons += [[("⬅️ К каналам аккаунта", f"channel:account:{aid}"), ("🏠 Главное меню", "nav:home")]]
    await edit(c, f"🧾 Последние анализы аккаунта #{aid}.", kb(buttons))

@router.callback_query(F.data.startswith("channel:analysis:"))
async def channel_analysis_view(c):
    if not await check_cb(c): return
    analysis_id = int(c.data.split(":")[2])
    async with SessionFactory() as db:
        row = await db.get(ChannelAnalysis, analysis_id)
    if not row:
        await c.answer("Результат анализа не найден.", show_alert=True)
        return
    topic = "по теме" if row.on_topic else "не по теме"
    publication_labels = {
        "published": "Комментарий опубликован под исходным постом",
        "manual": "Подготовлен черновик · ручной режим",
        "skipped_ai": "ИИ решил, что комментарий не нужен",
        "skipped_probability": "Не выбран комментарий по вероятности",
        "skipped_cooldown": "Пропущено из-за паузы между ответами",
        "legacy": "Старый анализ · публикация не выполнялась",
        "pending": "Ожидает обработки",
        "approved": "Ожидает публикации",
        "publishing": "Публикуется",
    }
    if row.publication_status == "legacy":
        decision = "не определялось в старом анализе"
    elif row.publication_status == "skipped_probability":
        decision = "ответ не выбран по вероятности"
    else:
        decision = "ответ нужен" if row.should_reply else "ответ не требуется"
    text = (
        f"📡 {row.channel_title} · пост #{row.post_id}\n"
        f"Комментариев: {row.comments_count} · обсуждение: {topic}\n"
        f"Решение ИИ: {decision}\n"
        f"Статус: {publication_labels.get(row.publication_status, row.publication_status)}\n\n"
        f"Сводка ИИ:\n{row.comments_summary[:1800]}\n\n"
        f"Вариант комментария к публикации:\n{row.suggested_response[:1800] or 'ИИ не предложил публиковать комментарий.'}"
        + (f"\n\nОшибка публикации: {row.publication_error[:700]}" if row.publication_error else "")
    )
    await edit(c, text, kb([
        [("⬅️ История анализа", f"channel:history:{row.account_id}")],
        [("🏠 Главное меню", "nav:home")],
    ]))


# Channel discovery is a separate flow from group discovery. Every candidate is
# verified as a broadcast channel by Telegram before it can be selected or joined.
async def _channel_candidates_isolated(aid: int, candidates: list[dict], concurrency: int = 4):
    async with isolated_account_client(aid) as (_account, client):
        return await verify_channel_candidates(client, candidates, concurrency=concurrency)


def _channel_entity(entity) -> bool:
    return bool(
        getattr(entity, "broadcast", False)
        and not getattr(entity, "megagroup", False)
        and not getattr(entity, "gigagroup", False)
    )


def _channel_candidate_keyboard(aid: int, candidates: list[dict], offset: int = 0):
    rows = []
    for index, item in enumerate(candidates[offset:offset + 12], start=offset):
        title = str(item.get("title") or "Канал")[:30]
        subscribers = int(item.get("members") or 0)
        suffix = f" · {subscribers:,}".replace(",", " ") if subscribers else ""
        rows.append([(f"📣 {title}{suffix}"[:60], f"channel:candidate:{aid}:{index}")])
    navigation = []
    if offset > 0:
        navigation.append(("⬅️ Назад", f"channel:page:{aid}:{max(0, offset - 12)}"))
    if offset + 12 < len(candidates):
        navigation.append(("➡️ Ещё результаты", f"channel:page:{aid}:{offset + 12}"))
    if navigation:
        rows.append(navigation)
    if any(item.get("verified") and item.get("link") for item in candidates):
        rows.append([("🔗 Показать ссылками", f"channel:links:{aid}")])
    rows.append([("⬅️ Инструменты", "tool:menu"), ("🏠 Главное меню", "nav:home")])
    return kb(rows)


async def _channel_search_prompt(c: CallbackQuery, state: FSMContext, account_ids: list[int]):
    if not account_ids:
        await c.answer("Нет включённых аккаунтов.", show_alert=True)
        return
    await state.clear()
    await state.update_data(
        ui_chat_id=c.message.chat.id,
        ui_message_id=c.message.message_id,
        return_callback="tool:menu",
        channel_search_account_id=account_ids[0],
        channel_search_account_ids=account_ids,
    )
    await state.set_state(Flow.channel_search_query)
    label = "всеми аккаунтами" if len(account_ids) > 1 else f"аккаунтом #{account_ids[0]}"
    await edit(
        c,
        f"📣 Поиск Telegram-каналов · {label}\n\n"
        "Введите тему или несколько ключевых слов. Например: «технологии», "
        "«новости IT», «автомобили».\n\n"
        "Поиск соберёт кандидатов из Telegram и нескольких поисковых систем, "
        "затем проверит каждую ссылку через Telegram API. В результатах останутся "
        "только каналы вещания; группы, чаты, профили, боты и нерабочие ссылки исключаются.",
        CANCEL,
    )


@router.callback_query(F.data == "tool:channels")
async def tool_channels_accounts(c: CallbackQuery):
    if not await check_cb(c):
        return
    async with SessionFactory() as db:
        accounts = list((await db.scalars(
            select(Account).where(Account.enabled.is_(True)).order_by(Account.id)
        )).all())
    if not accounts:
        await edit(c, "📣 Поиск каналов\n\nНет включённых аккаунтов. Сначала подключите и включите Telegram-аккаунт.", kb([
            [("👤 Аккаунты", "acc:list")],
            [("⬅️ Инструменты", "tool:menu")],
        ]))
        return
    rows = [[(f"🌐 Все аккаунты · {len(accounts)}", "tool:channels:all")]]
    rows += [[(f"👤 {account.display_name[:32]} · #{account.id}", f"tool:channels:account:{account.id}")]
             for account in accounts]
    rows.append([("⬅️ Инструменты", "tool:menu")])
    await edit(
        c,
        "📣 Поиск Telegram-каналов\n\n"
        "Можно искать всеми включёнными аккаунтами сразу или выбрать один. "
        "В результатах будут только публичные и доступные по приглашению каналы вещания — "
        "группы и супергруппы будут исключены.",
        kb(rows),
    )


@router.callback_query(F.data == "tool:channels:all")
async def tool_channels_all(c: CallbackQuery, state: FSMContext):
    if not await check_cb(c):
        return
    async with SessionFactory() as db:
        ids = list((await db.scalars(
            select(Account.id).where(Account.enabled.is_(True)).order_by(Account.id)
        )).all())
    await _channel_search_prompt(c, state, [int(value) for value in ids])


@router.callback_query(F.data.startswith("tool:channels:account:"))
async def tool_channels_account(c: CallbackQuery, state: FSMContext):
    if not await check_cb(c):
        return
    aid = int(c.data.rsplit(":", 1)[1])
    async with SessionFactory() as db:
        account = await db.get(Account, aid)
    if not account or not account.enabled:
        await c.answer("Аккаунт не найден или выключен.", show_alert=True)
        return
    await _channel_search_prompt(c, state, [aid])


@router.message(Flow.channel_search_query)
async def channel_search_query(m: Message, state: FSMContext):
    if not await check_msg(m) or not await private(m):
        return
    data = await state.get_data()
    query = " ".join((m.text or "").split())
    await delete_msg(m)
    if len(query) < 2:
        await prompt_message(m, state, "Введите хотя бы два символа для поиска.")
        return
    query = query[:100]
    account_ids = list(dict.fromkeys(
        int(value) for value in (data.get("channel_search_account_ids") or [])
        if str(value).isdigit()
    ))
    aid = int(data.get("channel_search_account_id", account_ids[0] if account_ids else 0))
    if aid not in account_ids:
        account_ids.insert(0, aid)
    if not account_ids:
        await finish_prompt(m, state, "Не выбраны включённые аккаунты.", kb([
            [("📣 Поиск каналов", "tool:channels")],
            [("⬅️ Инструменты", "tool:menu")],
        ]))
        return
    anchor = {"ui_chat_id": data.get("ui_chat_id"), "ui_message_id": data.get("ui_message_id")}
    await state.clear()
    await edit_anchor(
        m.bot, anchor,
        f"📣 Ищу каналы по запросу «{query}»\n\n"
        f"Telegram-поиск: {len(account_ids)} аккаунт(ов)\n"
        "1/3 · Ищу в Telegram и нескольких поисковых системах…\n"
        "2/3 · Объединяю результаты и проверяю тип ссылок через Telegram API.",
        kb([[("⬅️ Инструменты", "tool:menu")]]),
    )

    try:
        async def search_one_account(search_aid: int):
            try:
                async with SessionFactory() as db:
                    search_account = await db.get(Account, search_aid)
                if not search_account or not search_account.enabled:
                    return []
                async with isolated_telegram_client(search_account) as search_client:
                    if not await search_client.is_user_authorized():
                        return []
                    return await search_public_channels(search_client, query, limit=100)
            except Exception as exc:
                logging.warning("Channel discovery failed for account %s: %s", search_aid, exc)
                return []

        async def search_accounts():
            semaphore = asyncio.Semaphore(3)
            async def limited(search_aid):
                async with semaphore:
                    return await search_one_account(search_aid)
            batches = await asyncio.gather(
                *(limited(search_aid) for search_aid in account_ids),
                return_exceptions=True,
            )
            return [item for batch in batches if isinstance(batch, list) for item in batch]

        native_task = asyncio.create_task(search_accounts())
        web_task = asyncio.create_task(search_web_channels(query, limit=600, pages_per_query=5))
        native_result, web_result = await asyncio.gather(
            native_task, web_task, return_exceptions=True
        )
        native_results = native_result if isinstance(native_result, list) else []
        web_results = web_result if isinstance(web_result, list) else []
        combined = {}
        for item in native_results + web_results:
            link = str(item.get("link") or "").strip()
            if not link:
                continue
            key = link.casefold()
            if key in combined:
                current = combined[key]
                current["score"] = max(int(current.get("score") or 0), int(item.get("score") or 0))
                current["source"] = ", ".join(dict.fromkeys(
                    (current.get("source", "") + ", " + item.get("source", "")).strip(", ").split(", ")
                ))
            else:
                combined[key] = item

        raw_candidates = list(combined.values())[:600]
        await edit_anchor(
            m.bot, anchor,
            f"📣 Проверяю найденные ссылки · «{query}»\n\n"
            f"Аккаунтов в поиске: {len(account_ids)}\n"
            f"Кандидатов собрано: {len(raw_candidates)}\n"
            "Проверяю тип каждого канала непосредственно в Telegram. Группы и нерабочие ссылки отбрасываются.",
            kb([[("⬅️ Инструменты", "tool:menu")]]),
        )
        candidates = await _channel_candidates_isolated(aid, raw_candidates, concurrency=4)
    except Exception as exc:
        retry = "tool:channels:all" if len(account_ids) > 1 else f"tool:channels:account:{aid}"
        await edit_anchor(
            m.bot, anchor,
            f"❌ Не удалось выполнить поиск или проверить результаты.\n\n"
            f"{type(exc).__name__}: {str(exc)[:220]}\n\n"
            "Проверьте подключение аккаунта и повторите поиск.",
            kb([[("📣 Повторить поиск", retry)], [("⬅️ Инструменты", "tool:menu")]]),
        )
        return

    CHANNEL_CANDIDATES[(m.from_user.id, aid)] = candidates
    retry = "tool:channels:all" if len(account_ids) > 1 else f"tool:channels:account:{aid}"
    if not candidates:
        await edit_anchor(
            m.bot, anchor,
            f"По запросу «{query}» не удалось найти каналы, которые Telegram подтвердил как каналы вещания.\n\n"
            "Это не означает, что таких каналов нет: поисковики могут ограничивать выдачу, "
            "а Telegram не предоставляет полный каталог всех каналов. Попробуйте синонимы "
            "или более короткий запрос.",
            kb([[("📣 Новый поиск", retry)], [("⬅️ Инструменты", "tool:menu")]]),
        )
        return

    lines = [
        f"📣 Найдено каналов: {len(candidates)} · запрос «{query}»",
        f"Поиск выполнен через {len(account_ids)} аккаунт(ов) Telegram",
        "✓ Каждая ссылка проверена через Telegram API",
        "Группы, супергруппы, боты и профили исключены.",
        "",
        f"Показываю 1–{min(12, len(candidates))}. Нажмите на канал, чтобы открыть проверку.",
    ]
    for index, item in enumerate(candidates[:12], 1):
        subscribers = (
            f"{int(item.get('members') or 0):,}".replace(",", " ") + " подписчиков"
            if item.get("members") else "подписчики: неизвестно"
        )
        visibility = "приватный канал" if item.get("kind") == "channel" and not item.get("username") else "канал"
        lines.append(f"{index}. {str(item.get('title') or 'Канал')[:75]} · {subscribers} · {visibility}")
    await edit_anchor(m.bot, anchor, "\n".join(lines), _channel_candidate_keyboard(aid, candidates, 0))


@router.callback_query(F.data.startswith("channel:links:"))
async def channel_candidate_links(c: CallbackQuery):
    if not await check_cb(c):
        return
    aid = int(c.data.split(":")[2])
    candidates = CHANNEL_CANDIDATES.get((c.from_user.id, aid), [])
    links = list(dict.fromkeys(
        str(item.get("link") or "").strip()
        for item in candidates if item.get("verified") and item.get("link")
    ))
    if not links:
        await c.answer("Нет проверенных ссылок. Запустите поиск заново.", show_alert=True)
        return
    await c.answer("Отправляю список ссылок.")
    header = f"🔗 Проверенные ссылки на каналы · всего {len(links)}\n\n"
    chunks, current, current_len = [], [], len(header)
    for link in links:
        if current and current_len + len(link) + 1 > 3700:
            chunks.append(header + "\n".join(current))
            current, current_len = [], 0
        current.append(link)
        current_len += len(link) + 1
    if current:
        chunks.append((header if not chunks else "") + "\n".join(current))
    for index, chunk in enumerate(chunks, 1):
        if len(chunks) > 1:
            chunk = f"📄 Часть {index}/{len(chunks)}\n" + chunk
        await c.message.answer(chunk, disable_web_page_preview=True)


@router.callback_query(F.data.startswith("channel:page:"))
async def channel_candidate_page(c: CallbackQuery):
    if not await check_cb(c):
        return
    _, _, aid_raw, offset_raw = c.data.split(":")
    aid, offset = int(aid_raw), max(0, int(offset_raw))
    candidates = CHANNEL_CANDIDATES.get((c.from_user.id, aid), [])
    if not candidates:
        await edit(c, "Результаты больше не сохранены. Запустите поиск ещё раз.", kb([
            [("📣 Поиск каналов", f"tool:channels:account:{aid}")],
            [("⬅️ Инструменты", "tool:menu")],
        ]))
        return
    page = candidates[offset:offset + 12]
    lines = [
        f"Найденные каналы · аккаунт #{aid}",
        f"Показаны {offset + 1}–{min(offset + 12, len(candidates))} из {len(candidates)}",
        "",
        "Выберите канал для проверки.",
    ]
    for index, item in enumerate(page, start=offset + 1):
        lines.append(f"{index}. {str(item.get('title') or item.get('link') or 'Канал')[:70]}")
    await edit(c, "\n".join(lines), _channel_candidate_keyboard(aid, candidates, offset))


@router.callback_query(F.data.startswith("channel:results:"))
async def channel_results_back(c: CallbackQuery):
    if not await check_cb(c):
        return
    aid = int(c.data.split(":")[2])
    candidates = CHANNEL_CANDIDATES.get((c.from_user.id, aid), [])
    if not candidates:
        await edit(c, "Результаты больше не сохранены. Запустите поиск ещё раз.", kb([
            [("📣 Поиск каналов", f"tool:channels:account:{aid}")],
            [("⬅️ Инструменты", "tool:menu")],
        ]))
        return
    lines = [f"Найденные каналы · аккаунт #{aid}", "", "Выберите канал для проверки."]
    for index, item in enumerate(candidates[:12], 1):
        lines.append(f"{index}. {str(item.get('title') or item.get('link') or 'Канал')[:70]}")
    await edit(c, "\n".join(lines), _channel_candidate_keyboard(aid, candidates, 0))


@router.callback_query(F.data.startswith("channel:candidate:"))
async def channel_candidate_preview(c: CallbackQuery):
    if not await check_cb(c):
        return
    _, _, aid_raw, index_raw = c.data.split(":")
    aid, index = int(aid_raw), int(index_raw)
    candidates = CHANNEL_CANDIDATES.get((c.from_user.id, aid), [])
    if index < 0 or index >= len(candidates):
        await c.answer("Результаты устарели. Запустите поиск ещё раз.", show_alert=True)
        return
    item = candidates[index]
    link = item.get("link")
    if not link:
        await c.answer("У этого канала нет ссылки.", show_alert=True)
        return
    try:
        kind, target = _parse_chat_link(link)
        async with isolated_account_client(aid) as (_account, client):
            already = False
            if kind == "public":
                entity = await client.get_entity(target)
                if not _channel_entity(entity):
                    raise ValueError("Это группа или профиль, а не канал. Проверка отменена.")
                title = getattr(entity, "title", None) or target
                subscribers = int(getattr(entity, "participants_count", 0) or 0)
                entity_id = utils.get_peer_id(entity)
            else:
                invite = await client(CheckChatInviteRequest(hash=target))
                if isinstance(invite, ChatInviteAlready):
                    entity = invite.chat
                    if not _channel_entity(entity):
                        raise ValueError("Ссылка ведёт не на канал. Проверка отменена.")
                    title = getattr(entity, "title", None) or "Канал"
                    subscribers = int(getattr(entity, "participants_count", 0) or 0)
                    entity_id = utils.get_peer_id(entity)
                    already = True
                else:
                    if not getattr(invite, "channel", False) or getattr(invite, "megagroup", False):
                        raise ValueError("Ссылка ведёт на группу, а не на канал.")
                    title = getattr(invite, "title", None) or "Приватный канал"
                    subscribers = int(getattr(invite, "participants_count", 0) or 0)
                    entity_id = None
    except Exception as exc:
        await edit(c, f"Не удалось проверить ссылку:\n{str(exc)[:220]}", kb([
            [("⬅️ К результатам", f"channel:results:{aid}")],
            [("⬅️ Инструменты", "tool:menu")],
        ]))
        return
    CHANNEL_PENDING_JOIN[(c.from_user.id, aid)] = {
        "link": link, "kind": kind, "target": target, "title": title,
        "members": subscribers, "entity_id": entity_id, "already": already,
    }
    status = "Аккаунт уже состоит в канале." if already else "Подписка произойдёт только после отдельного подтверждения."
    await edit(c,
        f"📣 {title}\nПодписчиков: {subscribers or 'неизвестно'}\nСсылка: {link}\n\n"
        f"{status}\n\nПроверьте название и ссылку перед подпиской.",
        kb([
            [("✅ Подтвердить подписку", f"channel:joinconfirm:{aid}")],
            [("⬅️ К результатам", f"channel:results:{aid}")],
            [("⬅️ Инструменты", "tool:menu")],
        ]),
    )


@router.callback_query(F.data.startswith("channel:joinconfirm:"))
async def channel_join_confirm(c: CallbackQuery):
    if not await check_cb(c):
        return
    aid = int(c.data.split(":")[2])
    pending = CHANNEL_PENDING_JOIN.get((c.from_user.id, aid))
    if not pending:
        await c.answer("Предпросмотр устарел. Проверьте канал ещё раз.", show_alert=True)
        return
    try:
        async with isolated_account_client(aid) as (_account, client):
            entity = None
            if pending.get("already") and pending.get("entity_id"):
                entity = await client.get_entity(pending["entity_id"])
            elif pending["kind"] == "public":
                entity = await client.get_entity(pending["target"])
                if not _channel_entity(entity):
                    raise ValueError("Ссылка больше не ведёт на канал.")
                async def report_floodwait(seconds):
                    await edit(c,
                        f"⏳ Telegram временно ограничил вступления. Жду {seconds} сек., затем повторю подписку на канал «{pending['title']}»…",
                        kb([[("⬅️ К результатам", f"channel:results:{aid}")]]),
                    )
                await perform_join_request(aid, client, JoinChannelRequest(entity), report_floodwait)
            else:
                async def report_floodwait(seconds):
                    await edit(c,
                        f"⏳ Telegram временно ограничил вступления. Жду {seconds} сек., затем повторю подписку на канал «{pending['title']}»…",
                        kb([[("⬅️ К результатам", f"channel:results:{aid}")]]),
                    )
                joined = await perform_join_request(
                    aid, client, ImportChatInviteRequest(hash=pending["target"]), report_floodwait
                )
                entity = next((chat for chat in joined.chats if _channel_entity(chat)), None)
                if entity is None:
                    raise RuntimeError("Telegram не вернул вступивший канал.")
            if entity is None:
                entity = await client.get_entity(pending["target"])
            if not _channel_entity(entity):
                raise ValueError("Ссылка не ведёт на канал.")
            channel_id = int(utils.get_peer_id(entity))
            title = (getattr(entity, "title", None) or pending["title"] or "Канал")[:255]
    except Exception as exc:
        if isinstance(exc, FloodWaitError):
            await edit(c, f"⛔ Telegram ограничил вступления. Повторная попытка остановлена. Подождите {int(exc.seconds)} сек. и попробуйте снова.", kb([[('📣 Другой канал', f'tool:channels:account:{aid}')], [('⬅️ К результатам', f'channel:results:{aid}')]]))
            return
        if "InviteRequestSentError" in type(exc).__name__:
            await edit(c, "Запрос на подписку отправлен администраторам канала. Канал появится в списке после одобрения.", kb([
                [("📣 Проверить другой канал", f"tool:channels:account:{aid}")],
                [("⬅️ Инструменты", "tool:menu")],
            ]))
            return
        if "UserAlreadyParticipantError" in type(exc).__name__:
            try:
                async with isolated_account_client(aid) as (_account, client):
                    if pending["kind"] == "public":
                        entity = await client.get_entity(pending["target"])
                    else:
                        invite = await client(CheckChatInviteRequest(hash=pending["target"]))
                        entity = invite.chat if isinstance(invite, ChatInviteAlready) else None
                    if entity is None or not _channel_entity(entity):
                        raise RuntimeError("Не удалось определить канал, на который уже подписан аккаунт.")
                    channel_id = int(utils.get_peer_id(entity))
                    title = (getattr(entity, "title", None) or pending["title"] or "Канал")[:255]
            except Exception as nested:
                await edit(c, f"Аккаунт уже подписан, но не удалось обновить список каналов: {type(nested).__name__}.", kb([
                    [("⬅️ К результатам", f"channel:results:{aid}")],
                    [("⬅️ Инструменты", "tool:menu")],
                ]))
                return
        else:
            await edit(c, f"Не удалось подписаться: {type(exc).__name__}. Проверьте срок действия ссылки, лимиты Telegram и доступность канала.", kb([
                [("📣 Другой канал", f"tool:channels:account:{aid}")],
                [("⬅️ К результатам", f"channel:results:{aid}")],
            ]))
            return

    async with SessionFactory() as db:
        binding = await db.scalar(select(ChannelBinding).where(
            ChannelBinding.account_id == aid, ChannelBinding.channel_id == channel_id
        ))
        if binding:
            binding.title = title
        else:
            db.add(ChannelBinding(
                account_id=aid, channel_id=channel_id, title=title, enabled=False
            ))
        await db.commit()
    CHANNEL_PENDING_JOIN.pop((c.from_user.id, aid), None)
    await edit(c,
        f"✅ Канал «{title}» добавлен в список каналов аккаунта #{aid}.\n\n"
        "Подписка оформлена. Обработка канала пока выключена — при необходимости включите её в разделе «Каналы» аккаунта.",
        kb([
            [("📡 Открыть каналы аккаунта", f"channel:account:{aid}")],
            [("👤 К аккаунту", f"acc:view:{aid}")],
            [("🏠 Главное меню", "nav:home")],
        ]),
    )
